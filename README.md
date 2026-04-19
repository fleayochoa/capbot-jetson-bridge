# Jetson Nano Service

Servicio Python que corre en la Jetson Nano. Hace de puente entre el PC host y el ESP32,
gestiona el video de la cámara IMX219 y coordina heartbeats/paros de emergencia.

```
                    UDP 5005 (comandos)            Serial COBS+CRC16
    ┌──────────┐ ──────────────────────▶ ┌────────┐ ────────────▶ ┌───────┐
    │ PC Host  │ ◀────────────────────── │ Jetson │ ◀──────────── │ ESP32 │
    │          │    UDP 5006 (ACKs)      │        │   telemetría  │       │
    │          │ ◀────────────────────── │        │               └───────┘
    │          │   WS 8765 (telemetría)  │        │
    │          │ ◀────────────────────── │        │
    │          │   UDP 5000 (video H264) │        │
    └──────────┘                         └────────┘
```

## Arquitectura

```
jetson_service/
├── main.py                 # Entry point (asyncio loop)
├── config.py               # Puertos, timeouts, NTP, serial
├── core/
│   ├── bus.py              # Bus de eventos asyncio
│   ├── state.py            # Estado compartido
│   └── heartbeat.py        # Watchdogs de red + serial
├── protocol/
│   ├── udp_frame.py        # Frame binario 16B (idéntico al host)
│   └── cobs_frame.py       # COBS + CRC16 para el serial del ESP32
├── net/
│   ├── udp_server.py       # Recibe comandos, envía ACKs
│   ├── ws_server.py        # Publica telemetría a 50Hz
│   └── video_pipeline.py   # GStreamer IMX219 → H264 HW → UDP
├── hw/
│   └── esp32_link.py       # Serial bidireccional con ESP32
└── scripts/
    ├── install.sh          # systemd + chrony/ntp
    └── jetson-service.service
```

## Requisitos runtime en la Jetson

- JetPack 4.6+ (con nvarguscamerasrc y nvv4l2h264enc)
- Python 3.8+
- `pip install pyserial websockets`
- Chrony o ntpd configurado apuntando al PC host

## Ejecución manual

```bash
python3 main.py --host 0.0.0.0 --serial /dev/ttyUSB0 --host-ip 192.168.1.10
```

## Como servicio systemd

```bash
sudo ./scripts/install.sh 192.168.1.10
sudo systemctl status jetson-service
```