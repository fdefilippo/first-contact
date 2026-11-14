"""Logica di decisione: dato un messaggio, quali header aggiungere.

Nessuna dipendenza da pymilter: si testa con normali unit test.
"""

import datetime
import logging
import time
from typing import List, Optional, Tuple

from .banner import render_warning
from .headers import build_first_contact_header
from .policy import Policy, from_header_address, is_address_like, normalize_address
from .store import STATUS_KNOWN, STATUS_NEW, ContactStore


class MessageContext(object):
    """Dati raccolti dal milter durante la transazione SMTP.

    Classe semplice invece di ``dataclasses``: cosi' il codice gira anche
    sul Python 3.6 di Enterprise Linux 8.
    """

    def __init__(
        self,
        sender: Optional[str] = None,
        recipients: Optional[List[str]] = None,
        from_header: Optional[str] = None,
        message_id: Optional[str] = None,
        subject: Optional[str] = None,
        client_addr: Optional[str] = None,
        client_name: Optional[str] = None,
        helo: Optional[str] = None,
        auth_authen: Optional[str] = None,
        received_at: Optional[float] = None,
        multiple_recipients: bool = False,
    ) -> None:
        self.sender = sender
        self.recipients = recipients if recipients is not None else []
        self.from_header = from_header
        self.message_id = message_id
        self.subject = subject
        self.client_addr = client_addr
        self.client_name = client_name
        self.helo = helo
        self.auth_authen = auth_authen
        self.received_at = received_at
        # Include recipients beyond the processing cap in the privacy decision.
        self.multiple_recipients = multiple_recipients or len(self.recipients) > 1


class Decision(object):
    """Esito della valutazione per un messaggio."""

    def __init__(
        self,
        headers: Optional[List[Tuple[str, str]]] = None,
        new_pairs: Optional[List[Tuple[str, str]]] = None,
        resumed_pairs: Optional[List[Tuple[str, str, int, str, str]]] = None,
        reason: str = "",
        observed_only: bool = False,
        warning: Optional[str] = None,
        subject_tag: Optional[str] = None,
    ) -> None:
        self.headers = headers if headers is not None else []
        self.new_pairs = new_pairs if new_pairs is not None else []
        # (mittente, destinatario, giorni di assenza, ultimo contatto ISO,
        #  primo contatto ISO)
        self.resumed_pairs = resumed_pairs if resumed_pairs is not None else []
        self.reason = reason
        self.observed_only = observed_only
        # testo dell'avviso visibile (banner nel corpo), se previsto
        self.warning = warning
        # prefisso da mettere nell'oggetto, se previsto
        self.subject_tag = subject_tag


class Decider:
    def __init__(self, config, store: ContactStore, logger: Optional[logging.Logger] = None):
        self.config = config
        self.store = store
        self.policy = Policy(config)
        self.log = logger or logging.getLogger("firstcontact")

    def decide(self, ctx: MessageContext) -> Decision:
        envelope_sender = normalize_address(ctx.sender)
        sender_key = self._sender_key(ctx, envelope_sender)

        # Null-envelope exclusion is independent of the selected memory key.
        if not envelope_sender and self.config.get_bool("policy", "skip_null_sender"):
            return Decision(reason="null-sender")

        allowed, reason = self.policy.evaluate(
            envelope_sender or sender_key, ctx.client_addr, ctx.auth_authen
        )
        if not allowed:
            self.log.debug(
                "skip reason=%s sender=%s client=%s",
                reason,
                sender_key or "-",
                ctx.client_addr or "-",
            )
            return Decision(reason=reason)

        if not sender_key or not is_address_like(sender_key):
            return Decision(reason="invalid-sender")

        now = ctx.received_at if ctx.received_at is not None else time.time()
        first_seen = _iso_utc(now)
        decision = Decision(reason=reason, observed_only=self.config.observe_only)
        seen = set()

        dormant_days = self.config.dormant_days
        dormant_seconds = dormant_days * 86400 if dormant_days > 0 else 0
        # se la regola e' attiva, last_seen va aggiornato comunque, altrimenti
        # un mittente abituale sembrerebbe assente dopo la soglia
        track = self.config.track_stats or dormant_seconds > 0

        for raw_recipient in ctx.recipients:
            recipient = normalize_address(raw_recipient)
            if not recipient or not is_address_like(recipient):
                continue
            if recipient in seen:
                continue
            seen.add(recipient)

            sender_memory_key = self.policy.key_for(sender_key)
            status, previous_last, original_first = self.store.record_message(
                sender_memory_key,
                recipient,
                first_seen=int(now),
                first_client=ctx.client_addr,
                first_helo=ctx.helo,
                first_msgid=ctx.message_id,
                dormant_seconds=dormant_seconds,
                track=track,
            )
            if status == STATUS_KNOWN:
                self.log.debug("known sender=%s recipient=%s", sender_key, recipient)
                continue

            if status == STATUS_NEW:
                decision.new_pairs.append((sender_key, recipient))
                self.log.info(
                    "first-contact sender=%s recipient=%s client=%s%s",
                    sender_key,
                    recipient,
                    ctx.client_addr or "-",
                    " (observe-only: header non aggiunto)"
                    if decision.observed_only
                    else "",
                )
                if decision.observed_only:
                    continue
                decision.headers.append(
                    build_first_contact_header(
                        header_name=self.config.header_name,
                        sender=sender_key,
                        recipient=None if ctx.multiple_recipients else recipient,
                        first_seen=first_seen,
                        key_source=self.config.key_source,
                        key_scope=self.config.key_scope,
                        client_addr=ctx.client_addr,
                        helo=ctx.helo,
                        message_id=ctx.message_id,
                    )
                )
                continue

            # mittente che ricompare dopo un'assenza
            days = max(1, int((now - previous_last) // 86400))
            last_seen = _iso_utc(previous_last)
            original_first_seen = _iso_utc(original_first)
            decision.resumed_pairs.append(
                (sender_key, recipient, days, last_seen, original_first_seen)
            )
            self.log.info(
                "resumed sender=%s recipient=%s client=%s days=%d last-seen=%s%s",
                sender_key,
                recipient,
                ctx.client_addr or "-",
                days,
                last_seen,
                " (observe-only: header non aggiunto)"
                if decision.observed_only
                else "",
            )
            if decision.observed_only:
                continue
            decision.headers.append(
                build_first_contact_header(
                    header_name=self.config.header_name,
                    sender=sender_key,
                    recipient=None if ctx.multiple_recipients else recipient,
                    first_seen=_iso_utc(original_first),
                    key_source=self.config.key_source,
                    key_scope=self.config.key_scope,
                    client_addr=ctx.client_addr,
                    helo=ctx.helo,
                    message_id=ctx.message_id,
                    kind="resumed",
                    last_seen=last_seen,
                    days=days,
                )
            )

        if not decision.new_pairs and not decision.resumed_pairs:
            self.log.debug("no new contacts sender=%s", sender_key)
            return decision

        if not decision.observed_only:
            self._add_visible_warning(decision, ctx, sender_key, first_seen)
        return decision

    def _add_visible_warning(
        self, decision: Decision, ctx: MessageContext, sender_key: str, first_seen: str
    ) -> None:
        """Prepara banner e prefisso oggetto secondo [banner] mode."""
        mode = self.config.banner_mode
        if mode in ("subject", "both") and self.config.subject_tag:
            decision.subject_tag = self.config.subject_tag
        if mode not in ("body", "both"):
            return

        sections = []
        if decision.new_pairs:
            sections.append(
                render_warning(
                    self.config.banner_text,
                    sender=sender_key,
                    recipients=[] if ctx.multiple_recipients else [pair[1] for pair in decision.new_pairs],
                    first_seen=first_seen,
                    client=ctx.client_addr,
                )
            )
        for sender, recipient, days, last_seen, original_first_seen in decision.resumed_pairs:
            sections.append(
                render_warning(
                    self.config.banner_text_resumed,
                    sender=sender,
                    recipients=[] if ctx.multiple_recipients else [recipient],
                    first_seen=original_first_seen,
                    client=ctx.client_addr,
                    days=days,
                    last_seen=last_seen,
                )
            )
        text = "\n\n".join(section for section in sections if section)
        decision.warning = text or None
        if sections and not text:
            self.log.warning("template del banner vuoto: avviso non aggiunto")

    def _sender_key(self, ctx: MessageContext, envelope_sender: Optional[str]) -> Optional[str]:
        if self.config.key_source == "from":
            return from_header_address(ctx.from_header) or envelope_sender
        return envelope_sender


def _iso_utc(timestamp: float) -> str:
    moment = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
