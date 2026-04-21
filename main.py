"""Entry point del servicio Jetson.

Lanza en paralelo:
  - Servidor UDP de comandos (+ ACKs)
  - Servidor WebSocket de telemetría
  - Pipeline GStreamer de video
  - Enlace serial con ESP32
  - Watchdog de heartbeat del host

Todo coordinado por un único `stop_event` que se dispara con SIGINT/SIGTERM.
"""
# PY36: Eliminado `from __future__ import annotations` (PEP 563, disponible desde 3.7).
#       Sin él, las anotaciones se evalúan en tiempo de definición, por lo que todas
#       las anotaciones de tipo deben ser *nombres reales e importables* en 3.6.

import argparse
import asyncio
import logging
import signal
import sys

# PY36: Importamos tipos desde `typing` porque en 3.6 no existen los genéricos
#       built-in (PEP 585: `list[str]` requiere 3.9+; `X | None` requiere 3.10+).
from typing import List, Optional  # PY36: añadido

from config import CFG
from core.heartbeat import run_host_watchdog
from hw.esp32_link import Esp32Link
from net.udp_server import run_udp_server
from net.video_pipeline import run_video_pipeline
from net.ws_server import run_ws_server

log = logging.getLogger("jetson_service")


# PY36: La firma original era `argv: list[str]`. En 3.6 `list[...]` no es subscriptible
#       como anotación → usamos `List[str]` de `typing`.
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jetson Nano teleoperation service")
    p.add_argument("--host", default=CFG.network.listen_host,
                   help="Interfaz en la que escuchar UDP/WS (default: 0.0.0.0)")
    p.add_argument("--host-ip", default="",
                   help="IP del PC host (si se omite, se auto-detecta desde UDP)")
    p.add_argument("--serial", default=CFG.serial.port,
                   help="Puerto serial del ESP32 (default: /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=CFG.serial.baudrate,
                   help="Baudrate del serial (default: 921600)")
    p.add_argument("--hb-timeout-ms", type=int, default=CFG.network.host_heartbeat_timeout_ms,
                   help="Timeout de heartbeat del host en ms (default: 500)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def apply_cli(args: argparse.Namespace) -> None:
    CFG.network.listen_host = args.host
    CFG.network.host_ip = args.host_ip
    CFG.network.host_heartbeat_timeout_ms = args.hb_timeout_ms
    CFG.serial.port = args.serial
    CFG.serial.baudrate = args.baud


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# PY36: `loop` ahora se pasa como argumento explícito. En el original, el código
#       llamaba a `asyncio.get_running_loop()` dentro de las corrutinas (API de 3.7+).
#       En 3.6 lo más limpio y seguro es pasar el loop explícitamente y que cada
#       corrutina lo use para crear Queue/Event y para run_in_executor.
async def amain(stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    # PY36: Esp32Link internamente crea una `asyncio.Queue`. En 3.6 la Queue
    #       captura el loop por defecto al construirse; por eso le pasamos el loop
    #       explícitamente para evitar ambigüedades (el parámetro `loop` fue
    #       deprecado en 3.8 y removido en 3.10, pero en 3.6 sigue siendo válido).
    esp32 = Esp32Link(loop=loop)

    # PY36: El original usaba `asyncio.create_task(coro, name="...")`:
    #       - `asyncio.create_task` fue añadido en 3.7.
    #       - El parámetro `name=` fue añadido en 3.8.
    #       En 3.6 usamos `loop.create_task(coro)` y mantenemos los nombres
    #       en un dict paralelo para los logs.
    task_specs = [
        ("udp",           run_udp_server(stop_event, loop)),
        ("ws",            run_ws_server(stop_event)),
        ("video",         run_video_pipeline(stop_event, loop)),
        ("esp32",         esp32.run(stop_event)),
        ("host-watchdog", run_host_watchdog()),
    ]
    # PY36: `loop.create_task` existe desde 3.4.2, así que es seguro en 3.6.
    tasks = [loop.create_task(coro) for _, coro in task_specs]
    task_names = {t: name for t, (name, _) in zip(tasks, task_specs)}

    log.info("Servicio Jetson arrancado. Tareas: %s", list(task_names.values()))

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    # PY36: `asyncio.gather` existe desde 3.4; sin cambios.
    await asyncio.gather(*pending, return_exceptions=True)

    # Propagar excepciones de las tareas terminadas
    for t in done:
        exc = t.exception()
        if exc:
            # PY36: Sin `t.get_name()` (3.8+); usamos el dict externo.
            log.error("Tarea %s terminó con excepción: %s", task_names.get(t, "?"), exc)


# PY36: Firma original `argv: list[str] | None`. En 3.6 no existe ni `list[str]`
#       como genérico ni el operador `|` para uniones → `Optional[List[str]]`.
def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    setup_logging(args.log_level)
    apply_cli(args)

    # PY36: `asyncio.new_event_loop()` existe desde 3.4; sin cambio.
    loop = asyncio.new_event_loop()
    # PY36: Importante: fijamos este loop como "loop actual" ANTES de crear
    #       `asyncio.Event()`. En 3.6 `asyncio.Event()` sin argumento captura el
    #       loop vía `events.get_event_loop()`; si no hay loop actual, ese
    #       llamado podría crear uno distinto silenciosamente. El original no
    #       tenía esta sutileza porque en 3.10+ el loop se asocia al entrar en
    #       `run()`, no al instanciar el Event.
    asyncio.set_event_loop(loop)

    # PY36: En 3.6 `asyncio.Event()` acepta (y a veces requiere) `loop=`.
    #       Lo pasamos explícitamente por claridad.
    stop_event = asyncio.Event(loop=loop)

    def _on_signal():
        log.info("Señal de parada recibida")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows: add_signal_handler no soportado. Usamos signal.signal.
            signal.signal(sig, lambda *_: _on_signal())

    try:
        # PY36: Pasamos el loop a `amain` en vez de obtenerlo dentro con
        #       `get_running_loop()` (esa API no existe en 3.6).
        loop.run_until_complete(amain(stop_event, loop))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())