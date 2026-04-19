"""Watchdog del heartbeat del PC host.

Requisito: si no hay respuesta/comando del host en M ms, detener motores.

Estrategia: mantenemos un timestamp `host_last_seen` que se refresca con CADA
datagrama UDP válido recibido del host (motor, heartbeat o emergencia).
Un bucle asyncio revisa periódicamente si el timestamp está más viejo que
el umbral y, en ese caso, emite `STOP_MOTORS` una única vez hasta que el host
vuelva.

De la misma forma podríamos vigilar el ESP32, pero por el diseño el ESP32 tiene
su propio watchdog contra la Jetson (requisito). Aquí sólo reportamos.
"""
from __future__ import annotations

import asyncio
import logging
import time

from config import CFG
from core.bus import Ev, bus
from core.state import state

log = logging.getLogger(__name__)

# Frecuencia a la que el watchdog chequea. ~20Hz es suficiente para cumplir
# timeouts del orden de cientos de ms.
_CHECK_INTERVAL_S = 0.05


async def run_host_watchdog() -> None:
    """Bucle infinito que vigila el heartbeat del host."""
    # Suscribimos auto-refresh al recibir cualquier comando
    bus.on(Ev.CMD_MOTOR, _touch_host)
    bus.on(Ev.CMD_HEARTBEAT, _touch_host)
    bus.on(Ev.CMD_EMERGENCY, _touch_host)

    timeout_s = CFG.network.host_heartbeat_timeout_ms / 1000.0
    was_online = False

    while True:
        try:
            now = time.time()
            elapsed = now - state.host_last_seen if state.host_last_seen else float("inf")
            online = elapsed < timeout_s

            if online and not was_online:
                bus.emit(Ev.HOST_ONLINE, state.host_ip)
                log.info("Host online: %s", state.host_ip)
            elif not online and was_online:
                # Transición a offline -> detener motores
                bus.emit(Ev.HOST_OFFLINE, None)
                bus.emit(Ev.STOP_MOTORS, None)
                log.warning("Host offline (sin comandos en %.0f ms), deteniendo motores", elapsed * 1000)

            was_online = online
            await asyncio.sleep(_CHECK_INTERVAL_S)
        except asyncio.CancelledError:
            log.info("Watchdog de host cancelado")
            raise
        except Exception:
            log.exception("Error en watchdog de host")
            await asyncio.sleep(0.5)


def _touch_host(data) -> None:
    """Refresca el timestamp del host cada vez que llega un comando."""
    # El UDP server ya actualiza state.host_last_seen; este callback es
    # redundante pero explícito (idempotente).
    state.host_last_seen = time.time()