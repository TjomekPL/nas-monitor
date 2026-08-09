#!/usr/bin/env bash
# Resets the nas-monitor admin password (or creates the account if none
# exists yet, e.g. install.sh's prompt was skipped). Run on the server
# itself, over SSH:
#   sudo ./reset-admin-password.sh
#
# Takes effect immediately, no restart needed - the credentials file is
# read fresh on every login, never cached.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Uruchom jako root: sudo ./reset-admin-password.sh" >&2
  exit 1
fi

APP_DIR="/opt/nas-monitor"
CREDENTIALS_FILE="/etc/nas-monitor/auth-credentials.json"

if [[ ! -d "$APP_DIR" ]]; then
  echo "Nie znaleziono ${APP_DIR} - nas-monitor nie jest zainstalowany." >&2
  exit 1
fi

CURRENT_USERNAME=""
if [[ -f "$CREDENTIALS_FILE" ]]; then
  CURRENT_USERNAME=$("${APP_DIR}/venv/bin/python3" -c \
    "import json; print(json.load(open('${CREDENTIALS_FILE}')).get('username',''))" 2>/dev/null || true)
fi

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1; then
  BOLD=$(tput bold); CYAN=$(tput setaf 6); RESET=$(tput sgr0)
else
  BOLD=""; CYAN=""; RESET=""
fi
echo ""
echo "${BOLD}${CYAN}============================================================${RESET}"
echo "${BOLD}${CYAN}  RESET HASŁA ADMINISTRATORA - nas-monitor${RESET}"
echo "${BOLD}${CYAN}============================================================${RESET}"
echo ""

if [[ -n "$CURRENT_USERNAME" ]]; then
  read -rp "Nazwa użytkownika [${CURRENT_USERNAME}]: " ADMIN_USERNAME
  ADMIN_USERNAME="${ADMIN_USERNAME:-$CURRENT_USERNAME}"
else
  read -rp "Nazwa użytkownika [admin]: " ADMIN_USERNAME
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
fi

while true; do
  read -rsp "Nowe hasło (min. 10 znaków, litera i cyfra): " ADMIN_PASSWORD
  echo ""
  read -rsp "Powtórz nowe hasło: " ADMIN_PASSWORD_CONFIRM
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

echo ""
echo "Gotowe. Hasło ustawione dla konta: ${ADMIN_USERNAME}"
echo "Zaloguj się od razu - nie trzeba restartować usługi."
