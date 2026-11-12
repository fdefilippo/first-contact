#!/bin/bash
# first-contact - installazione.
#
# Senza argomenti mostra soltanto cosa farebbe (dry-run).
# Con --apply esegue davvero le modifiche sul server.
#
#   ./install.sh                     # piano, nessuna modifica
#   ./install.sh --apply             # installa servizio, utente, directory
#   ./install.sh --apply --with-postfix   # + modifica main.cf (con backup)
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/first-contact}"
CONFIG_DIR="/etc/first-contact"
CONFIG_FILE="${CONFIG_DIR}/config.ini"
STATE_DIR="/var/lib/first-contact"
QUEUE_SOCKET_DIR="/var/spool/postfix/first-contact"
SERVICE="first-contact-milter"
MILTER_TOKEN="unix:/first-contact/milter.sock"
WRAPPER="/usr/local/bin/first-contact"

APPLY=0
WITH_POSTFIX=0

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --with-postfix) WITH_POSTFIX=1 ;;
    -h|--help) usage ;;
    *) echo "argomento sconosciuto: $arg" >&2; exit 2 ;;
  esac
done

if [ "$WITH_POSTFIX" -eq 1 ] && [ "$APPLY" -eq 0 ]; then
  echo "nota: --with-postfix ha effetto solo insieme a --apply" >&2
fi

run() {
  if [ "$APPLY" -eq 1 ]; then
    echo "+ $*"
    "$@"
  else
    echo "  (dry-run) $*"
  fi
}

say() { printf '\n== %s\n' "$*"; }

cd "$(dirname "$0")/.."
REPO="$(pwd)"

say "0. prerequisiti"
command -v python3 >/dev/null || { echo "python3 assente" >&2; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 6):
    sys.exit("serve Python >= 3.6")
import sqlite3  # noqa: F401
print("python:", sys.version.split()[0])
PY

if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "serve root per --apply" >&2
  exit 1
fi

if ! python3 -c 'import Milter' >/dev/null 2>&1; then
  say "1. pymilter (python3-milter)"
  if [ "$APPLY" -eq 1 ]; then
    if ! apt-get install -y python3-milter; then
      cat >&2 <<'MSG'
installazione di python3-milter fallita.
Su Debian 10 (EOL) potrebbe servire un sources.list con archive.debian.org:
  deb http://archive.debian.org/debian buster main
  deb http://archive.debian.org/debian-security buster/updates main
Poi: apt-get update && apt-get install python3-milter
MSG
      exit 1
    fi
  else
    echo "  (dry-run) apt-get install -y python3-milter"
  fi
else
  say "1. pymilter gia' presente"
fi

say "2. utente di servizio"
if id firstcontact >/dev/null 2>&1; then
  echo "  utente firstcontact gia' esistente"
else
  run useradd --system --home-dir "$STATE_DIR" --no-create-home \
      --shell /usr/sbin/nologin firstcontact
fi

say "3. directory"
run install -d -m 0700 -o firstcontact -g firstcontact "$STATE_DIR"
run install -d -m 2750 -o firstcontact -g postfix "$QUEUE_SOCKET_DIR"

say "4. codice"
run install -d -m 0755 "$PREFIX"
if [ "$APPLY" -eq 1 ]; then
  rm -rf "${PREFIX}/firstcontact"
fi
run cp -a "${REPO}/src/firstcontact" "${PREFIX}/"
run install -m 0644 "${REPO}/README.md" "${PREFIX}/README.md"
if [ "$APPLY" -eq 1 ]; then
  chown -R root:root "$PREFIX"
  chmod -R a+rX,go-w "$PREFIX"
fi

say "5. configurazione"
run install -d -m 0755 "$CONFIG_DIR"
if [ -f "$CONFIG_FILE" ]; then
  echo "  $CONFIG_FILE esiste: non viene sovrascritto"
else
  run install -m 0640 -o root -g firstcontact "${REPO}/etc/first-contact.ini" "$CONFIG_FILE"
fi
WHITELIST_FILE="${CONFIG_DIR}/whitelist.txt"
if [ -f "$WHITELIST_FILE" ]; then
  echo "  $WHITELIST_FILE esiste: non viene sovrascritto"
else
  run install -m 0640 -o root -g firstcontact "${REPO}/etc/whitelist.txt" "$WHITELIST_FILE"
fi

say "6. comando first-contact"
if [ "$APPLY" -eq 1 ]; then
  cat > "$WRAPPER" <<EOF
#!/bin/sh
PYTHONPATH=${PREFIX} exec /usr/bin/python3 -m firstcontact "\$@"
EOF
  chmod 0755 "$WRAPPER"
  echo "+ scritto $WRAPPER"
else
  echo "  (dry-run) scrivere $WRAPPER"
fi

say "7. systemd"
run install -m 0644 "${REPO}/systemd/first-contact-milter.service" \
    "/etc/systemd/system/${SERVICE}.service"
run install -d -m 0755 "/etc/systemd/system/${SERVICE}.service.d"
run install -m 0644 "${REPO}/packaging/systemd-prefix.conf" \
    "/etc/systemd/system/${SERVICE}.service.d/prefix.conf"
run install -d -m 0755 "/usr/share/doc/first-contact"
run install -m 0644 "${REPO}/README.md" "/usr/share/doc/first-contact/README.md"
run install -m 0644 "${REPO}/etc/tmpfiles.d/first-contact.conf" \
    "/etc/tmpfiles.d/first-contact.conf"
run systemd-tmpfiles --create /etc/tmpfiles.d/first-contact.conf
run systemctl daemon-reload
run systemctl enable --now "$SERVICE"

if [ "$APPLY" -eq 1 ]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -S "${QUEUE_SOCKET_DIR}/milter.sock" ] && break
    sleep 0.5
  done
  if [ -S "${QUEUE_SOCKET_DIR}/milter.sock" ]; then
    echo "  socket attivo: ${QUEUE_SOCKET_DIR}/milter.sock"
  else
    echo "  ATTENZIONE: socket non creato; controllare: journalctl -u $SERVICE -n 30" >&2
  fi
fi

say "8. postfix"
echo "  aggiungere in coda a smtpd_milters:"
echo "    postconf -e 'smtpd_milters = \$(postconf -h smtpd_milters) ${MILTER_TOKEN}'"
echo "    postfix check && systemctl reload postfix"
echo "  (oppure rilanciare: ./install.sh --apply --with-postfix)"
echo "  nota: se opendkim FIRMA (Mode s/sv) va messo dopo first-contact, cosi'"
echo "        la firma locale copre anche il banner; vedi postfix/main.cf.snippet"

if [ "$WITH_POSTFIX" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
  current="$(postconf -h smtpd_milters)"
  case " $current " in
    *" ${MILTER_TOKEN} "*)
      echo "  gia' presente in smtpd_milters, nessuna modifica"
      ;;
    *)
      backup="/etc/postfix/main.cf.first-contact.$(date +%Y%m%d%H%M%S)"
      cp -a /etc/postfix/main.cf "$backup"
      echo "  backup: $backup"
      postconf -e "smtpd_milters = ${current} ${MILTER_TOKEN}"
      postfix check
      systemctl reload postfix
      echo "  nuovo valore: $(postconf -h smtpd_milters)"
      ;;
  esac
fi

say "9. verifica"
echo "  first-contact doctor"
echo "  systemctl status ${SERVICE} --no-pager"
echo "  tail -f /var/log/mail.log | grep first-contact"
