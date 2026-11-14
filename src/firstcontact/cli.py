"""Interfaccia a riga di comando: serve, check, list, stats, prune, doctor, simulate."""

import argparse
import datetime
from contextlib import contextmanager
import pwd
import logging
import logging.handlers
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Tuple

from . import __version__
from .banner import BannerError, build_banner_body, preview
from .config import Config, ConfigError
from .engine import Decider, MessageContext
from .headers import join_headers
from .milter_app import build_milter_class
from .policy import Policy, from_header_address, normalize_address
from .store import ContactStore, StoreError, open_store

LOG_FORMAT = "first-contact[%(process)d]: %(message)s"
EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_CONFIG = 2
EXIT_DEPENDENCY = 3

DOCTOR_RESULTS: List[Tuple[str, str]] = []


# --------------------------------------------------------------------------
# utilita'
# --------------------------------------------------------------------------
def _iso(timestamp) -> str:
    if timestamp in (None, ""):
        return "-"
    moment = datetime.datetime.fromtimestamp(int(timestamp), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%SZ")


def configure_logging(config: Config, stderr: bool = True) -> logging.Logger:
    logger = logging.getLogger("firstcontact")
    logger.setLevel(getattr(logging, config.get("logging", "level").upper()))
    logger.handlers = []
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT)

    if config.get_bool("logging", "syslog"):
        facility_name = config.get("logging", "syslog_facility").strip().lower()
        facility = logging.handlers.SysLogHandler.facility_names.get(
            facility_name, logging.handlers.SysLogHandler.LOG_MAIL
        )
        try:
            handler = logging.handlers.SysLogHandler(
                address="/dev/log", facility=facility
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except Exception:  # pragma: no cover - dipende dal sistema
            if stderr:
                sys.stderr.write(
                    "attenzione: syslog non disponibile, log solo su stderr\n"
                )

    log_file = config.get("logging", "file").strip()
    if log_file:
        try:
            handler = logging.FileHandler(log_file)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except OSError as exc:
            sys.stderr.write("attenzione: impossibile scrivere %s: %s\n" % (log_file, exc))

    if stderr:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _load_config(args) -> Config:
    config = Config.load(args.config)
    for warning in config.warnings:
        sys.stderr.write("config: %s\n" % warning)
    return config


def _open_store(config: Config, readonly: bool = False) -> ContactStore:
    return open_store(config, readonly=readonly)


@contextmanager
def _service_identity(command):
    """Run administrative queries as the service user, including WAL sidecars.

    SQLite mode=ro can still create -wal/-shm files. A root diagnostic must
    not leave these owned by root. This CLI path runs before starting threads.
    """
    if command not in ("doctor", "check", "list", "stats", "prune") or os.geteuid() != 0:
        yield
        return
    try:
        account = pwd.getpwnam("firstcontact")
    except KeyError:
        # Development/source checkout without an installed service account.
        yield
        return
    old_uid, old_gid, old_groups = os.geteuid(), os.getegid(), os.getgroups()
    try:
        os.initgroups(account.pw_name, account.pw_gid)
        os.setegid(account.pw_gid)
        os.seteuid(account.pw_uid)
        yield
    finally:
        os.seteuid(old_uid)
        os.setegid(old_gid)
        os.setgroups(old_groups)


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def cmd_serve(args) -> int:
    config = _load_config(args)
    logger = configure_logging(config)
    try:
        import Milter
    except ImportError:
        sys.stderr.write(
            "pymilter non installato. Su Debian: apt-get install python3-milter\n"
        )
        return EXIT_DEPENDENCY

    socket_path = config.socket_path
    if socket_path:
        parent = os.path.dirname(socket_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, mode=0o750)
            logger.info("creata directory %s", parent)
        os.umask(0o007)

    store = _open_store(config)
    decider = Decider(config, store, logger)
    milter_class = build_milter_class(Milter, decider, logger)

    logger.info(
        "avvio milter: socket=%s db=%s header=%s posizione=%s observe_only=%s",
        config.socket,
        config.store_path,
        config.header_name,
        config.header_position,
        config.observe_only,
    )
    Milter.factory = milter_class
    try:
        Milter.runmilter(
            "first-contact", config.socket, config.get_int("milter", "timeout")
        )
    except KeyboardInterrupt:
        logger.info("interruzione richiesta")
    finally:
        store.close()
        if socket_path and os.path.exists(socket_path):
            try:
                os.unlink(socket_path)
            except OSError:
                logger.warning("impossibile rimuovere %s", socket_path)
    return EXIT_OK


def _key_for(config: Config, address: str) -> str:
    return Policy(config).key_for(address)


# --------------------------------------------------------------------------
# check / list / stats / prune
# --------------------------------------------------------------------------
def cmd_check(args) -> int:
    config = _load_config(args)
    with _open_store(config, readonly=True) as store:
        sender = (
            from_header_address(args.sender)
            if args.from_header
            else normalize_address(args.sender)
        )
        recipient = normalize_address(args.recipient)
        if not sender or not recipient:
            sys.stderr.write("indirizzo mittente o destinatario non valido\n")
            return EXIT_CONFIG
        sender_key = _key_for(config, sender)

        policy = Policy(config)
        match = policy.whitelist.matches(sender, args.client)
        if match:
            print(
                "ESCLUSO sender=%s: in whitelist (%s), nessun avviso verrebbe aggiunto"
                % (sender, match)
            )
            return EXIT_OK
        if args.client:
            print("whitelist      : nessuna corrispondenza (client=%s)" % args.client)

        row = store.lookup(sender_key, recipient)
        dormant_days = config.dormant_days
        if row:
            last_seen = int(row["last_seen"] or row["first_seen"])
            days = max(0, int((time.time() - last_seen) // 86400))
            note = ""
            if dormant_days > 0:
                if days >= dormant_days:
                    note = " (soglia %dgg superata: avviso 'resumed')" % dormant_days
                else:
                    note = " (soglia %dgg: ancora nessun avviso)" % dormant_days
            print(
                "KNOWN  sender=%s recipient=%s primo=%s ultimo=%s messaggi=%s assenza=%dgg%s"
                % (
                    row["sender"],
                    row["recipient"],
                    _iso(row["first_seen"]),
                    _iso(row["last_seen"]),
                    row["messages"],
                    days,
                    note,
                )
            )
            return EXIT_OK
        print(
            "NEW    sender=%s recipient=%s (nessun contatto precedente)"
            % (sender_key, recipient)
        )
        return EXIT_OK


def cmd_list(args) -> int:
    config = _load_config(args)
    with _open_store(config, readonly=True) as store:
        sender = normalize_address(args.sender_filter) if args.sender_filter else None
        recipient = normalize_address(args.recipient_filter) if args.recipient_filter else None
        rows = list(
            store.iter_contacts(
                sender=sender, recipient=recipient, limit=args.limit, offset=args.offset
            )
        )
        if not rows:
            print("nessun contatto memorizzato")
            return EXIT_OK
        print("%-40s %-40s %-20s %-20s %8s" % ("MITTENTE", "DESTINATARIO", "PRIMO", "ULTIMO", "MSG"))
        for row in rows:
            print(
                "%-40s %-40s %-20s %-20s %8d"
                % (
                    row["sender"],
                    row["recipient"],
                    _iso(row["first_seen"]),
                    _iso(row["last_seen"]),
                    row["messages"],
                )
            )
        return EXIT_OK


def cmd_stats(args) -> int:
    config = _load_config(args)
    with _open_store(config, readonly=True) as store:
        data = store.stats()
        print("database      : %s" % config.store_path)
        print("coppie        : %s" % (data.get("pairs") or 0))
        print("mittenti      : %s" % (data.get("senders") or 0))
        print("destinatari   : %s" % (data.get("recipients") or 0))
        print("messaggi      : %s" % (data.get("messages") or 0))
        print("primo contatto: %s" % _iso(data.get("oldest")))
        print("ultimo        : %s" % _iso(data.get("newest")))
        return EXIT_OK


def cmd_prune(args) -> int:
    config = _load_config(args)
    days = args.days if args.days is not None else config.retention_days
    if days <= 0:
        print("retention_days non impostata (o 0): nessuna cancellazione")
        return EXIT_OK
    with _open_store(config, readonly=args.dry_run) as store:
        removed = store.prune(days, dry_run=args.dry_run)
        print(
            "%s %d (contatti non visti da oltre %d giorni)"
            % ("da eliminare:" if args.dry_run else "eliminati:", removed, days)
        )
        return EXIT_OK


# --------------------------------------------------------------------------
# simulate
# --------------------------------------------------------------------------
def cmd_simulate(args) -> int:
    config = _load_config(args)
    # nessun log: una simulazione non deve sporcare /var/log/mail.log
    logger = logging.getLogger("firstcontact.simulate")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    db_path = args.db
    temporary = None
    if not db_path:
        handle, db_path = tempfile.mkstemp(prefix="first-contact-sim-", suffix=".db")
        os.close(handle)
        temporary = db_path
    store = ContactStore(
        db_path, track_stats=config.get_bool("store", "track_stats"), timeout=2.0
    )
    try:
        from_header = message_id = subject = None
        raw_body = None
        content_type = content_transfer_encoding = None
        sender = args.sender
        if args.eml:
            with open(args.eml, "rb") as handle:
                raw = handle.read()
            message = _parse_message(raw)
            from_header = message.get("From")
            message_id = message.get("Message-ID")
            subject = message.get("Subject")
            content_type = message.get("Content-Type")
            content_transfer_encoding = message.get("Content-Transfer-Encoding")
            raw_body = _split_body(raw)
            if not sender:
                sender = from_header_address(from_header)
        if args.known_days is not None:
            # pre-popola il database temporaneo fingendo un contatto passato:
            # serve a provare la regola "mittente assente da N giorni".
            seeded_sender = normalize_address(sender)
            seeded_recipient = normalize_address(args.recipient)
            if seeded_sender and seeded_recipient:
                seed_time = int(time.time()) - int(args.known_days) * 86400
                store.first_contact(
                    _key_for(config, seeded_sender),
                    seeded_recipient,
                    first_seen=seed_time,
                )
        decider = Decider(config, store, logger)
        decision = decider.decide(
            MessageContext(
                sender=sender,
                recipients=[args.recipient],
                from_header=from_header,
                message_id=message_id,
                subject=subject,
                client_addr=args.client,
                helo=args.helo,
                received_at=None,
            )
        )
        print("socket         : %s" % config.socket)
        print("database       : %s%s" % (db_path, " (temporaneo)" if temporary else ""))
        print("mittente       : %s" % (normalize_address(sender) or "-"))
        print("destinatario   : %s" % normalize_address(args.recipient))
        print("client         : %s" % (args.client or "-"))
        if args.known_days is not None:
            print(
                "contatto finto : ultimo messaggio %d giorni fa (regola assenza)"
                % args.known_days
            )
        print("esito          : %s" % decision.reason)
        if decision.new_pairs:
            print("tipo allerta   : primo contatto")
        elif decision.resumed_pairs:
            _sender, _rcpt, days, last_seen, _first = decision.resumed_pairs[0]
            print(
                "tipo allerta   : mittente tornato dopo %d giorni (ultimo contatto %s)"
                % (days, last_seen)
            )
        if decision.reason.startswith("whitelist-"):
            print("whitelist      : mittente escluso, nessun avviso")
        if decision.observed_only:
            print("modalita'       : observe_only (nessun avviso aggiunto)")
        if not decision.headers:
            print("header         : nessuno")
        else:
            print("header da aggiungere:")
            print("  " + join_headers(decision.headers).replace("\n", "\n  "))

        _print_visible_warning(decision, config, raw_body, content_type,
                               content_transfer_encoding)
        return EXIT_OK
    finally:
        store.close()
        if temporary:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(temporary + suffix)
                except OSError:
                    pass


def _print_visible_warning(decision, config, raw_body, content_type, cte) -> None:
    """Mostra cosa vedrebbe l'utente (banner e/o oggetto)."""
    if decision.subject_tag:
        print("oggetto        : %s <oggetto originale>" % decision.subject_tag)
    if not decision.warning:
        if config.banner_mode == "off":
            print("avviso visibile: disattivato ([banner] mode = off)")
        elif config.banner_mode == "subject" and not decision.subject_tag:
            print("avviso visibile: solo oggetto, ma subject_tag e' vuoto")
        return
    print("avviso visibile: banner nelle parti text/plain e text/html")
    if config.html_box:
        print(
            "stile          : box HTML giallo, testo nero, bordo tratteggiato "
            "(fallback text/plain)"
        )
    else:
        print("stile          : solo testo, cornice tratteggiata")
    if raw_body is None:
        print("--- testo del banner ---")
        print(decision.warning)
        return
    try:
        rewrite = build_banner_body(
            original_body=raw_body,
            warning=decision.warning,
            content_type=content_type,
            content_transfer_encoding=cte,
            skip_protected=config.skip_signed,
        )
    except BannerError as exc:
        print("banner         : non costruibile (%s): resterebbe il solo header" % exc)
        return
    if rewrite is None:
        print("banner         : nessuna parte testuale modificabile, non applicato (solo header)")
        return
    print("--- inizio del messaggio riscritto ---")
    for line in preview(rewrite, limit=12):
        print(line)
    print("--- (segue il contenuto originale, invariato) ---")
    print("parti testuali modificate: %d" % rewrite.parts)


def _split_body(raw: bytes) -> bytes:
    """Corpo grezzo del messaggio (dopo la riga vuota degli header)."""
    for separator in (b"\r\n\r\n", b"\n\n"):
        index = raw.find(separator)
        if index >= 0:
            return raw[index + len(separator):]
    return b""


def _parse_message(raw: bytes):
    import email
    from email import policy as email_policy

    return email.message_from_bytes(raw, policy=email_policy.compat32)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------
def _report(level: str, message: str) -> None:
    DOCTOR_RESULTS.append((level, message))
    print("[%-4s] %s" % (level, message))


def _postconf(parameter: str) -> Optional[str]:
    postconf = shutil.which("postconf")
    if postconf:
        try:
            result = subprocess.run(
                [postconf, "-h", parameter],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.SubprocessError):
            pass
    path = "/etc/postfix/main.cf"
    if not os.path.exists(path):
        return None
    pattern = parameter + r"\s*=\s*(.*)"
    import re

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(pattern, line.strip())
                if match:
                    return match.group(1).strip()
    except OSError:
        return None
    return None


def _smtpd_chrooted() -> bool:
    """True se almeno un servizio smtpd gira con chroot=y (master.cf)."""
    path = "/etc/postfix/master.cf"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                fields = line.split()
                # service type private unpriv chroot wakeup maxproc command
                if len(fields) >= 8 and fields[7] == "smtpd" and fields[4] == "y":
                    return True
    except OSError:
        return False
    return False


def cmd_doctor(args) -> int:
    DOCTOR_RESULTS.clear()
    config = _load_config(args)
    _report("OK", "configurazione letta da %s" % config.path)
    for warning in config.warnings:
        _report("WARN", warning)
    if config.observe_only:
        _report(
            "WARN",
            "observe_only=true: i contatti vengono registrati ma nessun header "
            "viene aggiunto (rodaggio)",
        )
    else:
        _report("OK", "osservazione attiva: gli header vengono aggiunti")
    _report("INFO", "chiave: source=%s scope=%s header=%s" % (
        config.key_source, config.key_scope, config.header_name,
    ))

    # -- avviso visibile -------------------------------------------------
    mode = config.banner_mode
    if mode == "off":
        _report("INFO", "avviso visibile disattivato: viene aggiunto solo l'header")
    else:
        where = {
            "body": "banner in cima al corpo",
            "subject": "prefisso nell'oggetto",
            "both": "banner nel corpo e prefisso nell'oggetto",
        }[mode]
        _report("OK", "avviso visibile attivo: %s" % where)
    if mode in ("subject", "both") and not config.subject_tag:
        _report(
            "WARN",
            "[banner] mode=%s ma subject_tag e' vuoto: l'oggetto non cambia" % mode,
        )
    if mode in ("body", "both"):
        _report(
            "INFO",
            "il banner modifica le parti testuali del corpo: le firme DKIM "
            "del mittente possono risultare non valide per questo messaggio (compromesso "
            "accettato; usare [banner] mode = off per evitarle).",
        )
        limit = config.max_body_bytes
        _report(
            "INFO",
            "corpi oltre %s byte non vengono riscritti (resta il solo header)"
            % ("nessun limite" if limit == 0 else limit),
        )

    # -- regola "mittente assente" ---------------------------------------
    if config.dormant_days > 0:
        _report(
            "OK",
            "regola mittente assente attiva: avviso 'resumed' se il mittente "
            "non scriveva da almeno %d giorni" % config.dormant_days,
        )
        if config.retention_days and config.retention_days <= config.dormant_days:
            _report(
                "WARN",
                "retention_days (%d) <= dormant_days (%d): il pruning puo' "
                "cancellare lo storico prima che la regola scatti"
                % (config.retention_days, config.dormant_days),
            )
    else:
        _report(
            "INFO",
            "regola mittente assente disattivata ([policy] dormant_days = 0)",
        )

    # -- whitelist -------------------------------------------------------
    from .whitelist import WhitelistError, from_config as whitelist_from_config

    try:
        whitelist = whitelist_from_config(config)
        _report("OK", "whitelist: %s" % whitelist.summary())
        if whitelist.warnings:
            for warning in whitelist.warnings:
                _report("WARN", "whitelist: %s" % warning)
        if config.whitelist_file:
            if os.path.isfile(config.whitelist_file):
                _report("OK", "file whitelist leggibile: %s" % config.whitelist_file)
            else:
                _report(
                    "WARN",
                    "file whitelist assente o non leggibile: %s (le voci della "
                    "configurazione restano attive)" % config.whitelist_file,
                )
    except (ConfigError, WhitelistError) as exc:
        _report("FAIL", "whitelist: %s" % exc)

    try:
        import Milter

        version = getattr(Milter, "__version__", "?")
        _report("OK", "pymilter presente (versione %s)" % version)
    except ImportError:
        _report("FAIL", "pymilter assente: apt-get install python3-milter")

    store = None
    try:
        store = _open_store(config, readonly=True)
        result = store.integrity_check()
        if result == "ok":
            _report("OK", "database %s integro (%d coppie)" % (config.store_path, store.pairs()))
        else:
            _report("FAIL", "database %s: integrity_check = %s" % (config.store_path, result))
    except StoreError as exc:
        _report("FAIL", "database: %s" % exc)
    finally:
        if store is not None:
            store.close()

    store_dir = os.path.dirname(os.path.abspath(config.store_path))
    if os.path.isdir(store_dir):
        if os.access(store_dir, os.W_OK, effective_ids=True):
            _report("OK", "directory database %s scrivibile" % store_dir)
        else:
            _report(
                "WARN",
                "directory database %s non scrivibile dall'utente corrente "
                "(normale se non si e' root)" % store_dir,
            )
    else:
        _report("FAIL", "directory database assente: %s" % store_dir)

    socket_path = config.socket_path
    if socket_path:
        parent = os.path.dirname(socket_path)
        if os.path.isdir(parent):
            mode = stat.S_IMODE(os.stat(parent).st_mode)
            _report("OK", "directory socket %s (modo %04o)" % (parent, mode))
            if not os.access(parent, os.W_OK, effective_ids=True):
                _report("FAIL", "directory socket non scrivibile dall'utente corrente")
            if not mode & stat.S_ISGID:
                _report(
                    "WARN",
                    "directory socket senza setgid: il socket potrebbe non essere "
                    "accessibile a postfix (consigliato 2750 firstcontact:postfix)",
                )
        else:
            _report("FAIL", "directory del socket assente: %s" % parent)
        if os.path.exists(socket_path):
            _report(
                "WARN" if stat.S_ISSOCK(os.stat(socket_path).st_mode) else "FAIL",
                "socket %s %s"
                % (
                    socket_path,
                    "gia' presente: milter probabilmente in esecuzione"
                    if stat.S_ISSOCK(os.stat(socket_path).st_mode)
                    else "esiste ma non e' un socket",
                ),
            )
        else:
            _report("INFO", "socket %s non ancora creato" % socket_path)
    else:
        _report("INFO", "socket TCP %s (nessuna verifica di directory)" % config.socket)

    smtpd_milters = _postconf("smtpd_milters") or ""
    candidates = [config.socket]
    if socket_path and socket_path.startswith("/var/spool/postfix/"):
        candidates.append("unix:" + socket_path[len("/var/spool/postfix") :])
    tokens = smtpd_milters.split()
    matched = next((c for c in candidates if c in tokens), None)
    if matched:
        _report("OK", "smtpd_milters contiene %s" % matched)
        index = tokens.index(matched)
        after = tokens[index + 1 :]
        before = tokens[:index]
        if any("rspamd" in token.lower() for token in after):
            _report(
                "WARN",
                "c'e' rspamd dopo di noi in smtpd_milters: i messaggi che rspamd "
                "rifiuta verrebbero registrati come primo contatto. Consigliato "
                "mettere first-contact dopo il filtro antispam.",
            )
        if config.banner_mode != "off" and any(
            "opendkim" in token.lower() or "dkim" in token.lower() for token in before
        ):
            _report(
                "WARN",
                "opendkim e' prima di noi e l'avviso visibile modifica corpo/oggetto: "
                "se opendkim firma (Mode s o sv) la sua firma non copre la modifica "
                "e risultera' non valida. Se opendkim e' solo in verifica (Mode v) "
                "non ci sono effetti.",
            )
    else:
        _report(
            "FAIL",
            "smtpd_milters (%s) non contiene il nostro socket; atteso %s"
            % (smtpd_milters or "vuoto", " oppure ".join(candidates)),
        )

    action = _postconf("milter_default_action") or "tempfail"
    if action == "accept":
        _report("OK", "milter_default_action=accept (la posta passa anche a milter fermo)")
    else:
        _report(
            "WARN",
            "milter_default_action=%s: se il milter e' fermo la posta puo' subire "
            "ritardi o rifiuti" % action,
        )
    protocol = _postconf("milter_protocol") or "2"
    _report(
        "INFO" if protocol in ("6", "2") else "WARN",
        "milter_protocol=%s" % protocol,
    )

    if socket_path and _smtpd_chrooted() and not socket_path.startswith("/var/spool/postfix/"):
        _report(
            "FAIL",
            "smtpd gira in chroot: il socket unix deve stare sotto /var/spool/postfix "
            "(ora %s)" % socket_path,
        )

    unit = "/etc/systemd/system/first-contact-milter.service"
    if os.path.exists(unit):
        _report("OK", "unita' systemd installata: %s" % unit)
    else:
        _report("WARN", "unita' systemd assente: %s" % unit)

    failures = [level for level, _ in DOCTOR_RESULTS if level == "FAIL"]
    print("\n%d controlli, %d falliti" % (len(DOCTOR_RESULTS), len(failures)))
    return EXIT_PROBLEM if failures else EXIT_OK


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="first-contact",
        description="Header di allerta per il primo messaggio da un mittente (milter Postfix).",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument(
        "--config",
        default="/etc/first-contact/config.ini",
        help="file di configurazione (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Permette --config sia prima sia dopo il sottocomando (la unit systemd
    # lo passa dopo "serve"). Con SUPPRESS il valore del parser principale
    # resta valido quando l'opzione non viene ripetuta.
    config_parent = argparse.ArgumentParser(add_help=False)
    config_parent.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="file di configurazione (default: /etc/first-contact/config.ini)",
    )

    serve = subparsers.add_parser(
        "serve", help="esegue il milter (uso: systemd)", parents=[config_parent]
    )
    serve.set_defaults(func=cmd_serve)

    check = subparsers.add_parser(
        "check",
        help="dice se un mittente e' nuovo per un destinatario",
        parents=[config_parent],
    )
    check.add_argument("--sender", required=True)
    check.add_argument("--recipient", required=True)
    check.add_argument(
        "--client",
        default=None,
        help="indirizzo IP del client SMTP (per la whitelist CIDR)",
    )
    check.add_argument(
        "--from-header",
        action="store_true",
        help="interpreta --sender come valore di un header From:",
    )
    check.set_defaults(func=cmd_check)

    listing = subparsers.add_parser(
        "list", help="elenca i contatti memorizzati", parents=[config_parent]
    )
    listing.add_argument("--sender", dest="sender_filter")
    listing.add_argument("--recipient", dest="recipient_filter")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.set_defaults(func=cmd_list)

    stats = subparsers.add_parser(
        "stats", help="statistiche del database", parents=[config_parent]
    )
    stats.set_defaults(func=cmd_stats)

    prune = subparsers.add_parser(
        "prune", help="elimina i contatti vecchi", parents=[config_parent]
    )
    prune.add_argument("--days", type=int, default=None)
    prune.add_argument("--dry-run", action="store_true")
    prune.set_defaults(func=cmd_prune)

    simulate = subparsers.add_parser(
        "simulate",
        help="mostra l'header che verrebbe aggiunto a un messaggio .eml",
        parents=[config_parent],
    )
    simulate.add_argument("--eml", help="file RFC 5322 da esaminare")
    simulate.add_argument("--sender", help="mittente envelope (default: From:)")
    simulate.add_argument("--recipient", required=True)
    simulate.add_argument("--client", default="203.0.113.10", help="indirizzo IP del client")
    simulate.add_argument("--helo", default=None)
    simulate.add_argument(
        "--known-days",
        type=int,
        default=None,
        help="finge un contatto avvenuto N giorni fa, per provare la regola "
        "'mittente assente'",
    )
    simulate.add_argument(
        "--db",
        default=None,
        help="database da usare (default: file temporaneo, non tocca quello reale)",
    )
    simulate.set_defaults(func=cmd_simulate)

    doctor = subparsers.add_parser(
        "doctor",
        help="controlla configurazione e aggancio a Postfix",
        parents=[config_parent],
    )
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_CONFIG
    try:
        with _service_identity(args.command):
            return args.func(args)
    except ConfigError as exc:
        sys.stderr.write("errore di configurazione: %s\n" % exc)
        return EXIT_CONFIG
    except StoreError as exc:
        sys.stderr.write("errore database: %s\n" % exc)
        return EXIT_PROBLEM
    except KeyboardInterrupt:
        return 130
