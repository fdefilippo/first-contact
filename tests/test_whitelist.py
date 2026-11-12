import logging
import os
import shutil
import tempfile
import time
import unittest

from firstcontact.config import Config, ConfigError
from firstcontact.policy import Policy
from firstcontact.whitelist import SenderWhitelist, WhitelistError, from_config

# i test provocano voci non valide di proposito: niente rumore su stderr
_wl_logger = logging.getLogger("firstcontact.whitelist")
_wl_logger.handlers = [logging.NullHandler()]
_wl_logger.propagate = False


class InlineTest(unittest.TestCase):
    def test_address_domain_and_cidr(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline(["mario.rossi@example.com", "@example.com"], ["10.0.0.0/8"])
        self.assertEqual(
            whitelist.matches("mario.rossi@example.com", "203.0.113.9"),
            "address:mario.rossi@example.com",
        )
        self.assertEqual(
            whitelist.matches("altro@example.com", "203.0.113.9"),
            "domain:example.com",
        )
        self.assertEqual(
            whitelist.matches("chiunque@sub.example.com", "203.0.113.9"),
            "domain:sub.example.com",
        )
        self.assertEqual(
            whitelist.matches("esterno@altro.net", "10.1.2.3"),
            "cidr:10.0.0.0/8",
        )
        self.assertIsNone(whitelist.matches("esterno@altro.net", "203.0.113.9"))

    def test_wildcard_and_bare_domain(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline(["*.example.com", "example.net"], [])
        self.assertEqual(
            whitelist.matches("a@example.com", "203.0.113.9"), "domain:example.com"
        )
        self.assertEqual(
            whitelist.matches("a@example.net", "203.0.113.9"), "domain:example.net"
        )
        self.assertIsNone(whitelist.matches("a@notexample.com", "203.0.113.9"))

    def test_single_ip(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline([], ["203.0.113.9"])
        self.assertEqual(
            whitelist.matches("a@example.com", "203.0.113.9"), "cidr:203.0.113.9/32"
        )

    def test_case_and_brackets_are_normalised(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline(["Mario@Example.com"], [])
        self.assertIsNotNone(whitelist.matches("<MARIO@EXAMPLE.COM>", None))

    def test_ipv6(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline([], ["2001:db8::/32"])
        self.assertEqual(whitelist.matches("a@example.com", "2001:db8::1"), "cidr:2001:db8::/32")

    def test_invalid_entries_block_startup(self):
        for bad in ("senza chiocciola", "a@@b.it", "10.0.0.0/99", "@", "*."):
            with self.assertRaises(WhitelistError, msg=bad):
                SenderWhitelist().load_inline([bad], [])

    def test_summary(self):
        whitelist = SenderWhitelist()
        whitelist.load_inline(["a@b.it", "@c.it"], ["10.0.0.0/8"])
        self.assertEqual(whitelist.summary(), "1 indirizzo, 1 dominio, 1 rete")


class FileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="first-contact-wl-")
        self.path = os.path.join(self.directory, "whitelist.txt")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_file_is_lenient(self):
        self.write(
            "# commento\n"
            "mario@example.com\n"
            "\n"
            "riga non valida\n"
            "@example.org\n"
            "10.0.0.0/8\n"
        )
        whitelist = SenderWhitelist(self.path)
        whitelist.load_file()
        self.assertEqual(len(whitelist.warnings), 1)
        self.assertIn("riga non valida", whitelist.warnings[0])
        self.assertIsNotNone(whitelist.matches("mario@example.com", None))
        self.assertIsNotNone(whitelist.matches("x@example.org", None))
        self.assertIsNotNone(whitelist.matches("x@example.net", "10.9.9.9"))

    def test_missing_file_is_not_fatal(self):
        whitelist = SenderWhitelist(os.path.join(self.directory, "assente.txt"))
        whitelist.load_file()
        self.assertIsNone(whitelist.matches("a@b.it", "203.0.113.9"))

    def test_file_is_reloaded_when_it_changes(self):
        self.write("mario@example.com\n")
        whitelist = SenderWhitelist(self.path)
        self.assertIsNotNone(whitelist.matches("mario@example.com", None))
        # cambio dimensione + mtime per essere sicuri anche su fs grossolani
        self.write("mario@example.com\nanna@example.com\n")
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.assertIsNotNone(whitelist.matches("anna@example.com", None))

    def test_invalid_line_does_not_hide_valid_ones(self):
        self.write("!!!\nmario@example.com\n")
        whitelist = SenderWhitelist(self.path)
        whitelist.load_file()
        self.assertIsNotNone(whitelist.matches("mario@example.com", None))


class FromConfigTest(unittest.TestCase):
    def test_both_sources_are_merged(self):
        config = Config.from_overrides(
            {"whitelist": {"senders": "a@b.it, @c.it", "networks": "10.0.0.0/8"}}
        )
        whitelist = from_config(config)
        self.assertIsNotNone(whitelist.matches("a@b.it", None))
        self.assertIsNotNone(whitelist.matches("x@c.it", None))
        self.assertIsNotNone(whitelist.matches("x@d.it", "10.0.0.1"))

    def test_policy_reports_config_error_for_bad_inline_entry(self):
        config = Config.from_overrides({"whitelist": {"senders": "non valido"}})
        with self.assertRaises(ConfigError):
            Policy(config)

    def test_policy_reason_is_whitelist(self):
        config = Config.from_overrides({"whitelist": {"senders": "@example.com"}})
        allowed, reason = Policy(config).evaluate("mario@example.com", "203.0.113.9", None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "whitelist-domain")

    def test_whitelist_wins_over_authenticated(self):
        """La whitelist deve escludere anche i casi che sarebbero saltati dopo."""
        config = Config.from_overrides({"whitelist": {"networks": "10.0.0.0/8"}})
        allowed, reason = Policy(config).evaluate("a@b.it", "10.0.0.1", "utente")
        self.assertFalse(allowed)
        self.assertEqual(reason, "whitelist-cidr")


if __name__ == "__main__":
    unittest.main()
