"""Test del collante pymilter usando un finto modulo ``Milter``."""

import logging
import os
import shutil
import tempfile
import types
import unittest

from firstcontact.config import Config
from firstcontact.engine import Decider
from firstcontact.milter_app import build_milter_class
from firstcontact.store import ContactStore


class FakeMilterError(Exception):
    pass


class FakeBase(object):
    def __init__(self):
        self.added = []
        self.changed = []
        self.body_value = None
        self.replace_calls = 0
        self.reject_message = None

    def addheader(self, name, value, idx=-1):
        self.added.append((name, value, idx))

    def chgheader(self, name, idx, value):
        self.changed.append((name, idx, value))

    def replacebody(self, chunk):
        self.replace_calls += 1
        if not isinstance(chunk, bytes):
            chunk = b"".join(chunk)
        self.body_value = chunk

    def getsymval(self, name):
        return getattr(self, "macros", {}).get(name)


def fake_milter_module():
    module = types.ModuleType("Milter")
    module.Base = FakeBase
    module.CONTINUE = 0
    module.MilterError = FakeMilterError
    module.uniqueID = lambda: 7
    return module


class MilterTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-milter-")
        self.path = os.path.join(self.directory, "contacts.db")
        self.module = fake_milter_module()
        self.logger = logging.getLogger("firstcontact.test")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def make(self, overrides=None):
        config = Config.from_overrides(overrides or {})
        store = ContactStore(self.path)
        self.stores.append(store)
        decider = Decider(config, store, self.logger)
        return decider, store

    def deliver(self, milter, sender="<mario@example.com>", recipients=("<anna@example.it>",),
                client="203.0.113.9", helo="mail.example.com", headers=None, macros=None,
                body=None):
        milter.added = []
        milter.changed = []
        milter.body_value = None
        milter.replace_calls = 0
        if macros is not None:
            milter.macros = macros
        milter.connect("mail.example.com", 2, client)
        milter.hello(helo)
        milter.envfrom(sender)
        for recipient in recipients:
            milter.envrcpt(recipient)
        for name, value in (headers or [("From", "Mario <mario@example.com>"),
                                        ("Message-ID", "<abc@example.com>"),
                                        ("Subject", "ciao")]):
            milter.header(name, value)
        if body is not None:
            chunks = body if isinstance(body, (list, tuple)) else [body]
            for chunk in chunks:
                milter.body(chunk)
        return milter.eom()

    def test_transaction_adds_header_on_top(self):
        decider, store = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.assertEqual(self.deliver(milter), self.module.CONTINUE)
        self.assertEqual(len(milter.added), 1)
        name, value, idx = milter.added[0]
        self.assertEqual(name, "X-First-Contact")
        self.assertEqual(idx, 0)
        self.assertIn("sender=mario@example.com", value)
        self.assertIn("recipient=anna@example.it", value)
        self.assertIsNotNone(store.lookup("mario@example.com", "anna@example.it"))

    def test_second_transaction_has_no_header(self):
        decider, _ = self.make()
        milter_class = build_milter_class(self.module, decider, self.logger)
        first = milter_class()
        self.deliver(first)
        second = milter_class()
        self.deliver(second)
        self.assertEqual(second.added, [])

    def test_header_position_end(self):
        decider, _ = self.make({"milter": {"header_position": "end"}})
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter)
        self.assertEqual(milter.added[0][2], -1)

    def test_connect_accepts_inet_tuple(self):
        # pymilter 1.0.3 passa (ip, porta) per AF_INET, non una stringa
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, client=("203.0.113.9", 45678))
        self.assertEqual(milter.client_addr, "203.0.113.9")
        self.assertIn("client=203.0.113.9", milter.added[0][1])

    def test_connect_accepts_unix_string(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        milter.connect("local", 1, "/var/run/smtpd.sock")
        self.assertEqual(milter.client_addr, "/var/run/smtpd.sock")

    def test_fallback_when_insheader_is_unsupported(self):
        decider, _ = self.make()

        class NonInsBase(FakeBase):
            def addheader(self, name, value, idx=-1):
                if idx == 0:
                    raise FakeMilterError("insheader not supported")
                FakeBase.addheader(self, name, value, idx)

        module = fake_milter_module()
        module.Base = NonInsBase
        milter = build_milter_class(module, decider, self.logger)()
        self.deliver(milter)
        self.assertEqual(len(milter.added), 1)
        self.assertEqual(milter.added[0][2], -1)

    def test_shared_message_never_adds_hidden_recipient_to_any_copy(self):
        for cap in (1, 50):
            with self.subTest(max_recipients=cap):
                decider, store = self.make({
                    "policy": {"max_recipients": str(cap)},
                    "banner": {"text": "Warning {sender}: {recipient} / {recipients}"},
                })
                milter = build_milter_class(self.module, decider, self.logger)()
                sender = "sender%d@example.net" % cap
                self.deliver(milter, sender=sender,
                             recipients=("<visible@example.it>", "<hidden@example.it>"),
                             headers=[("To", "visible@example.it")], body=b"Original\r\n")
                # Postfix applies the same modifications to every delivered copy.
                added_content = "\n".join(value for _, value, _ in milter.added)
                added_content += milter.body_value.decode("utf-8")
                self.assertNotIn("hidden@example.it", added_content)
                self.assertNotIn("visible@example.it", added_content)
                self.assertIn("recipient=undisclosed", added_content)
                self.assertIsNotNone(store.lookup(sender, "visible@example.it"))

    def test_authenticated_session_is_ignored(self):
        decider, store = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, macros={"{auth_authen}": "mario"})
        self.assertEqual(milter.added, [])
        self.assertEqual(store.pairs(), 0)

    def test_local_pickup_without_client_address_is_ignored(self):
        decider, store = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, client=None)
        self.assertEqual(milter.added, [])
        self.assertEqual(store.pairs(), 0)

    def test_macro_fallback_for_client_address(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        milter.added = []
        milter.connect("unknown", 2, None)  # libmilter puo' non passare l'IP
        milter.hello("mail.example.com")
        milter.envfrom("<mario@example.com>")
        milter.envrcpt("<anna@example.it>")
        milter.macros = {"{client_addr}": "198.51.100.7"}
        milter.header("From", "Mario <mario@example.com>")
        milter.eom()
        self.assertEqual(len(milter.added), 1)
        self.assertIn("client=198.51.100.7", milter.added[0][1])

    def test_many_recipients_are_capped(self):
        decider, _ = self.make({"policy": {"max_recipients": "3"}})
        milter = build_milter_class(self.module, decider, self.logger)()
        recipients = ["<r%d@example.it>" % i for i in range(10)]
        self.deliver(milter, recipients=recipients)
        self.assertEqual(len(milter.recipients), 3)
        self.assertEqual(len(milter.added), 3)

    def test_decider_crash_never_breaks_mail(self):
        decider, _ = self.make()

        def boom(context):
            raise RuntimeError("guasto simulato")

        decider.decide = boom
        milter = build_milter_class(self.module, decider, self.logger)()
        self.assertEqual(self.deliver(milter), self.module.CONTINUE)
        self.assertEqual(milter.added, [])

    def test_state_is_reset_between_transactions(self):
        decider, _ = self.make()
        milter_class = build_milter_class(self.module, decider, self.logger)
        milter = milter_class()
        self.deliver(milter)
        # stesso oggetto, seconda transazione verso un destinatario nuovo
        self.deliver(milter, sender="<altro@example.com>")
        self.assertEqual(len(milter.added), 1)
        self.assertIn("sender=altro@example.com", milter.added[0][1])
        self.assertNotIn("mario@example.com", milter.added[0][1])

    def test_abort_and_close_are_safe(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        milter.connect("mail.example.com", 2, "203.0.113.9")
        milter.envfrom("<mario@example.com>")
        milter.abort()
        self.assertEqual(milter.recipients, [])
        self.assertEqual(milter.close(), self.module.CONTINUE)

    def test_two_messages_on_the_same_connection(self):
        """Postfix puo' riusare la connessione milter per piu' messaggi."""
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        milter.connect("mail.example.com", 2, "203.0.113.9")
        milter.hello("mail.example.com")

        milter.added = []
        milter.envfrom("<mario@example.com>")
        milter.envrcpt("<anna@example.it>")
        milter.header("From", "Mario <mario@example.com>")
        milter.eom()
        self.assertEqual(len(milter.added), 1)

        milter.added = []
        milter.envfrom("<altro@example.com>")
        milter.envrcpt("<bruno@example.it>")
        milter.header("From", "Altro <altro@example.com>")
        milter.eom()
        self.assertEqual(len(milter.added), 1)
        value = milter.added[0][1]
        self.assertIn("sender=altro@example.com", value)
        self.assertIn("recipient=bruno@example.it", value)
        self.assertNotIn("anna@example.it", value)

    # -- avviso visibile -------------------------------------------------
    def test_body_banner_is_prepended_without_new_mime_parts(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "ciao"),
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Transfer-Encoding", "7bit"),
            ("MIME-Version", "1.0"),
        ]
        self.deliver(milter, headers=headers, body=[b"Ciao ", b"Anna!\n"])
        self.assertEqual(self.module.CONTINUE, 0)
        self.assertEqual(len(milter.added), 1)
        self.assertEqual(milter.replace_calls, 1)
        self.assertTrue(milter.body_value.endswith(b"Ciao Anna!\n"))
        self.assertIn(b"primo messaggio", milter.body_value.lower())
        self.assertIn(b"allegati", milter.body_value.lower())
        # il corpo resta text/plain: nessuna nuova parte MIME, nessun header
        self.assertNotIn(b"text/html", milter.body_value)
        self.assertNotIn(b"multipart/", milter.body_value)
        self.assertEqual(milter.changed, [])

    def test_html_body_gets_the_banner_box(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "ciao"),
            ("Content-Type", "text/html; charset=utf-8"),
        ]
        self.deliver(
            milter,
            headers=headers,
            body=b"<html><body><p>Ciao</p></body></html>",
        )
        self.assertEqual(milter.replace_calls, 1)
        self.assertIn(b"background-color:#ffff00", milter.body_value)
        self.assertIn(b"<p>Ciao</p>", milter.body_value)
        self.assertNotIn(b"multipart/", milter.body_value)
        self.assertEqual(milter.changed, [])

    def test_base64_body_stays_base64(self):
        import base64

        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "ciao"),
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Transfer-Encoding", "base64"),
        ]
        self.deliver(milter, headers=headers, body=b"SGVsbG8=")
        self.assertEqual(milter.changed, [])
        decoded = base64.b64decode(milter.body_value)
        self.assertTrue(decoded.endswith(b"Hello"))
        self.assertIn(b"primo messaggio", decoded.lower())

    def test_subject_tag_is_prepended_once(self):
        decider, _ = self.make(
            {"banner": {"mode": "subject", "subject_tag": "[PRIMO CONTATTO]"}}
        )
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, body=b"testo\n")
        self.assertEqual(
            milter.changed, [("Subject", 1, "[PRIMO CONTATTO] ciao")]
        )
        self.assertEqual(milter.replace_calls, 0)

    def test_subject_tag_is_not_added_twice(self):
        decider, _ = self.make(
            {"banner": {"mode": "subject", "subject_tag": "[PRIMO]"}}
        )
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "[PRIMO] ciao"),
        ]
        self.deliver(milter, headers=headers)
        self.assertEqual(milter.changed, [])

    def test_missing_subject_is_created(self):
        decider, _ = self.make(
            {"banner": {"mode": "subject", "subject_tag": "[PRIMO]"}}
        )
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
        ]
        self.deliver(milter, headers=headers)
        self.assertIn(("Subject", "[PRIMO]", -1), milter.added)

    def test_signed_message_is_not_rewritten(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "ciao"),
            ("Content-Type", 'multipart/signed; boundary="firma"'),
        ]
        self.deliver(milter, headers=headers, body=b"--firma--\n")
        self.assertEqual(milter.replace_calls, 0)
        self.assertEqual(milter.changed, [])
        self.assertEqual(len(milter.added), 1)

    def test_signed_message_gets_no_subject_tag_either(self):
        decider, _ = self.make(
            {"banner": {"mode": "both", "subject_tag": "[PRIMO]"}}
        )
        milter = build_milter_class(self.module, decider, self.logger)()
        headers = [
            ("From", "Mario <mario@example.com>"),
            ("Message-ID", "<abc@example.com>"),
            ("Subject", "ciao"),
            ("Content-Type", 'multipart/signed; boundary="firma"'),
        ]
        self.deliver(milter, headers=headers, body=b"--firma--\n")
        self.assertEqual(milter.replace_calls, 0)
        self.assertEqual(milter.changed, [])
        self.assertEqual(len(milter.added), 1)

    def test_body_over_limit_falls_back_to_header_only(self):
        decider, _ = self.make({"banner": {"max_body_bytes": "10"}})
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, body=b"x" * 100)
        self.assertEqual(milter.replace_calls, 0)
        self.assertEqual(len(milter.added), 1)

    def test_without_body_callback_nothing_is_replaced(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, body=None)
        self.assertEqual(milter.replace_calls, 0)
        self.assertEqual(len(milter.added), 1)

    def test_replacebody_failure_is_swallowed(self):
        decider, _ = self.make()

        class NoReplace(FakeBase):
            def replacebody(self, chunk):
                raise FakeMilterError("replacebody non supportato")

        module = fake_milter_module()
        module.Base = NoReplace
        milter = build_milter_class(module, decider, self.logger)()
        self.assertEqual(self.deliver(milter, body=b"testo\n"), self.module.CONTINUE)
        self.assertEqual(len(milter.added), 1)

    def test_known_sender_gets_no_banner(self):
        decider, _ = self.make()
        milter = build_milter_class(self.module, decider, self.logger)()
        self.deliver(milter, body=b"primo\n")
        self.assertEqual(milter.replace_calls, 1)
        self.deliver(milter, body=b"secondo\n")
        self.assertEqual(milter.replace_calls, 0)
        self.assertEqual(milter.added, [])


if __name__ == "__main__":
    unittest.main()
