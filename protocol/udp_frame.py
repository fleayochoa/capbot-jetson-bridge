"""Frame UDP binario (espejo del dashboard).

Layout little-endian, 16 bytes fijos:
    [magic:2][version:1][type:1][seq:4][payload:6][crc16:2]

CRC16-CCITT (poly 0x1021, init 0xFFFF) sobre los primeros 14 bytes.

Este módulo debe mantenerse SINCRONIZADO con host_dashboard/protocol/udp_frame.py.
"""
# PY36: Eliminado `from __future__ import annotations`.
import struct
from dataclasses import dataclass
from enum import IntEnum

# PY36: `Tuple` desde typing en vez de `tuple[...]` (3.9+).
from typing import Tuple  # PY36: añadido

from config import CFG


class MsgType(IntEnum):
    CMD_MOTOR = 0x01
    CMD_HEARTBEAT = 0x02
    CMD_EMERGENCY = 0x03
    CMD_PID_PARAM = 0x04      # payload: ctrl_id(1) param_id(1) float32(4)
    CMD_SETPOINT_COMP = 0x05  # payload: comp_id(1) reserved(1) float32(4)
    CMD_MODE = 0x06           # payload: mode(1) reserved(5); 0=manual 1=autónomo
    ACK = 0x81


# Controller IDs para CMD_PID_PARAM
CTRL_LINEAR_POS = 0
CTRL_LINEAR_VEL = 1
CTRL_ANG_POS = 2
CTRL_ANG_VEL = 3

# Parameter IDs para CMD_PID_PARAM
PARAM_KP = 0
PARAM_KI = 1
PARAM_KD = 2

# Component IDs para CMD_SETPOINT_COMP (solo 0-2 se procesan; 3-4 son velocidades ignoradas)
SETPOINT_X_POS = 0
SETPOINT_Y_POS = 1
SETPOINT_ANG_POS = 2
SETPOINT_LIN_VEL = 3   # ignorado en Jetson
SETPOINT_ANG_VEL = 4   # ignorado en Jetson


def crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    msg_type: int
    seq: int
    payload: bytes  # exactamente 6 bytes

    def pack(self) -> bytes:
        if len(self.payload) != 6:
            raise ValueError("payload debe ser 6 bytes")
        header = struct.pack(
            "<HBBI6s",
            CFG.protocol.magic,
            CFG.protocol.version,
            self.msg_type & 0xFF,
            self.seq & 0xFFFFFFFF,
            self.payload,
        )
        crc = crc16_ccitt(header)
        return header + struct.pack("<H", crc)

    @classmethod
    def unpack(cls, data: bytes) -> "Frame":
        if len(data) != CFG.protocol.frame_size:
            raise ValueError("frame debe ser {} bytes".format(CFG.protocol.frame_size))
        magic, version, msg_type, seq, payload, crc = struct.unpack("<HBBI6sH", data)
        if magic != CFG.protocol.magic:
            raise ValueError("magic inválido")
        if version != CFG.protocol.version:
            raise ValueError("versión {} no soportada".format(version))
        if crc16_ccitt(data[:14]) != crc:
            raise ValueError("CRC inválido")
        return cls(msg_type=msg_type, seq=seq, payload=payload)


# PY36: Firma original `-> tuple[int, int, int]`. El genérico `tuple[...]` es 3.9+.
#       Usamos `Tuple[int, int, int]` de `typing`.
def decode_motor(payload: bytes) -> Tuple[int, int, int]:
    return struct.unpack("<hhh", payload)


def build_ack(seq: int) -> bytes:
    """ACK: payload[0..3] = seq reconocido, resto ceros."""
    payload = struct.pack("<I", seq) + b"\x00\x00"
    return Frame(MsgType.ACK, seq, payload).pack()


def decode_pid_param(payload: bytes) -> Tuple[int, int, float]:
    """Decodifica CMD_PID_PARAM. Retorna (ctrl_id, param_id, value)."""
    ctrl_id, param_id, value = struct.unpack("<BBf", payload[:6])
    return ctrl_id, param_id, value


def decode_setpoint_comp(payload: bytes) -> Tuple[int, float]:
    """Decodifica CMD_SETPOINT_COMP. Retorna (comp_id, value)."""
    comp_id, _, value = struct.unpack("<BBf", payload[:6])
    return comp_id, value


def decode_mode(payload: bytes) -> int:
    """Decodifica CMD_MODE. Retorna el modo (0=manual, 1=autónomo)."""
    (mode,) = struct.unpack("<B", payload[:1])
    return mode