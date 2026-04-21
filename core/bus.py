"""Bus de eventos asyncio.

Equivalente al SignalBus de Qt del dashboard, pero basado en asyncio.
Cada evento es un nombre + payload. Los productores hacen `bus.emit(event, data)`
y los consumidores se suscriben con `bus.on(event, callback)`.

Esto desacopla:
  - El UDP server del ESP32 link (el primero empuja comandos al bus, el segundo
    los consume sin conocer UDP).
  - El ESP32 link del WS server (el primero publica telemetría al bus, el segundo
    la re-emite a los clientes WS sin tocar el serial).
  - El heartbeat de los motores (el watchdog emite un evento, el link ESP32 lo
    consume y envía STOP).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

Callback = Callable[[Any], Optional[Awaitable[None]]]


class EventBus:
    """Bus pub/sub ligero sobre asyncio."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callback]] = defaultdict(list)

    def on(self, event: str, cb: Callback) -> None:
        self._subs[event].append(cb)

    def off(self, event: str, cb: Callback) -> None:
        if cb in self._subs[event]:
            self._subs[event].remove(cb)

    def emit(self, event: str, data: Any = None) -> None:
        """Emite un evento sin esperar a los consumidores.

        Los callbacks asincrónicos se lanzan como tareas; los sincrónicos
        se ejecutan en línea.
        """
        for cb in list(self._subs.get(event, ())):
            try:
                result = cb(data)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                log.exception("Error en callback de evento %s", event)


# Singleton
bus = EventBus()


# ------------------------------------------------------------
# Nombres de eventos (para evitar strings mágicos dispersos)
# ------------------------------------------------------------
class Ev:
    # Desde UDP server (host -> jetson)
    CMD_MOTOR = "cmd.motor"              # dict {left, right, aux, seq}
    CMD_HEARTBEAT = "cmd.heartbeat"      # dict {seq}
    CMD_EMERGENCY = "cmd.emergency"      # dict {seq}

    # Estados de conexión / watchdog
    HOST_ONLINE = "host.online"          # str (ip)
    HOST_OFFLINE = "host.offline"        # None
    ESP32_ONLINE = "esp32.online"        # None
    ESP32_OFFLINE = "esp32.offline"      # str (reason)

    # Desde ESP32 link (esp32 -> jetson)
    TELEMETRY = "telemetry"              # dict (ya decodificado)

    # Orden interna: detener motores ya (por watchdog o emergencia)
    STOP_MOTORS = "motors.stop"          # None

    # Video
    VIDEO_STATE = "video.state"          # str
