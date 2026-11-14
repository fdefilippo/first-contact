"""Avviso visibile all'utente, inserito nel testo del messaggio.

L'avviso **non crea nuove parti MIME** e non cambia il ``Content-Type``:
viene aggiunto in cima alle parti ``text/plain`` e ``text/html`` gia'
presenti (anche dentro ``multipart/alternative``, ``multipart/related`` o
``multipart/mixed``). La struttura del messaggio resta quella originale,
quindi i client mostrano il testo come al solito e gli allegati restano
allegati.

Per i messaggi ``multipart/signed`` e ``multipart/encrypted`` non si tocca
nulla: qualunque modifica invaliderebbe la firma o la cifratura.

L'inserimento avviene sulle parti esistenti, senza riscriverne gli header:
si decodifica solo il transfer encoding della parte (base64, quoted-printable)
e lo si riapplica identico. E' lo stesso principio usato dai tool per i
disclaimer (per esempio alterMIME).
"""

import base64
import binascii
import codecs
import html
import quopri
import re
import textwrap
from email.message import Message
from typing import List, Optional, Tuple

DEFAULT_WARNING = (
    "ATTENZIONE: e' il primo messaggio che ricevi da {sender}.\n"
    "Non e' detto che sia pericoloso, ma presta attenzione ad allegati e link:\n"
    "non aprire file e non cliccare collegamenti se non ti fidi del mittente."
)

DEFAULT_RESUMED_WARNING = (
    "ATTENZIONE: {sender} non ti scriveva da {days} giorni.\n"
    "Verifica l'identita' del mittente prima di aprire allegati o link."
)

PROTECTED_TYPES = ("multipart/signed", "multipart/encrypted", "application/pkcs7-mime")

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_BODY_TAG_RE = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
MAX_DEPTH = 10

# Stile del box: valori semplici, supportati dai client di posta.
BOX_BACKGROUND = "#ffff00"   # giallo
BOX_TEXT = "#000000"         # nero
BOX_BORDER = "2px dashed #000000"  # bordo tratteggiato nero
BOX_FONT = "Arial,Helvetica,sans-serif"
BOX_WIDTH = 74  # larghezza della cornice nel fallback testo


class BannerError(Exception):
    pass


def sanitize_text(value: Optional[str], maxlen: int = 120) -> str:
    """Rende un valore adatto a comparire nel testo dell'avviso."""
    if not value:
        return ""
    text = _CTRL_RE.sub(" ", str(value))
    text = " ".join(text.split())
    if len(text) > maxlen:
        text = text[: maxlen - 3] + "..."
    return text


def render_warning(
    template: str,
    sender: str,
    recipients: List[str],
    first_seen: str,
    client: Optional[str] = None,
    days: Optional[int] = None,
    last_seen: Optional[str] = None,
) -> str:
    """Sostituisce i segnaposto e impagina il testo a 78 colonne.

    I segnaposto sono sostituiti con ``str.replace`` (non ``format``) perche'
    il template puo' contenere graffe senza far fallire nulla.
    Sono disponibili ``{sender}``, ``{recipient}``, ``{recipients}``,
    ``{first_seen}``, ``{client}`` e, per il mittente che ricompare,
    ``{days}`` e ``{last_seen}``.
    """
    recipient_text = ", ".join(sanitize_text(r) for r in recipients if r)
    replacements = {
        "{sender}": sanitize_text(sender),
        "{recipient}": sanitize_text(recipients[0]) if recipients else "",
        "{recipients}": recipient_text,
        "{first_seen}": sanitize_text(first_seen, maxlen=40),
        "{client}": sanitize_text(client),
        "{days}": str(int(days)) if days is not None else "",
        "{last_seen}": sanitize_text(last_seen, maxlen=40),
    }
    text = template or DEFAULT_WARNING
    for token, value in replacements.items():
        text = text.replace(token, value)
    wrapped = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            wrapped.append("")
            continue
        wrapped.append(
            textwrap.fill(paragraph.strip(), width=78, break_long_words=False, break_on_hyphens=False)
        )
    return "\n".join(wrapped).strip()


def _dashed_rule(width: int = BOX_WIDTH) -> str:
    """Riga tratteggiata per il fallback in solo testo."""
    return ("- " * (width // 2)).rstrip()


def build_plain_banner(warning: str) -> str:
    """Versione text/plain: stessa allerta dentro una cornice tratteggiata."""
    lines = []
    for paragraph in (warning or "").splitlines():
        if not paragraph.strip():
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                paragraph.strip(),
                width=BOX_WIDTH - 2,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    rule = _dashed_rule()
    return rule + "\n" + "\n".join(lines) + "\n" + rule


def build_html_banner(warning: str) -> str:
    """Versione text/html: box giallo, testo nero, bordo tratteggiato nero.

    Il testo e' passato da ``html.escape``: un mittente con caratteri speciali
    non puo' iniettare markup nel messaggio. Il risultato e' un semplice
    ``table``, da inserire dentro il corpo HTML esistente.
    """
    body = "<br>\n".join(
        html.escape(line) for line in (warning or "").splitlines()
    )
    style = (
        "background-color:%s;color:%s;border:%s;padding:12px;"
        "font-family:%s;font-size:14px;line-height:1.45;"
        % (BOX_BACKGROUND, BOX_TEXT, BOX_BORDER, BOX_FONT)
    )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;margin:0 0 16px 0;">\n'
        '<tr><td bgcolor="#ffff00" style="' + style + '">\n'
        + body
        + "\n</td></tr>\n</table>"
    )


def is_protected(content_type: Optional[str]) -> bool:
    if not content_type:
        return False
    lowered = content_type.strip().lower()
    return any(lowered.startswith(prefix) for prefix in PROTECTED_TYPES)


class BannerRewrite:
    """Risultato della riscrittura: nuovo corpo e numero di parti toccate."""

    def __init__(self, body: bytes, parts: int = 0):
        self.body = body
        self.parts = parts


# --------------------------------------------------------------------------
# struttura MIME: si modificano solo i payload testuali, senza rigenerare
# boundary, header o parti non testuali
# --------------------------------------------------------------------------
def _content_info(content_type: Optional[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
    message = Message()
    message["Content-Type"] = content_type or "text/plain"
    try:
        return (
            message.get_content_maintype(),
            message.get_content_subtype(),
            message.get_boundary(),
            message.get_content_charset(),
        )
    except Exception:
        return "text", "plain", None, None


def _parse_headers(raw: bytes) -> dict:
    headers = {}
    last = None
    for line in raw.decode("latin-1").splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and last:
            headers[last] = (headers[last] + " " + line.strip()).strip()
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        last = name.strip().lower()
        headers[last] = value.strip()
    return headers


def _split_part(part: bytes) -> Optional[Tuple[bytes, bytes, bytes]]:
    """Divide una parte in header, separatore e payload.

    Restituisce ``None`` se non c'e' la riga vuota che separa gli header.
    """
    for sep in (b"\r\n\r\n", b"\n\n"):
        index = part.find(sep)
        if index >= 0:
            return part[:index], sep, part[index + len(sep):]
    return None


def _split_multipart(body: bytes, boundary: bytes):
    """Divide un corpo multipart conservando delimiter, preambolo ed epilogo.

    Restituisce ``(preamble, delimiters, parts, epilogue)`` oppure ``None`` se
    la struttura non e' utilizzabile. ``delimiters`` contiene la riga di
    apertura di ogni parte piu' quella di chiusura.
    """
    marker = b"--" + boundary
    closing = marker + b"--"
    lines = body.splitlines(keepends=True)
    preamble: List[bytes] = []
    delimiters: List[bytes] = []
    parts: List[bytes] = []
    epilogue: List[bytes] = []
    current: Optional[List[bytes]] = None
    found_closing = False

    for index, line in enumerate(lines):
        stripped = line.rstrip(b"\r\n").rstrip()
        if stripped == marker or stripped == closing:
            if current is not None:
                parts.append(b"".join(current))
                current = None
            delimiters.append(line)
            if stripped == closing:
                epilogue = lines[index + 1:]
                found_closing = True
                break
        elif not delimiters:
            preamble.append(line)
        elif current is None:
            current = [line]
        else:
            current.append(line)

    if not found_closing or not delimiters:
        return None
    if len(parts) != len(delimiters) - 1:
        return None
    return b"".join(preamble), delimiters, parts, b"".join(epilogue)


def _decode_cte(payload: bytes, content_transfer_encoding: Optional[str]) -> Optional[bytes]:
    value = (content_transfer_encoding or "").strip().lower()
    if value == "base64":
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            return None
    if value == "quoted-printable":
        return quopri.decodestring(payload)
    return payload


def _with_newline(data: bytes, newline: bytes) -> bytes:
    """Porta tutte le fini riga a ``newline`` (CRLF o LF), senza raddoppiarle."""
    data = data.replace(b"\r\n", b"\n")
    if newline == b"\r\n":
        data = data.replace(b"\n", b"\r\n")
    return data


def _encode_cte(payload: bytes, content_transfer_encoding: Optional[str], newline: bytes) -> bytes:
    # quopri usa gia' CRLF se l'input e' CRLF (soft break "=\r\n"), base64 usa
    # sempre LF: si normalizza invece di sostituire alla cieca "\n" con "\r\n",
    # che produrrebbe "=\r\r\n" (soft break non valido, "=" visibile nel testo).
    value = (content_transfer_encoding or "").strip().lower()
    if value == "base64":
        return _with_newline(base64.encodebytes(payload), newline)
    if value == "quoted-printable":
        return _with_newline(quopri.encodestring(payload), newline)
    return payload


def _encode_text(text: str, charset: Optional[str]) -> bytes:
    encoding = "utf-8"
    if charset:
        try:
            codecs.lookup(charset)
            encoding = charset
        except LookupError:
            pass
    return text.encode(encoding, "replace")


def _insert_html(payload: bytes, banner: bytes, newline: bytes) -> bytes:
    """Inserisce il box subito dopo ``<body>``, se c'e'; altrimenti in cima."""
    match = _BODY_TAG_RE.search(payload)
    if match:
        position = match.end()
        return payload[:position] + newline + banner + newline + payload[position:]
    return banner + newline + newline + payload


def _rewrite_content(
    payload: bytes,
    content_type: Optional[str],
    content_transfer_encoding: Optional[str],
    disposition: Optional[str],
    warning: str,
    html_box: bool,
    depth: int,
    skip_protected: bool = True,
) -> Tuple[bytes, int]:
    """Riscrive i payload testuali; restituisce (payload, parti modificate)."""
    if depth > MAX_DEPTH:
        return payload, 0

    maintype, subtype, boundary, charset = _content_info(content_type)

    if maintype == "multipart":
        if (skip_protected and subtype in ("signed", "encrypted")) or not boundary:
            return payload, 0
        parts_info = _split_multipart(payload, boundary.encode("ascii", "replace"))
        if parts_info is None:
            return payload, 0
        preamble, delimiters, parts, epilogue = parts_info
        changed = 0
        new_parts = []
        for part in parts:
            new_part, count = _rewrite_part(
                part, warning, html_box, depth + 1, skip_protected
            )
            new_parts.append(new_part)
            changed += count
        if not changed:
            return payload, 0
        rebuilt = [preamble]
        for index, part in enumerate(new_parts):
            rebuilt.append(delimiters[index])
            rebuilt.append(part)
        rebuilt.append(delimiters[len(new_parts)])
        rebuilt.append(epilogue)
        return b"".join(rebuilt), changed

    if maintype == "message":
        # messaggio allegato (message/rfc822): non si tocca
        return payload, 0

    if maintype != "text" or subtype not in ("plain", "html"):
        return payload, 0

    if "attachment" in (disposition or "").lower():
        return payload, 0

    if subtype == "html" and not html_box:
        return payload, 0

    if charset:
        # The rewriter searches HTML tags and inserts CR/LF as ASCII bytes.
        # UTF-16/32 (and other incompatible codecs) must remain untouched.
        try:
            if "\r\n<body>".encode(charset) != b"\r\n<body>":
                return payload, 0
        except (LookupError, UnicodeError):
            return payload, 0

    data = _decode_cte(payload, content_transfer_encoding)
    if data is None:
        return payload, 0

    banner = build_html_banner(warning) if subtype == "html" else build_plain_banner(warning)
    newline = b"\r\n" if b"\r\n" in payload else b"\n"
    encoded_banner = _with_newline(_encode_text(banner, charset), newline)

    if subtype == "html":
        changed_data = _insert_html(data, encoded_banner, newline)
    else:
        changed_data = encoded_banner + newline + newline + data

    return _encode_cte(changed_data, content_transfer_encoding, newline), 1


def _rewrite_part(
    part: bytes,
    warning: str,
    html_box: bool,
    depth: int,
    skip_protected: bool = True,
) -> Tuple[bytes, int]:
    split = _split_part(part)
    if split is None:
        return part, 0
    headers_raw, separator, payload = split
    headers = _parse_headers(headers_raw)

    new_payload, changed = _rewrite_content(
        payload,
        headers.get("content-type", "text/plain"),
        headers.get("content-transfer-encoding"),
        headers.get("content-disposition"),
        warning,
        html_box,
        depth,
        skip_protected,
    )
    if not changed:
        return part, 0
    return headers_raw + separator + new_payload, changed


def build_banner_body(
    original_body: bytes,
    warning: str,
    content_type: Optional[str] = None,
    content_transfer_encoding: Optional[str] = None,
    skip_protected: bool = True,
    html_box: bool = True,
) -> Optional[BannerRewrite]:
    """Aggiunge l'avviso alle parti testuali; ``None`` se non c'e' nulla da fare."""
    if skip_protected and is_protected(content_type):
        return None
    if not (warning or "").strip():
        return None

    body, parts = _rewrite_content(
        original_body or b"",
        content_type or "text/plain; charset=utf-8",
        content_transfer_encoding,
        "",
        warning,
        html_box,
        0,
        skip_protected,
    )
    if not parts:
        return None
    return BannerRewrite(body=body, parts=parts)


def preview(rewrite: BannerRewrite, limit: int = 6) -> List[str]:
    """Prime righe del nuovo corpo, per ``simulate`` e i test."""
    text = rewrite.body.decode("utf-8", "replace")
    return text.splitlines()[:limit]
