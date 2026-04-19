"""Estado compartido del servicio."""
from dataclasses import dataclass, field
import time


@dataclass
class ServiceState:
    host_ip: str = ""
    host_last_seen: float = 0.0
    esp32_connected: bool = False
    esp32_last_seen: float = 0.0
    video_state: str = "stopped"
    emergency_active: bool = False

    # Contadores para diagnóstico
    cmds_received: int = 0
    cmds_dropped: int = 0   # frames corruptos o versión mala
    acks_sent: int = 0
    telemetry_published: int = 0

    def touch_host(self, ip: str) -> None:
        self.host_ip = ip
        self.host_last_seen = time.time()

    def touch_esp32(self) -> None:
        self.esp32_connected = True
        self.esp32_last_seen = time.time()


state = ServiceState()