"""Servidor WebSocket de telemetría.

Mantiene un set de clientes conectados. La telemetría entra por el bus
(evento TELEMETRY) y se vuelca a todos los clientes en JSON.

Publicación a 50 Hz: no se genera aquí — el ESP32 manda sensores y los
eventos TELEMETRY llegan al ritmo que los produzca. Decimamos aquí para
no superar CFG.telemetry.publish_hz.

Cada cliente tiene su propia cola bounded: si se atasca (cliente lento o
red congestionada), descartamos lo más viejo para no acumular memoria.
"""
# PY36: Eliminado `from __future__ import annotations`.
import asyncio
import json
import logging
import time

# PY36: Any para anotar payloads genéricos; Set/Optional reemplazan sintaxis 3.9+/3.10+.
from typing import Any, Optional, Set  # PY36: añadido

# PY36: La dependencia `websockets` tiene una ruptura de API importante:
#       - websockets 10.0 (oct 2021) dejó de soportar Python 3.6 y cambió
#         el handler de 2 args (ws, path) a 1 arg (ws).
#       - websockets 9.1 es la última versión compatible con 3.6 y usa
#         `async def handler(ws, path)`.
#       Adaptamos el handler a la API de 9.x (con `path`) y congelamos la
#       versión en requirements.
try:
    import websockets
    # PY36: En websockets 9.x `WebSocketServerProtocol` está en el paquete raíz.
    #       En 10+ también vive en `websockets.server`. Para robustez probamos
    #       las dos rutas.
    try:
        from websockets.server import WebSocketServerProtocol
    except ImportError:  # websockets 9.x
        from websockets import WebSocketServerProtocol  # type: ignore
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    websockets = None
    WebSocketServerProtocol = object  # type: ignore
    ConnectionClosed = Exception      # type: ignore

from config import CFG
from core.bus import Ev, bus
from core.state import state

log = logging.getLogger(__name__)


class _Client:
    __slots__ = ("ws", "queue", "dropped")

    def __init__(self, ws) -> None:
        self.ws = ws
        # PY36: `asyncio.Queue[str]` no es subscriptible → sin genérico.
        #       Pasamos el loop para que 3.6 no intente resolverlo implícitamente.
        self.queue = asyncio.Queue(CFG.telemetry.ws_queue_max,
                                   loop=asyncio.get_event_loop())  # PY36: loop=
        self.dropped = 0


class TelemetryServer:
    def __init__(self) -> None:
        # PY36: `set[_Client]` → `Set[_Client]` (typing).
        self._clients = set()  # type: Set[_Client]
        # PY36: Igual que con Queue, `asyncio.Lock()` captura el loop por
        #       defecto al instanciarse. Le pasamos `loop=` para determinismo.
        self._lock = asyncio.Lock(loop=asyncio.get_event_loop())  # PY36: loop=
        self._decimator_counter = 0
        self._decimator_n = 1  # 1 = no decima
        self._last_pub_ts = 0.0

    # -------------------- pub/sub --------------------
    def attach(self) -> None:
        bus.on(Ev.TELEMETRY, self._on_telemetry)

    async def _on_telemetry(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        now = time.time()
        min_interval = 1.0 / CFG.telemetry.publish_hz
        if now - self._last_pub_ts < min_interval:
            return
        self._last_pub_ts = now

        msg = {
            "t": now,
            "seq": state.telemetry_published,
            "data": data,
            "host_online": (now - state.host_last_seen) < (CFG.network.host_heartbeat_timeout_ms / 1000.0),
            "esp32_online": state.esp32_connected,
        }
        payload = json.dumps(msg, separators=(",", ":"))
        state.telemetry_published += 1

        # Distribuir a clientes
        async with self._lock:
            targets = list(self._clients)
        for c in targets:
            self._enqueue(c, payload)

    def _enqueue(self, c: "_Client", payload: str) -> None:
        try:
            c.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Descartar uno viejo y meter el nuevo
            try:
                c.queue.get_nowait()
                c.queue.put_nowait(payload)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            c.dropped += 1

    # -------------------- handler WS --------------------
    # PY36: El handler en websockets 9.x recibe (ws, path). En 10+ es (ws,) solo.
    #       Aceptamos `path` y lo ignoramos, así el handler es compatible con
    #       ambas versiones (10+ lo tolera porque permite signatures con 2 args
    #       vía deprecation warning hasta 11.x).
    async def _handler(self, ws, path=None) -> None:  # PY36: añadido path=None
        client = _Client(ws)
        async with self._lock:
            self._clients.add(client)
        log.info("Cliente WS conectado: %s (total=%d)", ws.remote_address, len(self._clients))

        # PY36: `asyncio.create_task` no existe en 3.6 → usamos el loop actual.
        loop = asyncio.get_event_loop()
        send_task = loop.create_task(self._send_loop(client))

        try:
            # Mantenemos el socket vivo consumiendo lo que mande el cliente
            async for _ in ws:
                pass
        except ConnectionClosed:
            pass
        finally:
            send_task.cancel()
            async with self._lock:
                self._clients.discard(client)
            log.info("Cliente WS desconectado: %s (dropped=%d)", ws.remote_address, client.dropped)

    async def _send_loop(self, c: "_Client") -> None:
        try:
            while True:
                payload = await c.queue.get()
                await c.ws.send(payload)
        except (asyncio.CancelledError, ConnectionClosed):
            return
        except Exception:
            log.exception("Error enviando a cliente WS")


async def run_ws_server(stop_event: asyncio.Event) -> None:
    if websockets is None:
        log.error("websockets no está instalado; servidor WS no arranca")
        await stop_event.wait()
        return

    server = TelemetryServer()
    server.attach()

    # PY36: `async with websockets.serve(...)` (context manager async) funciona
    #       en websockets 8+/9+ y en 3.6. Se mantiene igual.
    async with websockets.serve(
        server._handler,
        CFG.network.listen_host,
        CFG.network.ws_telemetry_port,
        ping_interval=5,
        ping_timeout=5,
        max_size=2**20,
    ):
        log.info(
            "WS telemetría en ws://%s:%d",
            CFG.network.listen_host,
            CFG.network.ws_telemetry_port,
        )
        await stop_event.wait()