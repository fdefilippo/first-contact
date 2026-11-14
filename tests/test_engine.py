import logging
import os
import shutil
import tempfile
import unittest

from firstcontact.config import Config
from firstcontact.engine import Decider, MessageContext
from firstcontact.store import ContactStore, StoreError

TS = 1700000000  # 2023-11-14T22:13:20Z


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-engine-")
        self.path = os.path.join(self.directory, "contacts.db")
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
        return Decider(config, store, self.logger), store

    def context(self, **kwargs):
        values = dict(
            sender="<mario@example.com>",
            recipients=["<anna@example.it>"],
            from_header="Mario Rossi <mario@example.com>",
            message_id="<abc@example.com>",
            subject="ciao",
            client_addr="203.0.113.9",
            client_name="mail.example.com",
            helo="mail.example.com",
            received_at=TS,
        )
        values.update(kwargs)
        return MessageContext(**values)

    def test_first_message_gets_one_header(self):
        decider, store = self.make()
        decision = decider.decide(self.context())
        self.assertEqual(len(decision.headers), 1)
        name, value = decision.headers[0]
        self.assertEqual(name, "X-First-Contact")
        self.assertIn("sender=mario@example.com", value)
        self.assertIn("recipient=anna@example.it", value)
        self.assertIn("first-seen=2023-11-14T22:13:20Z", value)
        self.assertEqual(decision.new_pairs, [("mario@example.com", "anna@example.it")])
        row = store.lookup("mario@example.com", "anna@example.it")
        self.assertEqual(row["first_seen"], TS)
        self.assertEqual(row["first_client"], "203.0.113.9")

    def test_second_message_has_no_header(self):
        decider, _ = self.make()
        decider.decide(self.context())
        decision = decider.decide(self.context())
        self.assertEqual(decision.headers, [])
        self.assertEqual(decision.new_pairs, [])

    def test_observe_only_records_without_header(self):
        decider, store = self.make({"policy": {"observe_only": "true"}})
        decision = decider.decide(self.context())
        self.assertTrue(decision.observed_only)
        self.assertEqual(decision.headers, [])
        self.assertIsNotNone(store.lookup("mario@example.com", "anna@example.it"))

    def test_key_scope_domain(self):
        decider, _ = self.make({"policy": {"key_scope": "domain"}})
        self.assertEqual(len(decider.decide(self.context()).headers), 1)
        decision = decider.decide(self.context(sender="<altro@example.com>"))
        self.assertEqual(decision.headers, [])

    def test_key_source_from_header(self):
        decider, store = self.make({"policy": {"key_source": "from"}})
        decision = decider.decide(
            self.context(sender="<bounce@relay.example.net>", from_header="Mario <mario@example.com>")
        )
        self.assertEqual(len(decision.headers), 1)
        self.assertIn("sender=mario@example.com", decision.headers[0][1])
        self.assertIsNotNone(store.lookup("mario@example.com", "anna@example.it"))
        self.assertIsNone(store.lookup("bounce@relay.example.net", "anna@example.it"))

    def test_multiple_recipients(self):
        decider, _ = self.make()
        decision = decider.decide(
            self.context(recipients=["<anna@example.it>", "<bruno@example.it>"])
        )
        self.assertEqual(len(decision.headers), 2)
        self.assertEqual(
            [pair[1] for pair in decision.new_pairs],
            ["anna@example.it", "bruno@example.it"],
        )
        # secondo messaggio: solo il destinatario nuovo riceve l'header
        decision = decider.decide(
            self.context(recipients=["<anna@example.it>", "<carla@example.it>"])
        )
        self.assertEqual(len(decision.headers), 1)
        self.assertIn("recipient=undisclosed", decision.headers[0][1])

    def test_shared_message_redacts_new_and_resumed_recipients(self):
        decider, store = self.make({"banner": {
            "text": "New {recipient} {recipients}",
            "text_resumed": "Resumed {recipient} {recipients}",
        }})
        store.first_contact("mario@example.com", "old@example.it", first_seen=TS - 100 * 86400)
        decision = decider.decide(self.context(
            recipients=["<new@example.it>", "<old@example.it>"]))
        self.assertEqual(len(decision.headers), 2)
        self.assertEqual(len(decision.new_pairs), 1)
        self.assertEqual(len(decision.resumed_pairs), 1)
        for _, value in decision.headers:
            self.assertIn("recipient=undisclosed", value)
            self.assertNotIn("new@example.it", value)
            self.assertNotIn("old@example.it", value)
        self.assertNotIn("@example.it", decision.warning)

    def test_duplicate_recipient_is_emitted_once(self):
        decider, _ = self.make()
        decision = decider.decide(
            self.context(recipients=["<anna@example.it>", "<ANNA@example.it>", "<anna@Example.it>"])
        )
        self.assertEqual(len(decision.headers), 1)

    def test_authenticated_message_is_ignored(self):
        decider, store = self.make()
        decision = decider.decide(self.context(auth_authen="mario"))
        self.assertEqual(decision.reason, "authenticated")
        self.assertEqual(decision.headers, [])
        self.assertEqual(store.pairs(), 0)

    def test_loopback_client_is_ignored(self):
        decider, store = self.make()
        decision = decider.decide(self.context(client_addr="127.0.0.1"))
        self.assertEqual(decision.reason, "trusted-network")
        self.assertEqual(store.pairs(), 0)

    def test_local_submission_is_ignored_by_default(self):
        decider, store = self.make()
        decision = decider.decide(self.context(client_addr=None))
        self.assertEqual(decision.reason, "local")
        self.assertEqual(store.pairs(), 0)

    def test_local_submission_can_be_enabled(self):
        decider, _ = self.make({"policy": {"process_local": "true"}})
        decision = decider.decide(self.context(client_addr=None))
        self.assertEqual(len(decision.headers), 1)

    def test_null_sender_is_ignored(self):
        decider, store = self.make()
        decision = decider.decide(self.context(sender="<>", from_header=None))
        self.assertEqual(decision.reason, "null-sender")
        self.assertEqual(store.pairs(), 0)

    def test_custom_header_name(self):
        decider, _ = self.make({"milter": {"header_name": "X-Primo-Contatto"}})
        decision = decider.decide(self.context())
        self.assertEqual(decision.headers[0][0], "X-Primo-Contatto")

    def test_long_message_id_is_truncated(self):
        decider, _ = self.make()
        decision = decider.decide(self.context(message_id="<" + "x" * 500 + "@example.com>"))
        name, value = decision.headers[0]
        self.assertLessEqual(len(value), 900)
        for line in value.split("\n"):
            self.assertLessEqual(len(line), 998)

    def test_store_error_propagates(self):
        decider, store = self.make()

        def boom(*args, **kwargs):
            raise StoreError("database rotto")

        store.record_message = boom
        with self.assertRaises(StoreError):
            decider.decide(self.context())


class VisibleWarningTest(unittest.TestCase):
    """L'avviso visibile (banner e oggetto) prodotto da Decider."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-banner-")
        self.path = os.path.join(self.directory, "contacts.db")
        self.logger = logging.getLogger("firstcontact.test.banner")
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
        return Decider(config, store, self.logger), store

    def context(self, **kwargs):
        values = dict(
            sender="<mario@example.com>",
            recipients=["<anna@example.it>"],
            from_header="Mario Rossi <mario@example.com>",
            message_id="<abc@example.com>",
            subject="ciao",
            client_addr="203.0.113.9",
            received_at=TS,
        )
        values.update(kwargs)
        return MessageContext(**values)

    def test_default_mode_adds_body_warning(self):
        decider, _ = self.make()
        decision = decider.decide(self.context())
        self.assertEqual(len(decision.headers), 1)
        self.assertIsNotNone(decision.warning)
        self.assertIn("mario@example.com", decision.warning)
        self.assertIn("allegati", decision.warning.lower())
        self.assertIsNone(decision.subject_tag)

    def test_warning_does_not_disclose_recipients_of_shared_message(self):
        decider, _ = self.make(
            {"banner": {"text": "Primo contatto: {sender} -> {recipients}"}}
        )
        decision = decider.decide(
            self.context(recipients=["<anna@example.it>", "<bruno@example.it>"])
        )
        self.assertNotIn("anna@example.it", decision.warning)
        self.assertNotIn("bruno@example.it", decision.warning)

    def test_mode_off_has_no_visible_warning(self):
        decider, _ = self.make({"banner": {"mode": "off"}})
        decision = decider.decide(self.context())
        self.assertEqual(len(decision.headers), 1)
        self.assertIsNone(decision.warning)
        self.assertIsNone(decision.subject_tag)

    def test_mode_subject_only(self):
        decider, _ = self.make(
            {"banner": {"mode": "subject", "subject_tag": "[PRIMO CONTATTO]"}}
        )
        decision = decider.decide(self.context())
        self.assertIsNone(decision.warning)
        self.assertEqual(decision.subject_tag, "[PRIMO CONTATTO]")

    def test_mode_both(self):
        decider, _ = self.make(
            {"banner": {"mode": "both", "subject_tag": "[NUOVO]"}}
        )
        decision = decider.decide(self.context())
        self.assertIsNotNone(decision.warning)
        self.assertEqual(decision.subject_tag, "[NUOVO]")

    def test_custom_template(self):
        decider, _ = self.make(
            {"banner": {"text": "Primo messaggio da {sender} per {recipient}."}}
        )
        decision = decider.decide(self.context())
        self.assertEqual(
            decision.warning, "Primo messaggio da mario@example.com per anna@example.it."
        )

    def test_observe_only_has_no_visible_warning(self):
        decider, _ = self.make({"policy": {"observe_only": "true"}})
        decision = decider.decide(self.context())
        self.assertIsNone(decision.warning)
        self.assertIsNone(decision.subject_tag)

    def test_second_message_has_no_visible_warning(self):
        decider, _ = self.make()
        decider.decide(self.context())
        decision = decider.decide(self.context())
        self.assertIsNone(decision.warning)
        self.assertEqual(decision.headers, [])

    def test_whitelisted_sender_gets_nothing(self):
        decider, store = self.make({"whitelist": {"senders": "@example.com"}})
        decision = decider.decide(self.context())
        self.assertEqual(decision.reason, "whitelist-domain")
        self.assertEqual(decision.headers, [])
        self.assertIsNone(decision.warning)
        self.assertEqual(store.pairs(), 0)

    def test_whitelisted_network_gets_nothing(self):
        decider, store = self.make({"whitelist": {"networks": "203.0.113.0/24"}})
        decision = decider.decide(self.context())
        self.assertEqual(decision.reason, "whitelist-cidr")
        self.assertEqual(store.pairs(), 0)


class DormantSenderTest(unittest.TestCase):
    """Regola 'mittente assente da N giorni'."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-dormant-")
        self.path = os.path.join(self.directory, "contacts.db")
        self.logger = logging.getLogger("firstcontact.test.dormant")
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
        return Decider(config, store, self.logger), store

    def context(self, **kwargs):
        values = dict(
            sender="<mario@example.com>",
            recipients=["<anna@example.it>"],
            from_header="Mario Rossi <mario@example.com>",
            message_id="<abc@example.com>",
            subject="ciao",
            client_addr="203.0.113.9",
            received_at=TS,
        )
        values.update(kwargs)
        return MessageContext(**values)

    def seed(self, store, days):
        store.first_contact(
            "mario@example.com",
            "anna@example.it",
            first_seen=TS - int(days * 86400),
        )

    def test_sender_back_after_threshold_gets_resumed_alert(self):
        decider, store = self.make()
        self.seed(store, 100)
        decision = decider.decide(self.context())

        self.assertEqual(decision.new_pairs, [])
        self.assertEqual(
            decision.resumed_pairs[0][:3],
            ("mario@example.com", "anna@example.it", 100),
        )
        name, value = decision.headers[0]
        self.assertEqual(name, "X-First-Contact")
        self.assertIn("resumed", value)
        self.assertIn("days=100", value)
        self.assertIn("last-seen=2023-08-06T22:13:20Z", value)
        self.assertIn("non ti scriveva da 100 giorni", decision.warning)
        self.assertIn("identita", decision.warning)

    def test_sender_seen_recently_is_not_alerted(self):
        decider, store = self.make()
        self.seed(store, 30)
        decision = decider.decide(self.context())
        self.assertEqual(decision.headers, [])
        self.assertEqual(decision.resumed_pairs, [])
        self.assertIsNone(decision.warning)

    def test_rule_disabled_with_zero(self):
        decider, store = self.make({"policy": {"dormant_days": "0"}})
        self.seed(store, 500)
        decision = decider.decide(self.context())
        self.assertEqual(decision.headers, [])
        self.assertEqual(decision.resumed_pairs, [])
        self.assertIsNone(decision.warning)

    def test_custom_threshold(self):
        decider, store = self.make({"policy": {"dormant_days": "7"}})
        self.seed(store, 10)
        decision = decider.decide(self.context())
        self.assertEqual(decision.resumed_pairs[0][2], 10)
        self.assertIn("resumed", decision.headers[0][1])

    def test_last_seen_is_updated_even_without_stats(self):
        decider, store = self.make({"store": {"track_stats": "false"}})
        self.seed(store, 100)
        decider.decide(self.context())
        row = store.lookup("mario@example.com", "anna@example.it")
        self.assertEqual(row["last_seen"], TS)
        # il motore forza track=True quando dormant_days > 0, cosi' last_seen
        # resta aggiornato e non si generano falsi "assente"
        self.assertEqual(row["messages"], 2)

    def test_observe_only_resumed_records_without_alert(self):
        decider, store = self.make({"policy": {"observe_only": "true"}})
        self.seed(store, 100)
        decision = decider.decide(self.context())
        self.assertTrue(decision.observed_only)
        self.assertEqual(decision.headers, [])
        self.assertEqual(decision.resumed_pairs[0][2], 100)
        self.assertIsNone(decision.warning)
        self.assertEqual(store.lookup("mario@example.com", "anna@example.it")["last_seen"], TS)

    def test_both_first_contact_and_resumed_warnings_are_combined(self):
        decider, store = self.make()
        # mario e' conosciuto da anna (assente), ma nuovo per bruno
        self.seed(store, 100)
        decision = decider.decide(
            self.context(recipients=["<anna@example.it>", "<bruno@example.it>"])
        )
        self.assertIn("primo messaggio", decision.warning.lower())
        self.assertIn("non ti scriveva da 100 giorni", decision.warning)
        self.assertEqual(len(decision.headers), 2)

    def test_resumed_header_is_built_without_first_message_marker(self):
        decider, store = self.make()
        self.seed(store, 100)
        _name, value = decider.decide(self.context()).headers[0]
        self.assertNotIn("first-message", value)

    def test_first_seen_placeholder_is_the_original_contact(self):
        decider, store = self.make(
            {"banner": {"text_resumed": "primo contatto: {first_seen}"}}
        )
        self.seed(store, 100)
        decision = decider.decide(self.context())
        self.assertIn("2023-08-06T22:13:20Z", decision.warning)


if __name__ == "__main__":
    unittest.main()
