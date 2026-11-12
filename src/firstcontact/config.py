"""Lettura e validazione della configurazione (INI, solo stdlib).

Si usa INI e non TOML perche' Python 3.7 (Debian 10) non ha ``tomllib``.
"""

import configparser
import ipaddress
import os
import re
from typing import Dict, List, Optional

from .banner import DEFAULT_RESUMED_WARNING, DEFAULT_WARNING

DEFAULTS = {
    "milter": {
        "socket": "unix:/var/spool/postfix/first-contact/milter.sock",
        "header_name": "X-First-Contact",
        # "top" inserisce l'header in cima (dopo la prima Received),
        # "end" lo accoda in fondo al blocco header.
        "header_position": "top",
        # timeout (secondi) del loop milter; 0 = nessun timeout lato libmilter
        "timeout": "0",
    },
    "banner": {
        # off = solo header; body = avviso in testa al messaggio;
        # subject = prefisso nell'oggetto; both = entrambi
        "mode": "body",
        "text": DEFAULT_WARNING,
        # avviso quando il mittente ricompare dopo un'assenza
        "text_resumed": DEFAULT_RESUMED_WARNING,
        "subject_tag": "",
        # oltre questa dimensione il corpo non viene riscritto (si torna
        # al solo header); 0 = nessun limite
        "max_body_bytes": "20971520",
        # non toccare multipart/signed e multipart/encrypted
        "skip_signed": "true",
        # box HTML con sfondo giallo e bordo tratteggiato; false = solo testo
        "html_box": "true",
    },
    "policy": {
        # envelope = MAIL FROM, from = header From:
        "key_source": "envelope",
        # address = ogni indirizzo e' un mittente distinto,
        # domain  = il primo messaggio da un dominio
        "key_scope": "address",
        "process_authenticated": "false",
        "process_local": "false",
        "skip_null_sender": "true",
        # reti considerate "interne": i loro client non generano allerta
        "trusted_networks": "127.0.0.0/8, ::1",
        # domini di mittente da ignorare (mittenti locali)
        "skip_local_senders": "",
        "max_recipients": "50",
        # giorni di assenza dopo i quali il mittente e' "tornato"
        # (0 = regola disattivata)
        "dormant_days": "90",
        # registra i contatti ma non aggiunge header (rodaggio consigliato)
        "observe_only": "false",
    },
    "whitelist": {
        # mittenti esclusi dall'avviso: indirizzi, domini (@dominio) o CIDR
        "senders": "",
        "networks": "",
        # file con una voce per riga, riletto quando cambia (opzionale)
        "file": "",
    },
    "store": {
        "path": "/var/lib/first-contact/contacts.db",
        "track_stats": "true",
        # 0 = conserva per sempre
        "retention_days": "0",
    },
    "logging": {
        "level": "INFO",
        "syslog": "true",
        "syslog_facility": "mail",
        "syslog_tag": "first-contact",
        "file": "",
    },
}

SECTIONS = tuple(DEFAULTS.keys())
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_HEADER_NAME_RE = re.compile(r"^[!-9;-~]{1,78}$")
# pymilter/libmilter: inet:porta[@host], inet6:porta[@host], unix:/percorso
_INET_RE = re.compile(r"^(?P<proto>inet6?):(?P<port>[0-9]{1,5})(?:@(?P<host>[^@\s]+))?$")


class ConfigError(Exception):
    """Configurazione assente o non valida."""


class Config:
    def __init__(self, values: Dict[str, Dict[str, str]], path: Optional[str] = None):
        self._values = values
        self.path = path
        self.warnings: List[str] = []
        self._networks = None
        self.validate()

    # -- accesso ---------------------------------------------------------
    def get(self, section: str, key: str) -> str:
        return self._values[section][key]

    def get_bool(self, section: str, key: str) -> bool:
        raw = self.get(section, key).strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        raise ConfigError("[%s] %s: valore booleano non valido: %r" % (section, key, raw))

    def get_int(self, section: str, key: str) -> int:
        raw = self.get(section, key).strip()
        try:
            return int(raw)
        except ValueError:
            raise ConfigError("[%s] %s: valore intero non valido: %r" % (section, key, raw))

    def get_list(self, section: str, key: str) -> List[str]:
        raw = self.get(section, key)
        return [item.strip() for item in re.split(r"[,\n]", raw) if item.strip()]

    # -- proprieta' comode ----------------------------------------------
    @property
    def socket(self) -> str:
        value = self.get("milter", "socket").strip()
        if value.startswith("unix:"):
            path = value[5:].strip()
            if not path or not path.startswith("/"):
                raise ConfigError(
                    "[milter] socket: percorso unix non valido: %r (serve un "
                    "percorso assoluto)" % value
                )
            return "unix:" + path
        match = _INET_RE.match(value)
        if match:
            port = int(match.group("port"))
            if not 1 <= port <= 65535:
                raise ConfigError("[milter] socket: porta non valida: %r" % value)
            return value
        raise ConfigError(
            "[milter] socket: usare 'unix:/percorso/socket' oppure "
            "'inet:porta[@host]' / 'inet6:porta[@host]' (trovato %r)" % value
        )

    @property
    def socket_path(self) -> Optional[str]:
        sock = self.socket
        return sock[5:] if sock.startswith("unix:") else None

    @property
    def header_name(self) -> str:
        return self.get("milter", "header_name").strip()

    @property
    def header_position(self) -> str:
        return self.get("milter", "header_position").strip().lower()

    @property
    def key_source(self) -> str:
        return self.get("policy", "key_source").strip().lower()

    @property
    def key_scope(self) -> str:
        return self.get("policy", "key_scope").strip().lower()

    @property
    def trusted_networks(self) -> List[object]:
        if self._networks is None:
            nets = []
            for item in self.get_list("policy", "trusted_networks"):
                try:
                    nets.append(ipaddress.ip_network(item, strict=False))
                except ValueError:
                    raise ConfigError(
                        "[policy] trusted_networks: rete non valida: %r" % item
                    )
            self._networks = nets
        return self._networks

    @property
    def store_path(self) -> str:
        return os.path.expanduser(self.get("store", "path").strip())

    @property
    def retention_days(self) -> int:
        return self.get_int("store", "retention_days")

    @property
    def max_recipients(self) -> int:
        return self.get_int("policy", "max_recipients")

    @property
    def dormant_days(self) -> int:
        return self.get_int("policy", "dormant_days")

    @property
    def track_stats(self) -> bool:
        return self.get_bool("store", "track_stats")

    @property
    def observe_only(self) -> bool:
        return self.get_bool("policy", "observe_only")

    # -- avviso visibile ------------------------------------------------
    @property
    def banner_mode(self) -> str:
        return self.get("banner", "mode").strip().lower()

    @property
    def banner_text(self) -> str:
        return self.get("banner", "text")

    @property
    def banner_text_resumed(self) -> str:
        return self.get("banner", "text_resumed")

    @property
    def subject_tag(self) -> str:
        return " ".join(self.get("banner", "subject_tag").split())

    @property
    def max_body_bytes(self) -> int:
        return self.get_int("banner", "max_body_bytes")

    @property
    def skip_signed(self) -> bool:
        return self.get_bool("banner", "skip_signed")

    @property
    def html_box(self) -> bool:
        return self.get_bool("banner", "html_box")

    # -- whitelist -------------------------------------------------------
    @property
    def whitelist_file(self) -> Optional[str]:
        value = self.get("whitelist", "file").strip()
        return os.path.expanduser(value) if value else None

    # -- validazione ------------------------------------------------------
    def validate(self) -> None:
        self.socket  # noqa: B018 - forza la validazione
        if not _HEADER_NAME_RE.match(self.header_name):
            raise ConfigError(
                "[milter] header_name: nome header non valido: %r" % self.header_name
            )
        if self.header_position not in ("top", "end"):
            raise ConfigError(
                "[milter] header_position: usare 'top' oppure 'end' (trovato %r)"
                % self.header_position
            )
        if self.key_source not in ("envelope", "from"):
            raise ConfigError(
                "[policy] key_source: usare 'envelope' oppure 'from' (trovato %r)"
                % self.key_source
            )
        if self.key_scope not in ("address", "domain"):
            raise ConfigError(
                "[policy] key_scope: usare 'address' oppure 'domain' (trovato %r)"
                % self.key_scope
            )
        if self.banner_mode not in ("off", "body", "subject", "both"):
            raise ConfigError(
                "[banner] mode: usare 'off', 'body', 'subject' o 'both' (trovato %r)"
                % self.banner_mode
            )
        if self.max_body_bytes < 0:
            raise ConfigError("[banner] max_body_bytes: non puo' essere negativo")
        if "\n" in self.get("banner", "subject_tag"):
            raise ConfigError("[banner] subject_tag: deve stare su una riga")
        timeout = self.get_int("milter", "timeout")
        if timeout < 0:
            raise ConfigError("[milter] timeout: non puo' essere negativo")
        if self.get_int("policy", "max_recipients") < 1:
            raise ConfigError("[policy] max_recipients: deve essere >= 1")
        if self.dormant_days < 0:
            raise ConfigError("[policy] dormant_days: non puo' essere negativo")
        if self.retention_days < 0:
            raise ConfigError("[store] retention_days: non puo' essere negativo")
        if not self.store_path:
            raise ConfigError("[store] path: percorso vuoto")
        level = self.get("logging", "level").strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError("[logging] level: livello non riconosciuto: %r" % level)
        self.trusted_networks  # noqa: B018
        self.skip_signed  # noqa: B018
        self.html_box  # noqa: B018

    # -- costruttori ------------------------------------------------------
    @classmethod
    def defaults(cls) -> "Config":
        values = {section: dict(items) for section, items in DEFAULTS.items()}
        return cls(values)

    @classmethod
    def from_overrides(cls, overrides=None) -> "Config":
        """Config con i default, sovrascritti da ``{sezione: {chiave: valore}}``."""
        values = {section: dict(items) for section, items in DEFAULTS.items()}
        for section, items in (overrides or {}).items():
            if section not in values:
                raise ConfigError("sezione sconosciuta: %s" % section)
            values[section].update({k.lower(): str(v) for k, v in items.items()})
        return cls(values)

    @classmethod
    def load(cls, path: str) -> "Config":
        if not path:
            raise ConfigError("nessun file di configurazione indicato")
        if not os.path.exists(path):
            raise ConfigError("file di configurazione non trovato: %s" % path)
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # mantiene la forma delle chiavi
        try:
            with open(path, "r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except OSError as exc:
            raise ConfigError("impossibile leggere %s: %s" % (path, exc))
        except configparser.Error as exc:
            raise ConfigError("file INI non valido (%s): %s" % (path, exc))

        unknown_sections = [s for s in parser.sections() if s not in SECTIONS]
        if unknown_sections:
            raise ConfigError(
                "sezioni sconosciute in %s: %s" % (path, ", ".join(unknown_sections))
            )

        values = {section: dict(items) for section, items in DEFAULTS.items()}
        warnings = []
        for section in parser.sections():
            for key, value in parser.items(section):
                key = key.strip().lower()
                if key not in values[section]:
                    warnings.append("[%s] chiave sconosciuta ignorata: %s" % (section, key))
                    continue
                values[section][key] = value.strip()
        cfg = cls(values, path=path)
        cfg.warnings = warnings
        return cfg
