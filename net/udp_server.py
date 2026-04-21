"""Servidor UDP de comandos.

Escucha en `udp_cmd_port` frames binarios del host. Por cada frame válido:
  1. Actualiza `state.host_last_seen` (para el watchdog)
  2. Auto-detecta IP del host si aún no la tenemos
  3. Emite el evento correspondiente en el bus
  4. Envía ACK al host en `udp_ack_port`

El ACK se envía siempre, incluso para heartbeats, para que el host pueda
medir RTT y detectar pérdida. La emergencia es el caso crítico: el host
reintenta 50 veces hasta recibir ACK, así que el ACK DEBE salir.
"""
# PY36: Eliminado `from __future__ import annotations`.
import asyncio
import logging
import socket

# PY36: Optional y Tuple de typing (el original usaba `X | None` y `tuple[str, int]`).
from typing import Optional, Tuple  # PY36: añadido

from config import CFG
from core.bus import Ev, bus
from core.state import state
from protocol.udp_frame import Frame, MsgType, build_ack, decode_motor

log = logging.getLogger(__name__)


class UdpCommandServer(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        # PY36: `asyncio.DatagramTransport | None` → `Optional[asyncio.DatagramTransport]`.
        self._transport = None  # type: Optional[asyncio.DatagramTransport]
        # PY36: `socket.socket | None` → `Optional[socket.socket]`.
        self._ack_sock = None  # type: Optional[socket.socket]

    # asyncio.DatagramProtocol API
    def connection_made(self, transport) -> None:
        self._transport = transport
        log.info(
            "UDP comandos escuchando en %s:%d",
            CFG.network.listen_host,
            CFG.network.udp_cmd_port,
        )
        # ACK socket independiente
        self._ack_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ack_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)

    # PY36: Firma original `addr: tuple[str, int]`. Reemplazado por Tuple[str, int].
    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        host_ip = addr[0]
        try:
            frame = Frame.unpack(data)
        except ValueError as exc:
            state.cmds_dropped += 1
            log.debug("frame inválido de %s: %s", host_ip, exc)
            return

        if CFG.network.host_ip and host_ip != CFG.network.host_ip:
            log.warning("ignorando comando de %s (host configurado: %s)", host_ip, CFG.network.host_ip)
            return
        if not state.host_ip:
            state.host_ip = host_ip
            log.info("Host detectado: %s", host_ip)

        state.touch_host(host_ip)
        state.cmds_received += 1

        # Dispatch por tipo
        if frame.msg_type == MsgType.CMD_MOTOR:
            log.debug("motor command received")
            try:
                left, right, aux = decode_motor(frame.payload)
            except Exception:
                return
            bus.emit(Ev.CMD_MOTOR, {"left": left, "right": right, "aux": aux, "seq": frame.seq})
        elif frame.msg_type == MsgType.CMD_HEARTBEAT:
            bus.emit(Ev.CMD_HEARTBEAT, {"seq": frame.seq})
        elif frame.msg_type == MsgType.CMD_EMERGENCY:
            state.emergency_active = True
            bus.emit(Ev.CMD_EMERGENCY, {"seq": frame.seq})
            bus.emit(Ev.STOP_MOTORS, None)
            log.warning("PARO DE EMERGENCIA recibido (seq=%d)", frame.seq)
        else:
            log.debug("tipo desconocido: 0x%02X", frame.msg_type)
            return

        # ACK
        self._send_ack(frame.seq, host_ip)

    def _send_ack(self, seq: int, host_ip: str) -> None:
        if not self._ack_sock:
            return
        try:
            pkt = build_ack(seq)
            self._ack_sock.sendto(pkt, (host_ip, CFG.network.udp_ack_port))
            state.acks_sent += 1
        except OSError as exc:
            log.error("sendto ACK falló: %s", exc)

    def error_received(self, exc: Exception) -> None:
        log.error("Error UDP: %s", exc)

    # PY36: Firma original `exc: Exception | None` → Optional[Exception].
    def connection_lost(self, exc: Optional[Exception]) -> None:
        log.info("UDP cerrado: %s", exc)
        if self._ack_sock:
            self._ack_sock.close()


# PY36: El original no recibía `loop`. Añadido como parámetro porque:
#       - En 3.6 no existe `asyncio.get_running_loop()`.
#       - En 3.6 `asyncio.get_event_loop()` funciona pero es ambiguo cuando
#         hay varios loops; es más claro que main.py pase el suyo.
async def run_udp_server(stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
    transport, _ = await loop.create_datagram_endpoint(
        UdpCommandServer,
        local_addr=(CFG.network.listen_host, CFG.network.udp_cmd_port),
    )
    try:
        await stop_event.wait()
    finally:
        transport.close()