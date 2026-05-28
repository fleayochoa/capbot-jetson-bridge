"""Enlace serial con el ESP32 usando COBS+CRC16.

Cambios vs version original:
  - Nuevo handler on_vel_cmd: traduce Ev.CMD_VEL del bus a VEL_CMD por serial.
  - Permite que el bridge ROS y el dashboard manual coexistan: si llegan
    VEL_CMD frescos al ESP32, este tiene prioridad sobre MOTOR_CMD (logica
    en firmware).
"""
import asyncio
import logging
import time

from typing import Optional

try:
    import serial
except ImportError:
    serial = None

from config import CFG
from core.bus import Ev, bus
from core.state import state
from protocol.cobs_frame import (
    SerMsgType,
    SerialFrameBuffer,
    build_brake,
    build_heartbeat,
    build_motor,
    build_vel,
)

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_S = 0.05


class Esp32Link:
    def __init__(self, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._ser = None  # type: Optional["serial.Serial"]
        self._tx_queue = asyncio.Queue(loop=self._loop)
        self._buffer = SerialFrameBuffer()
        self._running = False

    async def run(self, stop_event: asyncio.Event) -> None:
        if serial is None:
            log.error("pyserial no instalado; enlace ESP32 deshabilitado")
            await stop_event.wait()
            return

        self._running = True
        self._subscribe_bus()

        loop = self._loop
        reader_task = loop.create_task(self._reader_loop())
        writer_task = loop.create_task(self._writer_loop())
        hb_task = loop.create_task(self._heartbeat_loop())
        watchdog_task = loop.create_task(self._esp32_watchdog())

        try:
            await stop_event.wait()
        finally:
            self._running = False
            for t in (reader_task, writer_task, hb_task, watchdog_task):
                t.cancel()
            await asyncio.gather(reader_task, writer_task, hb_task, watchdog_task,
                                 return_exceptions=True)
            self._close_port()

    def _subscribe_bus(self) -> None:
        bus.on(Ev.CMD_MOTOR,  self._on_motor_cmd)
        bus.on(Ev.CMD_VEL,    self._on_vel_cmd)
        bus.on(Ev.STOP_MOTORS, self._on_stop_motors)

    def _on_motor_cmd(self, data: dict) -> None:
        if state.emergency_active:
            return
        try:
            self._tx_queue.put_nowait(build_motor(data["left"], data["right"], data["aux"]))
        except asyncio.QueueFull:
            pass

    def _on_vel_cmd(self, data: dict) -> None:
        if state.emergency_active:
            return
        try:
            self._tx_queue.put_nowait(build_vel(data["v_mms"], data["w_mrads"]))
        except asyncio.QueueFull:
            pass

    def _on_stop_motors(self, _data) -> None:
        try:
            self._tx_queue.put_nowait(build_brake())
        except asyncio.QueueFull:
            pass

    # ---- Puerto, lectura, escritura, watchdog: iguales que en el original ----
    def _open_port(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=CFG.serial.port,
                baudrate=CFG.serial.baudrate,
                timeout=0.05,
                write_timeout=0.2,
                rtscts=False,
                dsrdtr=False,
            )
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            state.touch_esp32()
            bus.emit(Ev.ESP32_ONLINE, None)
            log.info("Serial ESP32 abierto: %s @ %d", CFG.serial.port, CFG.serial.baudrate)
            return True
        except (OSError, serial.SerialException) as exc:
            log.warning("No se pudo abrir serial %s: %s", CFG.serial.port, exc)
            return False

    def _close_port(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if state.esp32_connected:
            state.esp32_connected = False
            bus.emit(Ev.ESP32_OFFLINE, "port closed")

    async def _reader_loop(self) -> None:
        loop = self._loop
        while self._running:
            if self._ser is None or not self._ser.is_open:
                if not self._open_port():
                    await asyncio.sleep(1.0)
                    continue
            try:
                data = await loop.run_in_executor(None, self._read_available)
                if data:
                    state.touch_esp32()
                    frames = self._buffer.feed(data)
                    for fr in frames:
                        self._dispatch_frame(fr)
            except (OSError, serial.SerialException) as exc:
                log.warning("Error leyendo serial: %s", exc)
                self._close_port()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

    def _read_available(self) -> bytes:
        ser = self._ser
        if ser is None:
            return b""
        waiting = ser.in_waiting
        if waiting > 0:
            return ser.read(waiting)
        return ser.read(1)

    def _dispatch_frame(self, fr) -> None:
        try:
            if fr.msg_type == SerMsgType.TELEMETRY:
                self._handle_telemetry(fr.payload)
            elif fr.msg_type == SerMsgType.ESP_HELLO:
                log.info("ESP32 HELLO recibido")
            elif fr.msg_type == SerMsgType.HEARTBEAT:
                pass
            else:
                log.debug("frame serial desconocido: 0x%02X", fr.msg_type)
        except Exception:
            log.exception("Error procesando frame serial")

    def _handle_telemetry(self, payload: bytes) -> None:
        try:
            import json
            data = json.loads(payload.decode("utf-8"))
            if not isinstance(data, dict):
                return
        except (UnicodeDecodeError, ValueError):
            return
        bus.emit(Ev.TELEMETRY, data)

    async def _writer_loop(self) -> None:
        loop = self._loop
        while self._running:
            try:
                pkt = await asyncio.wait_for(self._tx_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            if self._ser is None or not self._ser.is_open:
                continue
            try:
                await loop.run_in_executor(None, self._ser.write, pkt)
            except (OSError, serial.SerialException) as exc:
                log.warning("Error escribiendo serial: %s", exc)
                self._close_port()

    async def _heartbeat_loop(self) -> None:
        hb = build_heartbeat()
        while self._running:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                if self._ser and self._ser.is_open:
                    try:
                        self._tx_queue.put_nowait(hb)
                    except asyncio.QueueFull:
                        pass
            except asyncio.CancelledError:
                return

    async def _esp32_watchdog(self) -> None:
        timeout_s = CFG.serial.rx_timeout_ms / 1000.0
        while self._running:
            try:
                await asyncio.sleep(0.1)
                if not state.esp32_connected:
                    continue
                if time.time() - state.esp32_last_seen > timeout_s:
                    state.esp32_connected = False
                    bus.emit(Ev.ESP32_OFFLINE, "rx timeout")
                    log.warning("ESP32 offline: sin datos en %.0f ms", timeout_s * 1000)
            except asyncio.CancelledError:
                return
