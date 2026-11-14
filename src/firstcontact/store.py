"""Stato persistente su SQLite: chi ha gia' scritto a chi.

Una sola tabella, una riga per coppia (mittente, destinatario). La
"prima volta" e' decisa in modo atomico da ``INSERT OR IGNORE``: se la
riga viene inserita, il contatto e' nuovo. Cosi' due messaggi identici
in arrivo nello stesso istante non producono due allerte.
"""

import os
from functools import wraps
import sqlite3
import threading
import time
from typing import Dict, Iterator, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL DEFAULT 0,
    messages   INTEGER NOT NULL DEFAULT 1,
    first_client TEXT,
    first_helo   TEXT,
    first_msgid  TEXT,
    PRIMARY KEY (sender, recipient)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_contacts_last_seen ON contacts (last_seen);
CREATE INDEX IF NOT EXISTS idx_contacts_sender   ON contacts (sender);
CREATE INDEX IF NOT EXISTS idx_contacts_rcpt     ON contacts (recipient);
"""

SCHEMA_VERSION = 1


class StoreError(Exception):
    pass


STATUS_NEW = "new"
STATUS_KNOWN = "known"
STATUS_RESUMED = "resumed"


def _serialized(method):
    """Keep each operation and shutdown exclusive on the shared connection."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class ContactStore:
    def __init__(self, path: str, track_stats: bool = True, timeout: float = 5.0):
        self.path = path
        self.track_stats = track_stats
        self.timeout = timeout
        # One bounded connection, also when pymilter creates new thread states.
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._ensured = False

    # -- connessioni ------------------------------------------------------
    def _ensure_dir(self) -> None:
        if self._ensured:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory, mode=0o700)
            except OSError as exc:
                if not os.path.isdir(directory):
                    raise StoreError(
                        "impossibile creare la directory %s: %s" % (directory, exc)
                    )
        self._ensured = True

    def _connect(self) -> sqlite3.Connection:
        # Callers hold _lock for the entire operation, including cursor reads.
        if self._conn is not None:
            return self._conn
        self._ensure_dir()
        conn = None
        try:
            conn = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                # Another process may be enabling WAL at this instant.
                pass
            conn.execute("PRAGMA synchronous=NORMAL")
            # Serialize first-time DDL across processes too. On older SQLite,
            # concurrent schema changes can invalidate prepared statements.
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
                conn.execute("COMMIT")
            except Exception:
                conn.rollback()
                raise
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise StoreError("impossibile aprire %s: %s" % (self.path, exc))
        self._conn = conn
        return conn

    @_serialized
    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- scrittura --------------------------------------------------------
    @_serialized
    def record_message(
        self,
        sender: str,
        recipient: str,
        first_seen: Optional[int] = None,
        first_client: Optional[str] = None,
        first_helo: Optional[str] = None,
        first_msgid: Optional[str] = None,
        dormant_seconds: int = 0,
        track: Optional[bool] = None,
    ) -> Tuple[str, int, int]:
        """Registra il messaggio e dice com'e' andata.

        Ritorna ``(stato, last_seen_precedente, first_seen_originale)`` con
        stato ``new`` (prima volta), ``resumed`` (mittente assente da almeno
        ``dormant_seconds``) oppure ``known``.

        Il caso ``resumed`` e' deciso da un ``UPDATE`` condizionato su
        ``last_seen``: se due messaggi arrivano insieme, solo il primo vede
        la riga "vecchia" e produce l'allerta.
        """
        now = int(first_seen if first_seen is not None else time.time())
        if track is None:
            track = self.track_stats
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO contacts "
                "(sender, recipient, first_seen, last_seen, messages, "
                " first_client, first_helo, first_msgid) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (sender, recipient, now, now, first_client, first_helo, first_msgid),
            )
            if cursor.rowcount == 1:
                return STATUS_NEW, now, now

            row = conn.execute(
                "SELECT first_seen, last_seen FROM contacts "
                "WHERE sender = ? AND recipient = ?",
                (sender, recipient),
            ).fetchone()
            if row is None:
                # riga sparita fra INSERT e SELECT (prune concorrente)
                return STATUS_NEW, now, now
            previous_first = int(row["first_seen"])
            previous_last = int(row["last_seen"])

            if dormant_seconds > 0 and previous_last > 0:
                cutoff = now - dormant_seconds
                if previous_last <= cutoff:
                    updated = conn.execute(
                        "UPDATE contacts SET last_seen = ?, messages = messages + ? "
                        "WHERE sender = ? AND recipient = ? AND last_seen <= ?",
                        (now, 1 if track else 0, sender, recipient, cutoff),
                    )
                    if updated.rowcount == 1:
                        return STATUS_RESUMED, previous_last, previous_first

            if track:
                conn.execute(
                    "UPDATE contacts SET last_seen = ?, messages = messages + 1 "
                    "WHERE sender = ? AND recipient = ?",
                    (now, sender, recipient),
                )
            return STATUS_KNOWN, previous_last, previous_first
        except sqlite3.Error as exc:
            raise StoreError("scrittura su %s fallita: %s" % (self.path, exc))

    def first_contact(
        self,
        sender: str,
        recipient: str,
        first_seen: Optional[int] = None,
        first_client: Optional[str] = None,
        first_helo: Optional[str] = None,
        first_msgid: Optional[str] = None,
    ) -> bool:
        """Registra il contatto; ``True`` se e' la prima volta."""
        status, _last, _first = self.record_message(
            sender,
            recipient,
            first_seen=first_seen,
            first_client=first_client,
            first_helo=first_helo,
            first_msgid=first_msgid,
        )
        return status == STATUS_NEW

    # -- lettura ----------------------------------------------------------
    @_serialized
    def lookup(self, sender: str, recipient: str) -> Optional[Dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM contacts WHERE sender = ? AND recipient = ?",
                (sender, recipient),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError("lettura da %s fallita: %s" % (self.path, exc))
        return dict(row) if row else None

    @_serialized
    def iter_contacts(
        self,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Iterator[Dict]:
        query = "SELECT * FROM contacts"
        clauses: List[str] = []
        params: List[object] = []
        if sender:
            clauses.append("sender = ?")
            params.append(sender)
        if recipient:
            clauses.append("recipient = ?")
            params.append(recipient)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY first_seen DESC LIMIT ? OFFSET ?"
        params.extend([max(1, int(limit)), max(0, int(offset))])
        conn = self._connect()
        try:
            # Materialize under the lock; do not expose a live shared cursor.
            return iter([dict(row) for row in conn.execute(query, params)])
        except sqlite3.Error as exc:
            raise StoreError("lettura da %s fallita: %s" % (self.path, exc))

    @_serialized
    def stats(self) -> Dict[str, object]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS pairs, "
                "COUNT(DISTINCT sender) AS senders, "
                "COUNT(DISTINCT recipient) AS recipients, "
                "MIN(first_seen) AS oldest, MAX(first_seen) AS newest, "
                "SUM(messages) AS messages FROM contacts"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError("lettura da %s fallita: %s" % (self.path, exc))
        return {key: row[key] for key in row.keys()} if row else {}

    @_serialized
    def integrity_check(self) -> str:
        conn = self._connect()
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error as exc:
            raise StoreError("integrity_check fallito: %s" % (exc,))

    # -- manutenzione -----------------------------------------------------
    @_serialized
    def prune(self, retention_days: int, now: Optional[int] = None, dry_run: bool = False) -> int:
        """Elimina i contatti non piu' visti da ``retention_days`` giorni."""
        if retention_days <= 0:
            return 0
        limit = int(now if now is not None else time.time()) - retention_days * 86400
        conn = self._connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE last_seen < ?", (limit,)
            ).fetchone()[0]
            if count and not dry_run:
                conn.execute("DELETE FROM contacts WHERE last_seen < ?", (limit,))
            return int(count)
        except sqlite3.Error as exc:
            raise StoreError("prune su %s fallito: %s" % (self.path, exc))

    @_serialized
    def pairs(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
        except sqlite3.Error as exc:
            raise StoreError("lettura da %s fallita: %s" % (self.path, exc))


def open_store(config) -> ContactStore:
    return ContactStore(
        config.store_path,
        track_stats=config.get_bool("store", "track_stats"),
        timeout=5.0,
    )
