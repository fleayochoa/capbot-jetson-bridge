"""Servidor WebSocket de telemetría.

Mantiene un set de clientes conectados. La telemetría entra por el bus
(evento TELEMETRY) y se vuelca a todos los clientes en JSON.

Publicación a 50 Hz: no se genera aquí — el ESP32 manda sensores y los
eventos TELEMETRY llegan al ritmo que los produzca. Si el ESP32 publica a
100 Hz pero queremos 50 Hz de cara al host, decimamos aquí (un contador).

Cada cliente tiene su propia cola bounded: si se atasca (cliente lento o
red congestionada), descartamos lo más viejo para no acumular memoria.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    websockets = None
    WebSocketServerProtocol = object  # type: ignore
    ConnectionClosed = Exception     # type: ignore

from config import CFG
from core.bus import Ev, bus
from core.state import state

log = logging.getLogger(__name__)


class _Client:
    __slots__ = ("ws", "queue", "dropped")

    def __init__(self, ws: WebSocketServerProtocol) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(CFG.telemetry.ws_queue_max)
        self.dropped = 0


class TelemetryServer:
    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self._lock = asyncio.Lock()
        self._decimator_counter = 0
        self._decimator_n = 1  # 1 = no decima
        self._last_pub_ts = 0.0

    # -------------------- pub/sub --------------------
    def attach(self) -> None:
        bus.on(Ev.TELEMETRY, self._on_telemetry)

    async def _on_telemetry(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        # Decimador simple para limitar a publish_hz aprox. El ESP32 puede
        # mandar más rápido que lo que queremos publicar. Si queremos
        # EXACTAMENTE 50Hz, usar un temporizador dedicado; aquí lo acotamos.
        now = time.time()
        min_interval = 1.0 / CFG.telemetry.publish_hz
        if now - self._last_pub_ts < min_interval:
            return
        self._last_pub_ts = now

        # Anexamos metadata
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

    def _enqueue(self, c: _Client, payload: str) -> None:
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
    async def _handler(self, ws: WebSocketServerProtocol) -> None:
        client = _Client(ws)
        async with self._lock:
            self._clients.add(client)
        log.info("Cliente WS conectado: %s (total=%d)", ws.remote_address, len(self._clients))
        send_task = asyncio.create_task(self._send_loop(client))
        try:
            # Mantenemos el socket vivo consumiendo lo que mande el cliente
            # (por si en el futuro queremos canal inverso; ahora lo ignoramos).
            async for _ in ws:
                pass
        except ConnectionClosed:
            pass
        finally:
            send_task.cancel()
            async with self._lock:
                self._clients.discard(client)
            log.info("Cliente WS desconectado: %s (dropped=%d)", ws.remote_address, client.dropped)

    async def _send_loop(self, c: _Client) -> None:
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