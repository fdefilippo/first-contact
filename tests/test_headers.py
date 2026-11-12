import unittest

from firstcontact.headers import (
    build_first_contact_header,
    encode_token,
    fold_header,
    join_headers,
    sanitize_value,
)


class SanitizeTest(unittest.TestCase):
    def test_control_characters_are_neutralised(self):
        self.assertEqual(sanitize_value("a\r\nBcc: evil@example.com"), "a Bcc: evil@example.com")
        self.assertEqual(sanitize_value("a\x00b\x07c"), "a b c")

    def test_length(self):
        self.assertLessEqual(len(sanitize_value("x" * 500, maxlen=80)), 80)

    def test_encode_token(self):
        self.assertEqual(encode_token("mario@example.com"), "mario@example.com")
        self.assertEqual(encode_token("mario rossi"), "mario%20rossi")
        self.assertEqual(encode_token("a\nb"), "a%20b")
        self.assertEqual(encode_token("josè"), "jos%C3%A8")


class FoldTest(unittest.TestCase):
    def test_short_value_is_untouched(self):
        self.assertEqual(fold_header("first-message; sender=a@b"), "first-message; sender=a@b")

    def test_long_value_is_folded(self):
        value = "; ".join("campo%d=valore%d" % (i, i) for i in range(20))
        folded = fold_header(value, width=60)
        lines = folded.split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines[1:]:
            self.assertTrue(line.startswith("\t"))
        for line in lines:
            self.assertLessEqual(len(line), 80)
        for token in value.split("; "):
            self.assertIn(token, folded.replace("\n\t", "; "))

    def test_no_line_over_998(self):
        value = "; ".join(["x" * 100] * 40)
        folded = fold_header(value[:900])
        for line in folded.split("\n"):
            self.assertLessEqual(len(line), 998)


class BuildHeaderTest(unittest.TestCase):
    def build(self, **kwargs):
        params = dict(
            header_name="X-First-Contact",
            sender="mario@example.com",
            recipient="anna@example.it",
            first_seen="2026-11-11T17:12:03Z",
            key_source="envelope",
            key_scope="address",
            client_addr="203.0.113.9",
            helo="mail.example.com",
            message_id="<abc@example.com>",
        )
        params.update(kwargs)
        return build_first_contact_header(**params)

    def test_expected_fields(self):
        name, value = self.build()
        self.assertEqual(name, "X-First-Contact")
        self.assertIn("first-message", value)
        self.assertIn("sender=mario@example.com", value)
        self.assertIn("recipient=anna@example.it", value)
        self.assertIn("first-seen=2026-11-11T17:12:03Z", value)
        self.assertIn("source=envelope", value)
        self.assertIn("scope=address", value)
        self.assertIn("client=203.0.113.9", value)
        self.assertIn("helo=mail.example.com", value)

    def test_ascii_only_and_no_injection(self):
        _, value = self.build(
            sender="mario\r\nBcc: evil@example.com",
            helo="host\nX-Evil: 1",
            message_id="<a@b>\r\nX-Evil: 2",
        )
        self.assertTrue(all(ord(ch) < 127 for ch in value))
        lowered = value.lower()
        # le intestazioni iniettate non devono comparire come campi separati
        self.assertNotIn("\nbcc:", lowered)
        self.assertNotIn("\nx-evil:", lowered)
        self.assertEqual(value.count("\n"), value.count("\n\t"))

    def test_optional_fields(self):
        _, value = self.build(client_addr=None, helo=None, message_id=None)
        self.assertNotIn("client=", value)
        self.assertNotIn("helo=", value)
        self.assertNotIn("msgid=", value)

    def test_resumed_kind_has_days_and_last_seen(self):
        name, value = self.build(
            kind="resumed",
            first_seen="2023-01-01T00:00:00Z",
            last_seen="2026-08-01T10:00:00Z",
            days=102,
        )
        self.assertEqual(name, "X-First-Contact")
        self.assertIn("resumed", value)
        self.assertNotIn("first-message", value)
        self.assertIn("last-seen=2026-08-01T10:00:00Z", value)
        self.assertIn("days=102", value)

    def test_first_message_has_no_resumed_fields(self):
        _name, value = self.build()
        self.assertNotIn("days=", value)
        self.assertNotIn("last-seen=", value)

    def test_join_headers(self):
        headers = [self.build(), self.build(recipient="altro@example.it")]
        text = join_headers(headers)
        self.assertEqual(text.count("X-First-Contact:"), 2)


if __name__ == "__main__":
    unittest.main()
