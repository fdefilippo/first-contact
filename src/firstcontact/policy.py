"""Normalizzazione indirizzi e regole "che messaggi guardare".

Qui non c'e' I/O: tutto e' testabile senza Postfix.
"""

import ipaddress
import re
from email.utils import parseaddr
from typing import Optional, Tuple

from .config import ConfigError

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_ADDR_LIKE_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
MAX_ADDRESS_LEN = 320


def _strip_brackets(value: str) -> str:
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        value = value[1:end] if end > 0 else value[1:]
    elif value.endswith(">") and "<" in value:
        value = value[value.find("<") + 1 : -1]
    return value.strip()


def normalize_address(raw: Optional[str]) -> Optional[str]:
    """Riporta un indirizzo a una forma confrontabile.

    Ritorna ``None`` per mittente nullo (``<>``), vuoto o inutilizzabile.
    Rimuove caratteri di controllo (difesa da header injection) e spazi.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    value = _CTRL_RE.sub("", raw).strip()
    if not value:
        return None
    value = _strip_brackets(value)
    value = _WS_RE.sub("", value).lower()
    if not value or value in ("<>", "<"):
        return None
    if len(value) > MAX_ADDRESS_LEN:
        value = value[:MAX_ADDRESS_LEN]
    return value


def from_header_address(raw: Optional[str]) -> Optional[str]:
    """Estrae l'indirizzo dal valore di un header ``From:``."""
    if not raw:
        return None
    cleaned = _CTRL_RE.sub(" ", str(raw)).strip()
    _, addr = parseaddr(cleaned)
    normalized = normalize_address(addr)
    if normalized and is_address_like(normalized):
        return normalized
    # parseaddr fallisce su forme esotiche: ultimo tentativo grezzo
    match = re.search(r"[^<>@\s]+@[^<>@\s]+", cleaned)
    if match:
        return normalize_address(match.group(0))
    return None


def address_domain(address: Optional[str]) -> Optional[str]:
    if not address or "@" not in address:
        return None
    _, _, domain = address.rpartition("@")
    return domain or None


def is_address_like(address: Optional[str]) -> bool:
    return bool(address and _ADDR_LIKE_RE.match(address))


class Policy:
    """Decide se un messaggio va valutato e quale chiave usare."""

    def __init__(self, config, whitelist=None):
        self.config = config
        if whitelist is None:
            # import ritardato: whitelist usa le funzioni di questo modulo
            from .whitelist import WhitelistError, from_config

            try:
                whitelist = from_config(config)
            except WhitelistError as exc:
                raise ConfigError("whitelist: %s" % exc)
        self.whitelist = whitelist

    # -- valutazione del contesto SMTP -----------------------------------
    def evaluate(
        self,
        sender: Optional[str],
        client_addr: Optional[str],
        auth_authen: Optional[str],
    ) -> Tuple[bool, str]:
        """Ritorna ``(da_valutare, motivo)``.

        Il motivo e' utile solo nei log quando il messaggio viene saltato.
        """
        cfg = self.config
        if not sender:
            if cfg.get_bool("policy", "skip_null_sender"):
                return False, "null-sender"
            return True, "null-sender-allowed"
        if not is_address_like(sender):
            return False, "invalid-sender"

        # whitelist: mittenti e reti esclusi esplicitamente dall'avviso
        match = self.whitelist.matches(sender, client_addr)
        if match:
            return False, "whitelist-" + match.split(":", 1)[0]

        if auth_authen and not cfg.get_bool("policy", "process_authenticated"):
            return False, "authenticated"

        local_domains = [d.lower() for d in cfg.get_list("policy", "skip_local_senders")]
        if local_domains and address_domain(sender) in local_domains:
            return False, "local-sender"

        if not client_addr:
            if cfg.get_bool("policy", "process_local"):
                return True, "local-allowed"
            return False, "local"

        network = self._network_for(client_addr)
        if network is not None:
            if cfg.get_bool("policy", "process_local"):
                return True, "trusted-network-allowed"
            return False, "trusted-network"

        return True, "external"

    def _network_for(self, client_addr: Optional[str]):
        if not client_addr:
            return None
        try:
            ip = ipaddress.ip_address(client_addr.strip().strip("[]"))
        except ValueError:
            return None
        for network in self.config.trusted_networks:
            if ip.version == network.version and ip in network:
                return network
        return None

    # -- chiave di memoria ------------------------------------------------
    def key_for(self, address: str) -> str:
        if self.config.key_scope == "domain":
            return address_domain(address) or address
        return address
