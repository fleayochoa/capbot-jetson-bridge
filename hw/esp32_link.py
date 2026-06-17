"""Enlace serial con el ESP32 usando COBS+CRC16.

Responsabilidades:
  1. Abrir/reabrir el puerto serial (reconexión automática).
  2. Leer bytes, alimentar el SerialFrameBuffer, decodificar frames y
     emitirlos al bus (p.ej. telemetría).
  3. Consumir del bus eventos de comando (motor, stop, emergencia) y
     escribirlos al puerto.
  4. Mandar heartbeat propio al ESP32 cada N ms para que el ESP32 pueda
     ejercer SU watchdog (si no ve nuestro heartbeat en ME ms, freno).

El I/O serial se ejecuta en un executor para no bloquear el loop asyncio
(pyserial es síncrono). La escritura se serializa con una cola.
"""
# PY36: Eliminado `from __future__ import annotations`.
import asyncio
import logging
import time
import struct

# PY36: Optional en vez de `X | None` (3.10+).
from typing import Optional  # PY36: añadido

try:
    import serial
except ImportError:  # pragma: no cover
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
    build_pid_param,
    build_setpoint_comp,
    build_mode_cmd,
)

log = logging.getLogger(__name__)

# Heartbeat al ESP32: más frecuente que su watchdog para no despertar paro.
# Si el ESP32 usa ME=200ms, mandamos cada 50ms.
_HEARTBEAT_INTERVAL_S = 0.05
_TELEM_FMT  = "<BBhh" + "f" * 12 + "HH"
_TELEM_SIZE = struct.calcsize(_TELEM_FMT)  # 58

class Esp32Link:
    # PY36: El constructor ahora recibe el loop explícitamente. Motivo:
    #       - En el original, `asyncio.Queue()` se construía en `__init__`
    #         sin loop. En 3.6 eso intenta capturar `get_event_loop()` y
    #         si no hay uno creado todavía (o está asociado a otro hilo)
    #         se dispara RuntimeError.
    #       - En 3.10+ Queue ya no necesita loop; en 3.8 se deprecó el
    #         parámetro; en 3.6 todavía es la forma recomendada.
    def __init__(self, loop=None):  # PY36: añadido loop
        self._loop = loop or asyncio.get_event_loop()  # PY36: añadido
        # PY36: Anotación `serial.Serial | None` → `Optional[serial.Serial]`.
        self._ser = None  # type: Optional["serial.Serial"]
        # PY36: Anotación `asyncio.Queue[bytes]` no se puede escribir en 3.6
        #       (la Queue no es subscriptible antes de 3.9). Creamos sin
        #       anotación genérica y pasamos `loop=` explícito.
        self._tx_queue = asyncio.Queue(loop=self._loop)  # PY36: añadido loop=
        self._buffer = SerialFrameBuffer()
        self._running = False

    async def run(self, stop_event: asyncio.Event) -> None:
        if serial is None:
            log.error("pyserial no instalado; enlace ESP32 deshabilitado")
            await stop_event.wait()
            return

        self._running = True
        self._subscribe_bus()

        # PY36: El original hacía `loop = asyncio.get_running_loop()` (3.7+).
        #       Usamos el loop guardado en __init__ (que es el actual cuando
        #       arranca el servicio).
        loop = self._loop

        # PY36: `asyncio.create_task` no existe en 3.6. Usamos
        #       `loop.create_task`, disponible desde 3.4.2.
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

    # --------------------------------------------------------
    # Suscripciones a eventos
    # --------------------------------------------------------
    def _subscribe_bus(self) -> None:
        bus.on(Ev.CMD_MOTOR, self._on_motor_cmd)
        bus.on(Ev.STOP_MOTORS, self._on_stop_motors)
        bus.on(Ev.CMD_PID_PARAM, self._on_pid_param)
        bus.on(Ev.CMD_SETPOINT, self._on_setpoint)
        bus.on(Ev.CMD_MODE, self._on_mode)

    def _on_motor_cmd(self, data: dict) -> None:
        # Si hay emergencia activa o host offline, ignorar comandos de motor.
        if state.emergency_active:
            return
        self._tx_queue.put_nowait(build_motor(data["left"], data["right"], data["aux"]))

    def _on_stop_motors(self, _data) -> None:
        try:
            self._tx_queue.put_nowait(build_brake())
        except asyncio.QueueFull:
            pass

    def _on_pid_param(self, data: dict) -> None:
        try:
            pkt = build_pid_param(data["ctrl_id"], data["param_id"], data["value"])
            self._tx_queue.put_nowait(pkt)
        except (KeyError, asyncio.QueueFull):
            pass

    def _on_setpoint(self, data: dict) -> None:
        try:
            pkt = build_setpoint_comp(data["comp_id"], data["value"])
            self._tx_queue.put_nowait(pkt)
        except (KeyError, asyncio.QueueFull):
            pass

    def _on_mode(self, data: dict) -> None:
        try:
            pkt = build_mode_cmd(data["mode"])
            self._tx_queue.put_nowait(pkt)
        except (KeyError, asyncio.QueueFull):
            pass

    # --------------------------------------------------------
    # Puerto
    # --------------------------------------------------------
    def _open_port(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=CFG.serial.port,
                baudrate=CFG.serial.baudrate,
                timeout=0.05,         # polling no bloqueante largo
                write_timeout=0.2,
                rtscts=False,
                dsrdtr=False,
            )
            # Drenar buffers de arranque (bootloader del ESP32 escupe texto)
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

    # --------------------------------------------------------
    # Lectura
    # --------------------------------------------------------
    async def _reader_loop(self) -> None:
        # PY36: El original volvía a llamar a `asyncio.get_running_loop()`
        #       aquí. Reutilizamos self._loop para consistencia.
        loop = self._loop
        while self._running:
            if self._ser is None or not self._ser.is_open:
                if not self._open_port():
                    await asyncio.sleep(1.0)
                    continue
            try:
                # Blocking read en executor para no trancar el loop
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
                pass  # opcional, ya actualizamos touch_esp32
            else:
                log.debug("frame serial desconocido: 0x%02X", fr.msg_type)
        except Exception:
            log.exception("Error procesando frame serial")

    def _handle_telemetry(self, payload: bytes) -> None:
        """Telemetría binaria de tamaño fijo (ver buildPayload en el ESP32)."""
        if len(payload) != _TELEM_SIZE:
            return
        (schema, mode, pwm_l, pwm_r,
        ox, oy, oa, ov, ow,
        spx, spy, spa, spv, spw,
        epos, eang, t1, t2) = struct.unpack(_TELEM_FMT, payload)
        if schema != 1:
            return  # esquema desconocido -> descartar
        data = {
            "mode": "auto" if mode else "manual",
            "u":   {"pwm_left": pwm_l, "pwm_right": pwm_r},
            "odo": {"x": ox, "y": oy, "a": oa, "v": ov, "w": ow},
            "sp":  {"x": spx, "y": spy, "a": spa, "v": spv, "w": spw},
            "error": {"pos": epos, "ang": eang},
            "tof": {"t1": t1, "t2": t2},
        }
        bus.emit(Ev.TELEMETRY, data)

    # --------------------------------------------------------
    # Escritura
    # --------------------------------------------------------
    async def _writer_loop(self) -> None:
        loop = self._loop  # PY36: self._loop en vez de get_running_loop()
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

    # --------------------------------------------------------
    # Heartbeat al ESP32
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # Watchdog: si el ESP32 no manda nada en X, marcarlo offline.
    # --------------------------------------------------------
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