import unittest

from firstcontact.config import Config
from firstcontact.policy import (
    Policy,
    address_domain,
    from_header_address,
    is_address_like,
    normalize_address,
)


class NormalizeTest(unittest.TestCase):
    def test_angle_brackets_and_case(self):
        self.assertEqual(normalize_address("<Mario.Rossi@Example.COM>"), "mario.rossi@example.com")

    def test_display_name_form(self):
        self.assertEqual(normalize_address("Mario Rossi <mario@example.com>"), "mario@example.com")

    def test_empty_and_null(self):
        for raw in (None, "", "   ", "<>", "<", "\r\n"):
            self.assertIsNone(normalize_address(raw), raw)

    def test_control_characters_are_removed(self):
        self.assertEqual(normalize_address("a@b.it\r\nBcc: evil@x"), "a@b.itbcc:evil@x")

    def test_length_is_capped(self):
        raw = "a" * 400 + "@example.com"
        self.assertLessEqual(len(normalize_address(raw)), 320)

    def test_from_header(self):
        self.assertEqual(
            from_header_address("Mario Rossi <mario@example.com>"), "mario@example.com"
        )
        self.assertEqual(
            from_header_address("=?utf-8?q?Mario_Rossi?= <mario@example.com>"),
            "mario@example.com",
        )
        self.assertIsNone(from_header_address("nessun indirizzo"))

    def test_domain_and_shape(self):
        self.assertEqual(address_domain("a@b.example"), "b.example")
        self.assertIsNone(address_domain("senza-chiocciola"))
        self.assertTrue(is_address_like("a@b.it"))
        self.assertFalse(is_address_like("a b"))
        self.assertFalse(is_address_like(None))


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.defaults()
        self.policy = Policy(self.config)

    def test_external_message(self):
        allowed, reason = self.policy.evaluate("mario@example.com", "203.0.113.9", None)
        self.assertTrue(allowed)
        self.assertEqual(reason, "external")

    def test_authenticated_is_skipped(self):
        allowed, reason = self.policy.evaluate("mario@example.com", "203.0.113.9", "mario")
        self.assertFalse(allowed)
        self.assertEqual(reason, "authenticated")

    def test_trusted_network_is_skipped(self):
        allowed, reason = self.policy.evaluate("mario@example.com", "127.0.0.1", None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "trusted-network")
        allowed, reason = self.policy.evaluate("mario@example.com", "::1", None)
        self.assertFalse(allowed)

    def test_local_pickup_is_skipped(self):
        allowed, reason = self.policy.evaluate("mario@example.com", None, None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "local")

    def test_null_sender(self):
        allowed, reason = self.policy.evaluate(None, "203.0.113.9", None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "null-sender")

    def test_invalid_sender(self):
        allowed, reason = self.policy.evaluate("non-un-indirizzo", "203.0.113.9", None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "invalid-sender")

    def test_local_sender_domain(self):
        config = Config.from_overrides({"policy": {"skip_local_senders": "example.com"}})
        allowed, reason = Policy(config).evaluate("mario@example.com", "203.0.113.9", None)
        self.assertFalse(allowed)
        self.assertEqual(reason, "local-sender")

    def test_opt_in_for_local_and_authenticated(self):
        config = Config.from_overrides(
            {
                "policy": {
                    "process_authenticated": "true",
                    "process_local": "true",
                    "trusted_networks": "",
                }
            }
        )
        policy = Policy(config)
        self.assertTrue(policy.evaluate("a@b.it", "127.0.0.1", "mario")[0])
        self.assertTrue(policy.evaluate("a@b.it", None, None)[0])


class KeyTest(unittest.TestCase):
    def test_key_scope_address_and_domain(self):
        address_cfg = Config.from_overrides({"policy": {"key_scope": "address"}})
        domain_cfg = Config.from_overrides({"policy": {"key_scope": "domain"}})
        self.assertEqual(Policy(address_cfg).key_for("mario@example.com"), "mario@example.com")
        self.assertEqual(Policy(domain_cfg).key_for("mario@example.com"), "example.com")


if __name__ == "__main__":
    unittest.main()
