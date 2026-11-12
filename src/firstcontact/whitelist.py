"""Whitelist: mittenti e reti che non devono generare alcun avviso.

Formati accettati (uno per riga nel file, oppure separati da virgola nella
configurazione):

    mario.rossi@example.com      indirizzo esatto
    @example.com                 dominio (e sottodomini)
    example.com                  come sopra
    *.example.com                come sopra
    10.0.0.0/8                   rete CIDR
    203.0.113.9                  singolo indirizzo IP
    # commento                   (solo nel file)

Il file indicato in ``[whitelist] file`` viene riletto automaticamente quando
cambia (mtime/dimensione): si possono aggiungere o togliere voci senza
riavviare il servizio. Le righe non valide del file vengono ignorate con un
avviso nei log; gli errori nella configurazione INI invece bloccano l'avvio.
"""

import ipaddress
import logging
import os
import re
from typing import List, Optional, Sequence, Tuple

from .policy import address_domain, is_address_like, normalize_address

_ADDRESS_LIKE_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

log = logging.getLogger("firstcontact.whitelist")


class WhitelistError(Exception):
    pass


class SenderWhitelist:
    def __init__(self, path: Optional[str] = None):
        self.path = path or None
        self.addresses = set()
        self.domains = set()
        self.networks: List[ipaddress._BaseNetwork] = []
        self.warnings: List[str] = []
        self._file_mtime: Optional[Tuple[int, int]] = None
        self._file_addresses = set()
        self._file_domains = set()
        self._file_networks: List[ipaddress._BaseNetwork] = []
        self._file_missing_logged = False

    # -- caricamento ------------------------------------------------------
    def load_inline(self, senders: Sequence[str], networks: Sequence[str]) -> None:
        """Voci dalla configurazione: un errore blocca l'avvio."""
        for raw in list(senders) + list(networks):
            entry = _parse_entry(raw)
            if entry is None:
                raise WhitelistError("voce whitelist non valida: %r" % raw)
            self._add(entry)

    def load_file(self) -> None:
        """Rilegge il file se e' cambiato. Errori non bloccanti."""
        if not self.path:
            return
        try:
            info = os.stat(self.path)
        except OSError:
            if not self._file_missing_logged:
                log.warning("file whitelist non accessibile: %s", self.path)
                self._file_missing_logged = True
            return
        self._file_missing_logged = False
        signature = (int(info.st_mtime), int(info.st_size))
        if signature == self._file_mtime:
            return
        addresses, domains, networks, warnings = set(), set(), [], []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    entry = _parse_entry(line)
                    if entry is None:
                        warnings.append(
                            "%s:%d: voce ignorata: %r" % (self.path, number, text)
                        )
                        continue
                    kind, value = entry
                    if kind == "address":
                        addresses.add(value)
                    elif kind == "domain":
                        domains.add(value)
                    else:
                        networks.append(value)
        except OSError as exc:
            log.warning("lettura di %s fallita: %s", self.path, exc)
            return
        self._file_addresses = addresses
        self._file_domains = domains
        self._file_networks = networks
        self._file_mtime = signature
        self.warnings = warnings
        for warning in warnings:
            log.warning("%s", warning)
        log.info(
            "whitelist ricaricata da %s: %d indirizzi, %d domini, %d reti",
            self.path,
            len(addresses),
            len(domains),
            len(networks),
        )

    def _add(self, entry: Tuple[str, object]) -> None:
        kind, value = entry
        if kind == "address":
            self.addresses.add(value)
        elif kind == "domain":
            self.domains.add(value)
        else:
            self.networks.append(value)

    # -- interrogazione ---------------------------------------------------
    def reload(self) -> None:
        self.load_file()

    def matches(self, sender: Optional[str], client_addr: Optional[str]) -> Optional[str]:
        """Ritorna la regola che ha fatto match, oppure ``None``."""
        self.load_file()

        address = normalize_address(sender)
        if address and is_address_like(address):
            for rule in (self.addresses, self._file_addresses):
                if address in rule:
                    return "address:" + address
            domain = address_domain(address)
            if domain:
                for rule in (self.domains, self._file_domains):
                    if _domain_matches(domain, rule):
                        return "domain:" + domain

        if client_addr:
            try:
                ip = ipaddress.ip_address(client_addr.strip().strip("[]"))
            except ValueError:
                ip = None
            if ip is not None:
                for network in list(self.networks) + list(self._file_networks):
                    if ip.version == network.version and ip in network:
                        return "cidr:" + str(network)
        return None

    def summary(self) -> str:
        return "%s, %s, %s" % (
            _count(len(self.addresses) + len(self._file_addresses), "indirizzo", "indirizzi"),
            _count(len(self.domains) + len(self._file_domains), "dominio", "domini"),
            _count(len(self.networks) + len(self._file_networks), "rete", "reti"),
        )


def _count(value: int, singular: str, plural: str) -> str:
    return "%d %s" % (value, singular if value == 1 else plural)


def _domain_matches(domain: str, rules) -> bool:
    for rule in rules:
        if domain == rule or domain.endswith("." + rule):
            return True
    return False


def _parse_entry(raw: str) -> Optional[Tuple[str, object]]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("#"):
        return None
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    lowered = text.lower()

    # rete o indirizzo IP
    if "/" in lowered or _looks_like_ip(lowered):
        try:
            return "network", ipaddress.ip_network(lowered, strict=False)
        except ValueError:
            return None

    if lowered.startswith("@"):
        lowered = lowered[1:]
    elif lowered.startswith("*."):
        lowered = lowered[2:]
    lowered = lowered.strip(".")

    if "@" in lowered:
        local, _, domain = lowered.partition("@")
        if not local or not domain or not _DOMAIN_RE.match(domain):
            return None
        return "address", lowered

    if not _DOMAIN_RE.match(lowered) or "." not in lowered:
        return None
    return "domain", lowered


def _looks_like_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def from_config(config) -> SenderWhitelist:
    whitelist = SenderWhitelist(config.whitelist_file)
    whitelist.load_inline(
        config.get_list("whitelist", "senders"),
        config.get_list("whitelist", "networks"),
    )
    whitelist.load_file()
    return whitelist
