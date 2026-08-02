#!/usr/bin/env bash
# Instaluje nas-monitor jako usługę systemd. Uruchom z katalogu repo:
#   sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Uruchom jako root: sudo ./install.sh" >&2
  exit 1
fi

APP_DIR="/opt/nas-monitor"

echo "==> Instalowanie pakietów systemowych (smartmontools, mdadm, samba, sshpass, python3-venv)..."
apt-get update -qq
apt-get install -y smartmontools mdadm samba sshpass openssh-client python3-venv

echo "==> Kopiowanie plików do ${APP_DIR}..."
mkdir -p "${APP_DIR}"
cp -r nas_monitor requirements.txt "${APP_DIR}/"

echo "==> Tworzenie virtualenv i instalacja zależności Python..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

echo "==> Instalacja usługi systemd..."
cp nas-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable nas-monitor
# restart (not "enable --now") so re-running this script on an already
# running service actually picks up new code - "--now" is a no-op start
# if the unit is already active, which silently left old code loaded
# after copying new files in place.
systemctl restart nas-monitor

IP=$(hostname -I | awk '{print $1}')
echo "==> Gotowe. Dashboard: http://${IP}:8420"

if ! systemctl is-active --quiet NetworkManager; then
  echo ""
  echo "Uwaga: NetworkManager nie jest aktywny na tym hoście."
  echo "Reszta nas-monitor dziala normalnie (dyski, uzytkownicy, udzialy,"
  echo "klucze SSH, podglad sieci) - ale EDYCJA ustawien sieciowych"
  echo "(IP/brama/DNS) w zakladce Siec wymaga NetworkManagera i bedzie"
  echo "niedostepna, dopoki go nie wlaczysz."
fi
