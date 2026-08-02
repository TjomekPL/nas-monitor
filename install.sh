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

STATE_DIR="/etc/nas-monitor"
CREDENTIALS_FILE="${STATE_DIR}/auth-credentials.json"

if [[ -f "$CREDENTIALS_FILE" ]]; then
  echo "==> Konto administratora już skonfigurowane - pomijam (użyj panelu konta w dashboardzie, żeby zmienić hasło)."
  ADMIN_USERNAME=""
else
  if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
    BOLD=$(tput bold); CYAN=$(tput setaf 6); RESET=$(tput sgr0)
  else
    BOLD=""; CYAN=""; RESET=""
  fi
  echo ""
  echo "${BOLD}${CYAN}============================================================${RESET}"
  echo "${BOLD}${CYAN}  KONFIGURACJA KONTA ADMINISTRATORA${RESET}"
  echo "${CYAN}  (logowanie do dashboardu - osobne od kont systemowych/SMB)${RESET}"
  echo "${BOLD}${CYAN}============================================================${RESET}"
  echo ""
  read -rp "Nazwa użytkownika [admin]: " ADMIN_USERNAME
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"

  while true; do
    read -rsp "Hasło (min. 10 znaków, litera i cyfra): " ADMIN_PASSWORD
    echo ""
    read -rsp "Powtórz hasło: " ADMIN_PASSWORD_CONFIRM
    echo ""

    if [[ "$ADMIN_PASSWORD" != "$ADMIN_PASSWORD_CONFIRM" ]]; then
      echo "Hasła nie są takie same, spróbuj ponownie."
      continue
    fi
    if [[ ${#ADMIN_PASSWORD} -lt 10 ]]; then
      echo "Hasło musi mieć co najmniej 10 znaków."
      continue
    fi
    if ! [[ "$ADMIN_PASSWORD" =~ [A-Za-z] ]]; then
      echo "Hasło musi zawierać przynajmniej jedną literę."
      continue
    fi
    if ! [[ "$ADMIN_PASSWORD" =~ [0-9] ]]; then
      echo "Hasło musi zawierać przynajmniej jedną cyfrę."
      continue
    fi
    break
  done

  printf '%s\n%s\n' "$ADMIN_USERNAME" "$ADMIN_PASSWORD" | (cd "${APP_DIR}" && "${APP_DIR}/venv/bin/python3" -m nas_monitor.setup_admin)
  unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM
fi

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
if [[ -n "$ADMIN_USERNAME" ]]; then
  echo "    Konto administratora: ${ADMIN_USERNAME}"
  echo "    Zaloguj się pod: http://${IP}:8420/login"
fi

if ! systemctl is-active --quiet NetworkManager; then
  echo ""
  echo "Uwaga: NetworkManager nie jest aktywny na tym hoście."
  echo "Reszta nas-monitor dziala normalnie (dyski, uzytkownicy, udzialy,"
  echo "klucze SSH, podglad sieci) - ale EDYCJA ustawien sieciowych"
  echo "(IP/brama/DNS) w zakladce Siec wymaga NetworkManagera i bedzie"
  echo "niedostepna, dopoki go nie wlaczysz."
fi
