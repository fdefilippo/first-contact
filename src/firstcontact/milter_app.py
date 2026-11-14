"""Collante con pymilter (``python3-milter``).

Qui c'e' solo il minimo: raccolta dei dati della transazione SMTP,
aggiunta dell'header tecnico e applicazione dell'avviso visibile
(banner nel corpo e/o prefisso nell'oggetto). Ogni errore viene assorbito:
un problema del milter non deve mai fermare la posta.
"""

import logging
from typing import List, Optional

from .banner import BannerError, build_banner_body, is_protected
from .engine import Decider, MessageContext


def _addr_text(address) -> Optional[str]:
    """Indirizzo del client in forma testuale.

    pymilter passa una stringa per AF_UNIX e una tupla per AF_INET/AF_INET6:
    ``(ip, porta)`` oppure ``(ip, porta, flow, scope)``.
    """
    if address is None:
        return None
    if isinstance(address, (tuple, list)):
        if not address:
            return None
        address = address[0]
    text = str(address).strip()
    return text or None


def build_milter_class(milter_mod, decider: Decider, logger: logging.Logger):
    """Crea la classe milter legata al modulo ``Milter`` e al decider."""
    cont = getattr(milter_mod, "CONTINUE", 0)
    unique_id = getattr(milter_mod, "uniqueID", lambda: 0)

    class FirstContactMilter(milter_mod.Base):
        def __init__(self):
            self.id = unique_id()
            self._reset()

        # -- stato -------------------------------------------------------
        def _reset(self) -> None:
            self.client_addr: Optional[str] = None
            self.client_name: Optional[str] = None
            self.helo_name: Optional[str] = None
            self._reset_message()

        def _reset_message(self) -> None:
            """Stato che vale per un solo messaggio (una sessione SMTP puo'
            trasportare piu' messaggi: MAIL FROM apre la transazione)."""
            self.sender: Optional[str] = None
            self.recipients: List[str] = []
            self.multiple_recipients = False
            self.from_header: Optional[str] = None
            self.message_id: Optional[str] = None
            self.subject: Optional[str] = None
            self.auth_authen: Optional[str] = None
            self.content_type: Optional[str] = None
            self.content_transfer_encoding: Optional[str] = None
            self.mime_version: Optional[str] = None
            self._headers_seen = set()
            self._warned_limit = False
            self._body_chunks: List[bytes] = []
            self._body_size = 0
            self._body_overflow = False
            self._saw_body = False

        def _macro(self, name: str) -> Optional[str]:
            getter = getattr(self, "getsymval", None)
            if getter is None:
                return None
            try:
                value = getter(name)
            except Exception:  # pragma: no cover - dipende da libmilter
                return None
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        # -- callback SMTP -----------------------------------------------
        def connect(self, hostname, family, address):
            self._reset()
            self.client_name = (hostname or "").strip() or None
            self.client_addr = _addr_text(address)
            return cont

        def hello(self, name):
            self.helo_name = (name or "").strip() or None
            return cont

        def envfrom(self, sender, *args):
            self._reset_message()
            self.sender = sender
            return cont

        def envrcpt(self, recipient, *args):
            if self.recipients:
                self.multiple_recipients = True
            limit = decider.config.max_recipients
            if len(self.recipients) >= limit:
                if not self._warned_limit:
                    logger.warning(
                        "piu' di %d destinatari: i successivi vengono ignorati", limit
                    )
                    self._warned_limit = True
                return cont
            self.recipients.append(recipient)
            return cont

        def header(self, name, value):
            key = (name or "").lower()
            if key in self._headers_seen:
                return cont
            if key in (
                "from",
                "message-id",
                "subject",
                "content-type",
                "content-transfer-encoding",
                "mime-version",
            ):
                self._headers_seen.add(key)
                if key == "from":
                    self.from_header = value
                elif key == "message-id":
                    self.message_id = (value or "").strip()
                elif key == "subject":
                    self.subject = value
                elif key == "content-type":
                    self.content_type = (value or "").strip()
                elif key == "content-transfer-encoding":
                    self.content_transfer_encoding = (value or "").strip()
                else:
                    self.mime_version = (value or "").strip()
            return cont

        def body(self, chunk):
            if self._body_overflow:
                return cont
            self._saw_body = True
            limit = decider.config.max_body_bytes
            if isinstance(chunk, str):
                data = chunk.encode("latin-1", "replace")
            else:
                data = bytes(chunk)
            self._body_chunks.append(data)
            self._body_size += len(data)
            if limit and self._body_size > limit:
                self._body_overflow = True
                self._body_chunks = []
                logger.warning(
                    "corpo oltre %d byte: avviso visibile non applicato (solo header)",
                    limit,
                )
            return cont

        # -- fine messaggio ----------------------------------------------
        def eom(self):
            try:
                ctx = MessageContext(
                    sender=self.sender,
                    recipients=[r for r in self.recipients if r],
                    multiple_recipients=self.multiple_recipients,
                    from_header=self.from_header,
                    message_id=self.message_id,
                    subject=self.subject,
                    client_addr=self.client_addr or self._macro("{client_addr}"),
                    client_name=self.client_name or self._macro("{client_name}"),
                    helo=self.helo_name or self._macro("{helo_name}"),
                    auth_authen=self.auth_authen or self._macro("{auth_authen}"),
                )
                decision = decider.decide(ctx)
                for header_name, value in decision.headers:
                    self._add_header(header_name, value)
                protected = (
                    decider.config.skip_signed and is_protected(self.content_type)
                )
                if protected:
                    if decision.subject_tag or decision.warning:
                        logger.info(
                            "messaggio firmato/cifrato (Content-Type: %s): avviso "
                            "visibile non applicato, resta il solo header",
                            self.content_type,
                        )
                else:
                    if decision.subject_tag:
                        self._tag_subject(decision.subject_tag)
                    if decision.warning:
                        self._prepend_banner(decision.warning)
            except Exception:
                logger.exception("errore durante la valutazione del messaggio")
            return cont

        def _add_header(self, name: str, value: str) -> None:
            position = decider.config.header_position
            if position == "top":
                try:
                    self.addheader(name, value, 0)
                    return
                except Exception:
                    logger.debug(
                        "insheader non disponibile, accodo l'header in fondo", exc_info=True
                    )
            self.addheader(name, value)

        def _tag_subject(self, tag: str) -> None:
            try:
                if self.subject:
                    if self.subject.lower().startswith(tag.lower()):
                        return
                    self.chgheader("Subject", 1, "%s %s" % (tag, self.subject))
                else:
                    self.addheader("Subject", tag)
            except Exception:
                logger.warning("impossibile modificare l'oggetto", exc_info=True)

        def _prepend_banner(self, warning: str) -> None:
            if self._body_overflow:
                return
            if not self._saw_body:
                # nessun callback body(): non rischiare di sostituire con vuoto
                logger.debug("corpo non ricevuto: avviso visibile non applicato")
                return
            raw_body = b"".join(self._body_chunks)
            try:
                rewrite = build_banner_body(
                    original_body=raw_body,
                    warning=warning,
                    content_type=self.content_type,
                    content_transfer_encoding=self.content_transfer_encoding,
                    skip_protected=decider.config.skip_signed,
                    html_box=decider.config.html_box,
                )
            except BannerError:
                logger.warning("impossibile costruire l'avviso", exc_info=True)
                return
            if rewrite is None:
                logger.info(
                    "nessuna parte di testo modificabile (Content-Type: %s): avviso "
                    "visibile non applicato, resta il solo header",
                    self.content_type or "assente",
                )
                return
            try:
                self.replacebody(rewrite.body)
            except Exception:
                logger.warning("replacebody non disponibile", exc_info=True)

        def abort(self):
            # RSET aborts a transaction, not the SMTP connection.
            self._reset_message()
            return cont

        def close(self):
            self._reset()
            return cont

    return FirstContactMilter
