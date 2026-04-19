"""Entry point del servicio Jetson.

Lanza en paralelo:
  - Servidor UDP de comandos (+ ACKs)
  - Servidor WebSocket de telemetría
  - Pipeline GStreamer de video
  - Enlace serial con ESP32
  - Watchdog de heartbeat del host

Todo coordinado por un único `stop_event` que se dispara con SIGINT/SIGTERM.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from config import CFG
from core.heartbeat import run_host_watchdog
from hw.esp32_link import Esp32Link
from net.udp_server import run_udp_server
from net.video_pipeline import run_video_pipeline
from net.ws_server import run_ws_server

log = logging.getLogger("jetson_service")


def parse_args(argv: list[str]) -> argparse.Namespace:
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


async def amain(stop_event: asyncio.Event) -> None:
    esp32 = Esp32Link()

    tasks = [
        asyncio.create_task(run_udp_server(stop_event), name="udp"),
        asyncio.create_task(run_ws_server(stop_event), name="ws"),
        asyncio.create_task(run_video_pipeline(stop_event), name="video"),
        asyncio.create_task(esp32.run(stop_event), name="esp32"),
        asyncio.create_task(run_host_watchdog(), name="host-watchdog"),
    ]

    log.info("Servicio Jetson arrancado. Tareas: %s", [t.get_name() for t in tasks])

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    # Propagar excepciones de las tareas terminadas
    for t in done:
        exc = t.exception()
        if exc:
            log.error("Tarea %s terminó con excepción: %s", t.get_name(), exc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    setup_logging(args.log_level)
    apply_cli(args)

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()

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
        loop.run_until_complete(amain(stop_event))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())