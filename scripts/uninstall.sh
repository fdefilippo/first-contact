#!/bin/bash
# first-contact - disinstallazione.
#
#   ./uninstall.sh                  # dry-run
#   ./uninstall.sh --apply          # ferma e rimuove servizio, codice, config
#   ./uninstall.sh --apply --purge  # + elimina anche il database dei contatti
#
set -euo pipefail

PREFIX="${PREFIX:-/opt/first-contact}"
CONFIG_DIR="/etc/first-contact"
STATE_DIR="/var/lib/first-contact"
QUEUE_SOCKET_DIR="/var/spool/postfix/first-contact"
SERVICE="first-contact-milter"
MILTER_TOKEN="unix:/first-contact/milter.sock"
WRAPPER="/usr/local/bin/first-contact"

APPLY=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "argomento sconosciuto: $arg" >&2; exit 2 ;;
  esac
done

run() {
  if [ "$APPLY" -eq 1 ]; then echo "+ $*"; "$@"; else echo "  (dry-run) $*"; fi
}

split_milters() {
  local value="$1 " entry="" char depth=0 i
  # Gli spazi e le virgole dentro un blocco appartengono alle opzioni del milter.
  for ((i = 0; i < ${#value}; i++)); do
    char=${value:i:1}
    case "$char" in
      '{') depth=$((depth + 1)) ;;
      '}')
        if ((depth == 0)); then return 1; fi
        depth=$((depth - 1))
        ;;
      [[:space:],])
        if ((depth == 0)); then
          if [[ -n "$entry" ]]; then
            milters+=("$entry")
            entry=""
          fi
          continue
        fi
        ;;
    esac
    entry+="$char"
  done
  ((depth == 0))
}

if [ "$APPLY" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
  echo "serve root per --apply" >&2
  exit 1
fi

echo "== postfix: rimozione del milter da smtpd_milters"
current="$(postconf -h smtpd_milters 2>/dev/null || true)"
milters=()
if ! split_milters "$current"; then
  echo "smtpd_milters non valido: parentesi graffe sbilanciate" >&2
  exit 1
fi
new=""
found=0
for entry in "${milters[@]}"; do
  endpoint=$entry
  if [[ $entry =~ ^[{][[:space:]]*([^[:space:],{}]+) ]]; then
    endpoint=${BASH_REMATCH[1]}
  fi
  if [[ $endpoint == "$MILTER_TOKEN" ]]; then
    found=1
  else
    new+="${new:+ }${entry}"
  fi
done

if [ "$found" -eq 0 ]; then
  echo "  token non presente, nulla da fare"
elif [ "$APPLY" -eq 1 ]; then
  backup="/etc/postfix/main.cf.first-contact-removed.$(date +%Y%m%d%H%M%S)"
  cp -a /etc/postfix/main.cf "$backup"
  postconf -e "smtpd_milters = ${new}"
  postfix check
  systemctl reload postfix
  echo "  backup: $backup"
  echo "  nuovo valore: $(postconf -h smtpd_milters)"
else
  echo "  (dry-run) rimuovere ${MILTER_TOKEN} da smtpd_milters e ricaricare postfix"
fi

echo "== servizio"
run systemctl disable --now "$SERVICE" || true
run rm -f "/etc/systemd/system/${SERVICE}.service"
run rm -rf "/etc/systemd/system/${SERVICE}.service.d"
run rm -f /etc/tmpfiles.d/first-contact.conf
run rm -rf /usr/share/doc/first-contact
run systemctl daemon-reload

echo "== file"
run rm -f "$WRAPPER"
run rm -rf "$PREFIX"
run rm -rf "$CONFIG_DIR"

if [ "$PURGE" -eq 1 ]; then
  echo "== dati (--purge)"
  run rm -rf "$STATE_DIR"
  run rm -rf "$QUEUE_SOCKET_DIR"
else
  echo "== dati conservati in $STATE_DIR (usare --purge per eliminarli)"
  run rm -f "${QUEUE_SOCKET_DIR}/milter.sock"
fi

echo "== utente firstcontact: lasciato intatto (rimuovere a mano se serve)"
