"""Entry point del servicio Jetson.

Cambios:
  - Anadido task 'ros2-bridge' que arranca el nodo ROS 2.
  - Anadido flag CLI --no-ros para correr sin ROS (caso de la GUI sola).
"""
import argparse
import asyncio
import logging
import signal
import sys

from typing import List, Optional

from config import CFG
from core.heartbeat import run_host_watchdog
from hw.esp32_link import Esp32Link
from net.udp_server import run_udp_server
from net.video_pipeline import run_video_pipeline
from net.ws_server import run_ws_server
from ros2.ros2_bridge import run_ros2_bridge

log = logging.getLogger("jetson_service")


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jetson Nano teleoperation service")
    p.add_argument("--host", default=CFG.network.listen_host)
    p.add_argument("--host-ip", default="")
    p.add_argument("--serial", default=CFG.serial.port)
    p.add_argument("--baud", type=int, default=CFG.serial.baudrate)
    p.add_argument("--hb-timeout-ms", type=int, default=CFG.network.host_heartbeat_timeout_ms)
    p.add_argument("--no-ros", action="store_true",
                   help="No arrancar el bridge ROS 2 (modo solo dashboard manual)")
    p.add_argument("--ros-publish-tf", action="store_true",
                   help="Publicar TF odom->base_link desde el bridge (desactivar si usas EKF)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def apply_cli(args: argparse.Namespace) -> None:
    CFG.network.listen_host = args.host
    CFG.network.host_ip = args.host_ip
    CFG.network.host_heartbeat_timeout_ms = args.hb_timeout_ms
    CFG.serial.port = args.serial
    CFG.serial.baudrate = args.baud


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def amain(stop_event: asyncio.Event,
                loop: asyncio.AbstractEventLoop,
                args: argparse.Namespace) -> None:
    esp32 = Esp32Link(loop=loop)

    task_specs = [
        ("udp",           run_udp_server(stop_event, loop)),
        ("ws",            run_ws_server(stop_event)),
        ("video",         run_video_pipeline(stop_event, loop)),
        ("esp32",         esp32.run(stop_event)),
        ("host-watchdog", run_host_watchdog()),
    ]

    if not args.no_ros:
        task_specs.append(
            ("ros2-bridge", run_ros2_bridge(stop_event, loop,
                                             publish_odom_tf=args.ros_publish_tf))
        )

    tasks = [loop.create_task(coro) for _, coro in task_specs]
    task_names = {t: name for t, (name, _) in zip(tasks, task_specs)}

    log.info("Servicio Jetson arrancado. Tareas: %s", list(task_names.values()))

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for t in done:
        exc = t.exception()
        if exc:
            log.error("Tarea %s termino con excepcion: %s", task_names.get(t, "?"), exc)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    setup_logging(args.log_level)
    apply_cli(args)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_event = asyncio.Event(loop=loop)

    def _on_signal():
        log.info("Senal de parada recibida")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _on_signal())

    try:
        loop.run_until_complete(amain(stop_event, loop, args))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
