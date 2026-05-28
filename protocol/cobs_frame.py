"""Framing COBS + CRC16 para el enlace serial con el ESP32.

Formato en la cola (del stream byte-serial):
    [ COBS-encoded( [type:1][len:1][payload:len][crc16:2] ) ][ 0x00 ]

Tipos de mensaje:
    0x10  MOTOR_CMD   payload = <hhh> (left, right, aux)          PWM crudo, teleop
    0x11  BRAKE_ON    payload vacio
    0x12  HEARTBEAT   payload vacio
    0x13  VEL_CMD     payload = <hh>  (v_mm_s, w_mrad_s)          Nav2 /cmd_vel
    0x20  TELEMETRY   payload = JSON UTF-8
    0x21  ESP_HELLO   payload vacio
"""
import struct
from dataclasses import dataclass
from enum import IntEnum

from typing import List

from protocol.udp_frame import crc16_ccitt


DELIMITER = 0x00


class SerMsgType(IntEnum):
    MOTOR_CMD = 0x10
    BRAKE_ON  = 0x11
    HEARTBEAT = 0x12
    VEL_CMD   = 0x13
    TELEMETRY = 0x20
    ESP_HELLO = 0x21


# ------------------------------------------------------------
# COBS
# ------------------------------------------------------------
def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx = 0
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        code = data[i]
        if code == 0:
            raise ValueError("cero inesperado en stream COBS")
        end = i + code
        if end > n:
            raise ValueError("codigo COBS se pasa del final")
        out.extend(data[i + 1:end])
        i = end
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)


# ------------------------------------------------------------
# Frame serial
# ------------------------------------------------------------
@dataclass
class SerFrame:
    msg_type: int
    payload: bytes

    def pack(self) -> bytes:
        if len(self.payload) > 255:
            raise ValueError("payload max 255 bytes")
        raw = struct.pack("<BB", self.msg_type & 0xFF, len(self.payload)) + self.payload
        crc = crc16_ccitt(raw)
        raw += struct.pack("<H", crc)
        return cobs_encode(raw) + bytes([DELIMITER])

    @classmethod
    def unpack(cls, encoded: bytes) -> "SerFrame":
        raw = cobs_decode(encoded)
        if len(raw) < 4:
            raise ValueError("frame truncado")
        msg_type, length = raw[0], raw[1]
        if len(raw) != 2 + length + 2:
            raise ValueError("longitud inconsistente: decl={}, real={}".format(
                length, len(raw) - 4))
        payload = raw[2:2 + length]
        (crc_recv,) = struct.unpack("<H", raw[2 + length:])
        expected = crc16_ccitt(raw[:2 + length])
        if crc_recv != expected:
            raise ValueError("CRC serial invalido")
        return cls(msg_type=msg_type, payload=payload)


class SerialFrameBuffer:
    def __init__(self, max_frame_bytes: int = 512):
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, data: bytes) -> List[SerFrame]:
        frames = []  # type: List[SerFrame]
        for b in data:
            if b == DELIMITER:
                if self._buf:
                    try:
                        frames.append(SerFrame.unpack(bytes(self._buf)))
                    except ValueError:
                        pass
                    self._buf.clear()
            else:
                self._buf.append(b)
                if len(self._buf) > self._max:
                    self._buf.clear()
        return frames


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def build_motor(left: int, right: int, aux: int = 0) -> bytes:
    payload = struct.pack("<hhh", left, right, aux)
    return SerFrame(SerMsgType.MOTOR_CMD, payload).pack()


def build_vel(v_mms: int, w_mrad_s: int) -> bytes:
    """Velocidad lineal en mm/s y angular en mrad/s. Saturado a int16."""
    v_mms    = max(-32768, min(32767, int(v_mms)))
    w_mrad_s = max(-32768, min(32767, int(w_mrad_s)))
    payload = struct.pack("<hh", v_mms, w_mrad_s)
    return SerFrame(SerMsgType.VEL_CMD, payload).pack()


def build_brake() -> bytes:
    return SerFrame(SerMsgType.BRAKE_ON, b"").pack()


def build_heartbeat() -> bytes:
    return SerFrame(SerMsgType.HEARTBEAT, b"").pack()
