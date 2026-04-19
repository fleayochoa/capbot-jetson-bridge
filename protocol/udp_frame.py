"""Frame UDP binario (espejo del dashboard).

Layout little-endian, 16 bytes fijos:
    [magic:2][version:1][type:1][seq:4][payload:6][crc16:2]

CRC16-CCITT (poly 0x1021, init 0xFFFF) sobre los primeros 14 bytes.

Este módulo debe mantenerse SINCRONIZADO con host_dashboard/protocol/udp_frame.py.
Cualquier cambio en el wire format debe tocar ambos archivos a la vez.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from config import CFG


class MsgType(IntEnum):
    CMD_MOTOR = 0x01
    CMD_HEARTBEAT = 0x02
    CMD_EMERGENCY = 0x03
    ACK = 0x81


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
            raise ValueError(f"payload debe ser 6 bytes")
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
            raise ValueError(f"frame debe ser {CFG.protocol.frame_size} bytes")
        magic, version, msg_type, seq, payload, crc = struct.unpack("<HBBI6sH", data)
        if magic != CFG.protocol.magic:
            raise ValueError(f"magic inválido")
        if version != CFG.protocol.version:
            raise ValueError(f"versión {version} no soportada")
        if crc16_ccitt(data[:14]) != crc:
            raise ValueError("CRC inválido")
        return cls(msg_type=msg_type, seq=seq, payload=payload)


def decode_motor(payload: bytes) -> tuple[int, int, int]:
    return struct.unpack("<hhh", payload)


def build_ack(seq: int) -> bytes:
    """ACK: payload[0..3] = seq reconocido, resto ceros."""
    payload = struct.pack("<I", seq) + b"\x00\x00"
    return Frame(MsgType.ACK, seq, payload).pack()