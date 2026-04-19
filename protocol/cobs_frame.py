"""Framing COBS + CRC16 para el enlace serial con el ESP32.

Formato en la cola (del stream byte-serial):

    [ COBS-encoded( [type:1][len:1][payload:len][crc16:2] ) ][ 0x00 ]

- El 0x00 final es el delimitador de frame (tras COBS no puede haber ceros
  dentro del payload encoded, así que el delimitador es unívoco).
- Dentro del payload bruto (antes de COBS) se envía: tipo, longitud, payload,
  CRC16 del tipo+len+payload.

Tipos de mensaje propuestos (ajustar con firmware del ESP32):
    0x10  MOTOR_CMD   payload = <hhh> (left, right, aux)
    0x11  BRAKE_ON    payload vacío (freno activo)
    0x12  HEARTBEAT   payload vacío
    0x20  TELEMETRY   payload = JSON UTF-8 o struct binario (a definir)
    0x21  ESP_HELLO   payload vacío (el ESP32 saluda al arrancar)

Este módulo sólo provee encode/decode genéricos; el mapeo de tipos vive en
hw/esp32_link.py.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from protocol.udp_frame import crc16_ccitt  # reutilizamos la misma CRC


DELIMITER = 0x00


class SerMsgType(IntEnum):
    MOTOR_CMD = 0x10
    BRAKE_ON = 0x11
    HEARTBEAT = 0x12
    TELEMETRY = 0x20
    ESP_HELLO = 0x21


# ------------------------------------------------------------
# COBS (Consistent Overhead Byte Stuffing)
# ------------------------------------------------------------
# Implementación canónica single-pass:
#   - Recorremos data una sola vez, acumulando en `out` un "code byte"
#     seguido de hasta 254 bytes no-cero.
#   - Cuando encontramos un 0 en `data`, cerramos el bloque actual
#     escribiendo el code y abrimos uno nuevo.
#   - Cuando el bloque llega a 254 bytes sin encontrar cero, cerramos con
#     code=0xFF (marcador "sin cero al final") y abrimos nuevo bloque SIN
#     consumir nada. El decoder distingue 0xFF porque no reinserta cero.
# ------------------------------------------------------------
def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])       # placeholder del primer code
    code_idx = 0
    code = 1                   # cuenta el code + bytes no-cero incluidos
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
            raise ValueError("código COBS se pasa del final")
        out.extend(data[i + 1:end])
        i = end
        # Un cero implícito se reinserta entre bloques SALVO que el bloque
        # haya llegado a 254 bytes (code==0xFF) o estemos al final.
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
        """encoded NO incluye el delimitador final."""
        raw = cobs_decode(encoded)
        if len(raw) < 4:
            raise ValueError("frame truncado")
        msg_type, length = raw[0], raw[1]
        if len(raw) != 2 + length + 2:
            raise ValueError(f"longitud inconsistente: decl={length}, real={len(raw) - 4}")
        payload = raw[2:2 + length]
        (crc_recv,) = struct.unpack("<H", raw[2 + length:])
        expected = crc16_ccitt(raw[:2 + length])
        if crc_recv != expected:
            raise ValueError("CRC serial inválido")
        return cls(msg_type=msg_type, payload=payload)


# ------------------------------------------------------------
# Stream parser (ESP32 manda bytes, aquí los acumulamos)
# ------------------------------------------------------------
class SerialFrameBuffer:
    """Acumula bytes y entrega frames completos cuando ve 0x00."""

    def __init__(self, max_frame_bytes: int = 512):
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, data: bytes) -> list[SerFrame]:
        frames: list[SerFrame] = []
        for b in data:
            if b == DELIMITER:
                if self._buf:
                    try:
                        frames.append(SerFrame.unpack(bytes(self._buf)))
                    except ValueError:
                        # Frame corrupto: descartar silenciosamente
                        pass
                    self._buf.clear()
            else:
                self._buf.append(b)
                if len(self._buf) > self._max:
                    # Desincronizados: purgar hasta próximo delimitador
                    self._buf.clear()
        return frames


# ------------------------------------------------------------
# Helpers de alto nivel
# ------------------------------------------------------------
def build_motor(left: int, right: int, aux: int = 0) -> bytes:
    payload = struct.pack("<hhh", left, right, aux)
    return SerFrame(SerMsgType.MOTOR_CMD, payload).pack()


def build_brake() -> bytes:
    return SerFrame(SerMsgType.BRAKE_ON, b"").pack()


def build_heartbeat() -> bytes:
    return SerFrame(SerMsgType.HEARTBEAT, b"").pack()