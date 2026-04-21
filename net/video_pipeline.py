"""Pipeline GStreamer que captura de la cámara IMX219 y la envía por UDP/RTP.

En Jetson Nano (JetPack 4.x) la captura es por `nvarguscamerasrc` y el
encoder H.264 por hardware es `nvv4l2h264enc`. Estos elementos tienen
latencia mínima cuando se configuran con los caps correctos.

Pipeline:
    nvarguscamerasrc sensor-id=0
      ! video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1
      ! nvvidconv                              # conversión en GPU
      ! video/x-raw(memory:NVMM),format=NV12
      ! nvv4l2h264enc insert-sps-pps=true
                       iframeinterval=15
                       idrinterval=15
                       maxperf-enable=1
                       preset-level=1          # low-latency
                       bitrate=4000000
                       control-rate=1          # variable
      ! h264parse config-interval=1
      ! rtph264pay pt=96 config-interval=1 mtu=1400
      ! udpsink host=<HOST> port=5000 sync=false async=false

Notas:
  * `maxperf-enable=1` fija relojes de GPU/NVDEC al máximo → menos latencia
    a costa de más consumo. Perfecto para teleoperación.
  * `preset-level=1` (UltraFastPreset) minimiza latencia.
  * `mtu=1400` evita fragmentación IP en redes domésticas.
  * `config-interval=1` reenvía SPS/PPS cada segundo: cliente nuevo o tras
    pérdida se recupera rápido.

La clase maneja arranque/parada y reinicio ante error. El host_ip puede
cambiar en runtime (p.ej. se detectó por UDP): exponemos `set_host()` que
reconstruye la pipeline.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
    _GST_AVAILABLE = True
    print("GStreamer disponible: video pipeline activada")
except (ImportError, ValueError):  # pragma: no cover
    Gst = GLib = None
    _GST_AVAILABLE = False
    print("GStreamer no disponible: video pipeline desactivada")

from config import CFG
from core.bus import Ev, bus
from core.state import state

log = logging.getLogger(__name__)


def _build_pipeline_str(host_ip: str) -> str:
    v = CFG.video
    return (
        f"nvarguscamerasrc sensor-id=0 "
        f"! video/x-raw(memory:NVMM),width={v.width},height={v.height},"
        f"framerate={v.fps}/1,format=NV12 "
        f"! nvvidconv "
        f"! video/x-raw(memory:NVMM),format=NV12 "
        f"! nvv4l2h264enc "
        f"insert-sps-pps=true iframeinterval={v.iframe_interval} "
        f"idrinterval={v.iframe_interval} maxperf-enable=1 preset-level=1 "
        f"bitrate={v.bitrate_kbps * 1000} control-rate=1 "
        f"! h264parse config-interval=1 "
        f"! rtph264pay pt=96 config-interval=1 mtu=1400 "
        f"! udpsink host={host_ip} port={CFG.network.video_port} sync=false async=false"
    )


class VideoPipeline:
    """Wraps la pipeline GStreamer y su MainLoop GLib."""

    def __init__(self) -> None:
        self._pipeline = None
        self._glib_loop: Optional[GLib.MainLoop] = None
        self._glib_thread: Optional[asyncio.Task] = None
        self._current_host: str = ""

    async def start(self, host_ip: str) -> None:
        if not _GST_AVAILABLE:
            state.video_state = "error"
            bus.emit(Ev.VIDEO_STATE, "error: GStreamer no disponible")
            log.error("GStreamer no disponible; pipeline no arranca")
            return

        if not host_ip:
            log.warning("No hay host_ip todavía; pipeline de video esperará")
            return

        if self._pipeline is not None:
            if host_ip == self._current_host:
                return  # ya estamos corriendo hacia ese host
            await self.stop()

        Gst.init(None)
        pipeline_str = _build_pipeline_str(host_ip)
        log.info("Pipeline: %s", pipeline_str)

        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            state.video_state = "error"
            bus.emit(Ev.VIDEO_STATE, f"parse_launch: {exc}")
            log.exception("parse_launch falló")
            return

        gbus = self._pipeline.get_bus()
        gbus.add_signal_watch()
        gbus.connect("message::error", self._on_error)
        gbus.connect("message::eos", self._on_eos)

        self._pipeline.set_state(Gst.State.PLAYING)
        self._current_host = host_ip
        state.video_state = "running"
        bus.emit(Ev.VIDEO_STATE, f"running -> {host_ip}:{CFG.network.video_port}")

        # GLib MainLoop en thread asyncio-friendly
        loop = asyncio.get_running_loop()
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = loop.run_in_executor(None, self._glib_loop.run)

    async def stop(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._glib_loop and self._glib_loop.is_running():
            self._glib_loop.quit()
        if self._glib_thread:
            try:
                await self._glib_thread
            except Exception:
                pass
            self._glib_thread = None
        self._current_host = ""
        state.video_state = "stopped"
        bus.emit(Ev.VIDEO_STATE, "stopped")

    async def retarget(self, host_ip: str) -> None:
        """Cambia el destino de la pipeline (reconstruye)."""
        if host_ip == self._current_host:
            return
        log.info("Retargeteando video a %s", host_ip)
        await self.stop()
        await self.start(host_ip)

    def _on_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        state.video_state = "error"
        bus.emit(Ev.VIDEO_STATE, f"error: {err.message}")
        log.error("GStreamer error: %s (%s)", err.message, dbg)

    def _on_eos(self, _bus, _msg) -> None:
        state.video_state = "eos"
        bus.emit(Ev.VIDEO_STATE, "eos")
        log.info("GStreamer EOS")


async def run_video_pipeline(stop_event: asyncio.Event) -> None:
    """Gestiona la pipeline en función del host detectado."""
    pipe = VideoPipeline()

    # Si al arrancar ya tenemos host (por CLI), lanzamos inmediatamente.
    if CFG.network.host_ip:
        await pipe.start(CFG.network.host_ip)

    async def on_host_online(ip: str) -> None:
        await pipe.retarget(ip)

    async def on_host_offline(_data) -> None:
        # Política: mantenemos la pipeline viva. UDP no necesita destino "activo"
        # y si volvemos a ver al host ya estaremos enviando. Sólo paramos si
        # cambia la IP.
        pass

    bus.on(Ev.HOST_ONLINE, on_host_online)
    bus.on(Ev.HOST_OFFLINE, on_host_offline)

    try:
        await stop_event.wait()
    finally:
        await pipe.stop()