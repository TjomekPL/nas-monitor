#!/usr/bin/env bash
# Instaluje nas-monitor jako usługę systemd. Uruchom z katalogu repo:
#   sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Uruchom jako root: sudo ./install.sh" >&2
  exit 1
fi

APP_DIR="/opt/nas-monitor"
REPO_URL="https://github.com/TjomekPL/nas-monitor.git"

echo "==> Instalowanie pakietów systemowych (smartmontools, mdadm, samba, sshpass, python3-venv, git, parted, xfsprogs, btrfs-progs, exfatprogs)..."
apt-get update -qq
apt-get install -y smartmontools mdadm samba sshpass openssh-client python3-venv acl git parted xfsprogs btrfs-progs exfatprogs

# ${APP_DIR} is now a real git checkout, not a plain file copy - that's
# what lets the "Zainstaluj aktualizację" button in the dashboard later
# do a plain `git fetch` + `git reset --hard` instead of re-downloading
# the whole project. An install from before this existed left ${APP_DIR}
# as a directory with no .git in it; that one-time case is handled below
# by just re-cloning fresh (nothing under ${APP_DIR} is ever real state -
# credentials/session data live in /etc/nas-monitor, untouched by this).
if [[ -d "${APP_DIR}/.git" ]]; then
  echo "==> ${APP_DIR} już jest repozytorium git - pobieram najnowszą wersję..."
  git -C "${APP_DIR}" fetch --tags --quiet origin
  git -C "${APP_DIR}" reset --hard --quiet origin/main
else
  echo "==> Klonowanie repozytorium do ${APP_DIR}..."
  rm -rf "${APP_DIR}"
  git clone --quiet "${REPO_URL}" "${APP_DIR}"
fi

echo "==> Tworzenie virtualenv i instalacja zależności Python..."
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"

echo "==> Instalowanie nginx i fail2ban (HTTPS + ochrona przed brute-force)..."
apt-get install -y nginx fail2ban openssl

# self-signed TLS cert - generated once, then reused on every future
# install.sh run (regenerating it on each update would mean re-accepting
# the browser warning every single release, for no real benefit).
TLS_DIR="/etc/nas-monitor/tls"
CERT_FILE="${TLS_DIR}/nas-monitor.crt"
KEY_FILE="${TLS_DIR}/nas-monitor.key"
HTTPS_OK=0

mkdir -p "${TLS_DIR}"
chmod 700 "${TLS_DIR}"

if [[ ! -f "$CERT_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "==> Generowanie certyfikatu self-signed..."
  HOST_CN=$(hostname)
  HOST_IP=$(hostname -I | awk '{print $1}')
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$KEY_FILE" -out "$CERT_FILE" \
    -subj "/CN=${HOST_CN}" \
    -addext "subjectAltName=DNS:${HOST_CN},IP:${HOST_IP}" \
    2>/dev/null || true
fi
chmod 600 "$KEY_FILE" 2>/dev/null || true
chmod 644 "$CERT_FILE" 2>/dev/null || true

# Everything below is deliberately non-fatal (no bare failing command
# outside an `if`) - a broken nginx config or a cert that failed to
# generate must never abort the whole install and leave the dashboard
# itself not installed. Worst case, HTTPS_OK stays 0 and the service
# falls back to plain HTTP on 0.0.0.0:8420, same as before this feature
# existed - see the __BIND_ADDR__ substitution further down.
if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
  echo "==> Konfiguracja nginx (reverse proxy + TLS)..."
  sed -e "s#__CERT_PATH__#${CERT_FILE}#" -e "s#__KEY_PATH__#${KEY_FILE}#" \
    "${APP_DIR}/nginx/nas-monitor.conf" > /etc/nginx/sites-available/nas-monitor
  ln -sf /etc/nginx/sites-available/nas-monitor /etc/nginx/sites-enabled/nas-monitor
  rm -f /etc/nginx/sites-enabled/default

  if nginx -t 2>/tmp/nas-monitor-nginx-test.log; then
    systemctl enable nginx >/dev/null 2>&1 || true
    if systemctl restart nginx; then
      HTTPS_OK=1
    else
      echo "OSTRZEŻENIE: nginx nie wystartował - zostaję na zwykłym HTTP na porcie 8420." >&2
    fi
  else
    echo "OSTRZEŻENIE: nieprawidłowa konfiguracja nginx - zostaję na zwykłym HTTP na porcie 8420:" >&2
    cat /tmp/nas-monitor-nginx-test.log >&2
  fi
else
  echo "OSTRZEŻENIE: nie udało się wygenerować certyfikatu TLS - zostaję na zwykłym HTTP na porcie 8420." >&2
fi

echo "==> Konfiguracja fail2ban..."
mkdir -p /var/log/nas-monitor
cp "${APP_DIR}/fail2ban/nas-monitor.filter.conf" /etc/fail2ban/filter.d/nas-monitor.conf
cp "${APP_DIR}/fail2ban/nas-monitor.jail.conf" /etc/fail2ban/jail.d/nas-monitor.conf
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban || echo "OSTRZEŻENIE: fail2ban nie wystartował - sprawdź 'sudo systemctl status fail2ban'." >&2

if [[ "$HTTPS_OK" -eq 1 ]]; then
  BIND_ADDR="127.0.0.1"
else
  BIND_ADDR="0.0.0.0"
fi

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

echo "==> Ukrywanie kont SMB-only z graficznego ekranu logowania (AccountsService)..."
# nas_monitor.users.create_user() already does this for every NEW
# account going forward - this is the one-time backfill for accounts
# created by an earlier version of the tool, before this existed. Safe
# to re-run every install.sh - a marker that's already there is just
# overwritten with the same content.
mkdir -p /var/lib/AccountsService/users
if getent group smb_users >/dev/null 2>&1; then
  for u in $(getent group smb_users | cut -d: -f4 | tr ',' ' '); do
    [[ -n "$u" ]] && printf '[User]\nSystemAccount=true\n' > "/var/lib/AccountsService/users/${u}"
  done
fi
# accounts-daemon caches its user list in memory - writing the marker
# files above does nothing visible on the actual login screen until it
# re-reads them, which only happens on its own restart (not just a
# screen lock, and not automatically on file change). Best-effort:
# a headless install has no display manager and thus no reason for
# this service to even be installed, so a missing/inactive
# accounts-daemon here is fine, not an error.
systemctl restart accounts-daemon 2>/dev/null || true

echo "==> Instalacja usługi systemd..."
sed "s#__BIND_ADDR__#${BIND_ADDR}#" "${APP_DIR}/nas-monitor.service" > /etc/systemd/system/nas-monitor.service
systemctl daemon-reload
systemctl enable nas-monitor
# restart (not "enable --now") so re-running this script on an already
# running service actually picks up new code - "--now" is a no-op start
# if the unit is already active, which silently left old code loaded
# after copying new files in place.
systemctl restart nas-monitor

IP=$(hostname -I | awk '{print $1}')
if [[ "$HTTPS_OK" -eq 1 ]]; then
  echo "==> Gotowe. Dashboard: https://${IP}"
  echo "    Certyfikat jest self-signed - przeglądarka pokaże ostrzeżenie przy"
  echo "    pierwszym wejściu, zaakceptuj je raz (\"Zaawansowane\" -> \"Przejdź dalej\")."
else
  echo "==> Gotowe. Dashboard: http://${IP}:8420"
fi
if [[ -n "$ADMIN_USERNAME" ]]; then
  echo "    Konto administratora: ${ADMIN_USERNAME}"
fi

if ! systemctl is-active --quiet NetworkManager; then
  echo ""
  echo "Uwaga: NetworkManager nie jest aktywny na tym hoście."
  echo "Reszta nas-monitor dziala normalnie (dyski, uzytkownicy, udzialy,"
  echo "klucze SSH, podglad sieci) - ale EDYCJA ustawien sieciowych"
  echo "(IP/brama/DNS) w zakladce Siec wymaga NetworkManagera i bedzie"
  echo "niedostepna, dopoki go nie wlaczysz."
fi
