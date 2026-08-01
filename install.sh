#!/usr/bin/env bash
# Instaluje nas-monitor jako usługę systemd. Uruchom z katalogu repo:
#   sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Uruchom jako root: sudo ./install.sh" >&2
  exit 1
fi

APP_DIR="/opt/nas-monitor"

echo "==> Instalowanie pakietów systemowych (smartmontools, mdadm, python3-venv)..."
apt-get update -qq
apt-get install -y smartmontools mdadm python3-venv

echo "==> Kopiowanie plików do ${APP_DIR}..."
mkdir -p "${APP_DIR}"
cp -r nas_monitor requirements.txt "${APP_DIR}/"

echo "==> Tworzenie virtualenv i instalacja zależności Python..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

echo "==> Instalacja usługi systemd..."
cp nas-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nas-monitor

IP=$(hostname -I | awk '{print $1}')
echo "==> Gotowe. Dashboard: http://${IP}:8420"
