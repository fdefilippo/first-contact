"""Costruzione dell'header di allerta, sicura per RFC 5322."""

import re
import string
from typing import Iterable, List, Optional, Tuple

# Caratteri ammessi senza escaping nel valore (testo stampabile ASCII
# esclusi i separatori strutturali dell'header).
_SAFE_CHARS = set(string.ascii_letters + string.digits + "!#$%&'*+-./=?^_`{|}~@[]:,<>")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_VALUE_LEN = 900  # RFC 5322: massimo 998 ottetti per riga, con margine


def sanitize_value(value: str, maxlen: int = 200) -> str:
    """Riduce un valore arbitrario a testo ASCII stampabile su una riga."""
    if value is None:
        return ""
    text = _CTRL_RE.sub(" ", str(value))
    text = " ".join(text.split())
    if len(text) > maxlen:
        text = text[: maxlen - 3] + "..."
    return text


def encode_token(value: str, maxlen: int = 320) -> str:
    """Percent-encoding dei caratteri non sicuri (indirizzi esotici inclusi).

    Garantisce che l'header resti ASCII su una riga e non possa essere
    spezzato o iniettato.
    """
    text = sanitize_value(value, maxlen=maxlen)
    out = []
    for char in text:
        if char in _SAFE_CHARS:
            out.append(char)
        elif char == " ":
            out.append("%20")
        else:
            # i caratteri non ASCII diventano la loro codifica UTF-8
            for byte in char.encode("utf-8"):
                out.append("%%%02X" % byte)
    return "".join(out)


def fold_header(value: str, width: int = 76) -> str:
    """Piega un valore header su piu' righe usando la continuazione standard."""
    if len(value) <= width:
        return value
    parts: List[str] = []
    current = ""
    for chunk in value.split("; "):
        piece = chunk if not current else "; " + chunk
        if current and len(current) + len(piece) > width:
            parts.append(current + ";")
            current = chunk
        else:
            current += piece
    if current:
        parts.append(current)
    folded = parts[0]
    for part in parts[1:]:
        folded += "\n\t" + part
    return folded


def build_first_contact_header(
    header_name: str,
    sender: str,
    recipient: str,
    first_seen: str,
    key_source: str,
    key_scope: str,
    client_addr: Optional[str] = None,
    helo: Optional[str] = None,
    message_id: Optional[str] = None,
    kind: str = "first-message",
    last_seen: Optional[str] = None,
    days: Optional[int] = None,
) -> Tuple[str, str]:
    """Restituisce ``(nome, valore)`` dell'header da aggiungere.

    ``kind`` distingue il primo contatto (``first-message``) dal mittente che
    ricompare dopo un'assenza (``resumed``); in quest'ultimo caso si aggiungono
    ``last-seen`` e ``days``.
    """
    fields = [
        kind,
        "sender=" + encode_token(sender),
        "recipient=" + encode_token(recipient),
        "first-seen=" + encode_token(first_seen, maxlen=40),
    ]
    if last_seen:
        fields.append("last-seen=" + encode_token(last_seen, maxlen=40))
    if days is not None:
        fields.append("days=%d" % int(days))
    fields.extend(
        [
            "source=" + encode_token(key_source, maxlen=20),
            "scope=" + encode_token(key_scope, maxlen=20),
        ]
    )
    if client_addr:
        fields.append("client=" + encode_token(client_addr, maxlen=64))
    if helo:
        fields.append("helo=" + encode_token(helo.lower(), maxlen=64))
    if message_id:
        fields.append("msgid=" + encode_token(message_id, maxlen=160))
    value = "; ".join(fields)
    value = value[:MAX_VALUE_LEN]
    return header_name, fold_header(value)


def join_headers(headers: Iterable[Tuple[str, str]]) -> str:
    """Rappresentazione leggibile (usata da ``simulate`` e dai test)."""
    return "\n".join("%s: %s" % (name, value) for name, value in headers)
