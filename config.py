"""Configuración centralizada del servicio Jetson.

Todos los valores pueden sobrescribirse desde CLI (ver main.py).
"""
from dataclasses import dataclass, field


@dataclass
class NetworkConfig:
    # Escucha de comandos
    listen_host: str = "0.0.0.0"
    udp_cmd_port: int = 5005        # host -> jetson
    udp_ack_port: int = 5006        # jetson -> host
    ws_telemetry_port: int = 8765   # jetson -> host (WS)
    video_port: int = 5000          # jetson -> host (UDP RTP H264)

    # IP del host. Se setea desde CLI o por detección automática
    # (el primer comando UDP recibido fija la IP).
    host_ip: str = ""

    # Heartbeat: si no llega nada del host en este tiempo, detener motores.
    # M ms en el requisito -> 500ms es un valor conservador por defecto.
    host_heartbeat_timeout_ms: int = 500


@dataclass
class ProtocolConfig:
    magic: int = 0xABCD
    version: int = 1
    frame_size: int = 16


@dataclass
class SerialConfig:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 921600
    # Si la Jetson no recibe nada del ESP32 en este tiempo,
    # consideramos el link caído (no detiene motores directamente;
    # eso lo hace el ESP32 por su propio watchdog).
    rx_timeout_ms: int = 300


@dataclass
class TelemetryConfig:
    publish_hz: int = 50
    # Si la cola WS supera este tamaño asumimos que un cliente está atascado
    # y soltamos paquetes antiguos para no crecer sin límite.
    ws_queue_max: int = 100


@dataclass
class VideoConfig:
    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_kbps: int = 4000  # H264 hardware. Bajar para menos latencia.
    # Iframe frecuente ayuda a recuperar tras pérdidas de paquetes
    iframe_interval: int = 15


@dataclass
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    video: VideoConfig = field(default_factory=VideoConfig)


# Singleton mutable — se sobrescribe desde main.py tras parsear argv
CFG = Config()