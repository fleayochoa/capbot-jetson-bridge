#!/usr/bin/env bash
# install.sh HOST_IP
#
# Instala el servicio Jetson:
#   1. Copia código a /opt/jetson_service
#   2. Instala dependencias Python
#   3. Configura chrony apuntando al PC host como servidor NTP
#   4. Instala y habilita el systemd unit
#
# Uso:
#   sudo ./install.sh 192.168.1.10

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Ejecutar como root (sudo)" >&2
    exit 1
fi

if [[ $# -lt 1 ]]; then
    echo "Uso: $0 <HOST_IP>" >&2
    exit 1
fi

HOST_IP="$1"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="/opt/jetson_service"
SERVICE_USER="${SERVICE_USER:-jetson}"

echo "=== 1/4 Copiando código a $DST_DIR ==="
mkdir -p "$DST_DIR"
rsync -a --delete --exclude='__pycache__' --exclude='scripts/install.sh' \
    "$SRC_DIR/" "$DST_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DST_DIR"

echo "=== 2/4 Instalando dependencias Python ==="
apt-get update
apt-get install -y python3-pip python3-gi gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad python3-serial chrony
pip3 install --upgrade websockets

echo "=== 3/4 Configurando NTP (chrony) apuntando a $HOST_IP ==="
CHRONY_CONF="/etc/chrony/chrony.conf"
if [[ -f "$CHRONY_CONF" ]]; then
    # Eliminar pool/server previos (comentándolos) y añadir el nuestro
    sed -i -E 's/^(pool |server )/#&/' "$CHRONY_CONF"
    # Quitar lineas previas que hayamos añadido
    sed -i '/^# --- jetson_service managed ---/,/^# --- end jetson_service ---/d' "$CHRONY_CONF"
    cat >> "$CHRONY_CONF" <<EOF

# --- jetson_service managed ---
server ${HOST_IP} iburst minpoll 4 maxpoll 6
makestep 1.0 3
# --- end jetson_service ---
EOF
    systemctl restart chrony
    echo "Chrony reiniciado. Fuentes:"
    sleep 2
    chronyc sources || true
else
    echo "WARNING: no se encontró $CHRONY_CONF; saltando configuración NTP"
fi

echo "=== 4/4 Instalando systemd unit ==="
UNIT_SRC="$SRC_DIR/scripts/jetson-service.service"
UNIT_DST="/etc/systemd/system/jetson-service.service"
sed "s|__HOST_IP__|$HOST_IP|g" "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"

systemctl daemon-reload
systemctl enable jetson-service.service
systemctl restart jetson-service.service

echo ""
echo "=== LISTO ==="
echo "Estado: systemctl status jetson-service"
echo "Logs:   journalctl -u jetson-service -f"