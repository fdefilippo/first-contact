import os
import tempfile
import unittest

from firstcontact.config import Config, ConfigError


class DefaultsTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        config = Config.defaults()
        self.assertEqual(config.socket, "unix:/var/spool/postfix/first-contact/milter.sock")
        self.assertEqual(config.header_name, "X-First-Contact")
        self.assertEqual(config.header_position, "top")
        self.assertEqual(config.key_source, "envelope")
        self.assertEqual(config.key_scope, "address")
        self.assertFalse(config.observe_only)
        self.assertEqual(config.max_recipients, 50)
        self.assertEqual(len(config.trusted_networks), 2)
        self.assertEqual(config.banner_mode, "body")
        self.assertIn("allegati", config.banner_text)
        self.assertEqual(config.subject_tag, "")
        self.assertEqual(config.max_body_bytes, 20971520)
        self.assertTrue(config.skip_signed)
        self.assertTrue(config.html_box)
        self.assertIsNone(config.whitelist_file)
        self.assertEqual(config.dormant_days, 90)
        self.assertIn("non ti scriveva da", config.banner_text_resumed)

    def test_banner_modes(self):
        for mode in ("off", "body", "subject", "both"):
            config = Config.from_overrides({"banner": {"mode": mode}})
            self.assertEqual(config.banner_mode, mode)

    def test_banner_bad_values(self):
        cases = [
            {"banner": {"mode": "sempre"}},
            {"banner": {"max_body_bytes": "-1"}},
            {"banner": {"subject_tag": "prima\nseconda"}},
            {"banner": {"skip_signed": "forse"}},
            {"banner": {"html_box": "forse"}},
        ]
        for overrides in cases:
            with self.assertRaises(ConfigError, msg=overrides):
                config = Config.from_overrides(overrides)
                config.skip_signed  # forza la validazione del booleano

    def test_whitelist_file_is_expanded(self):
        config = Config.from_overrides({"whitelist": {"file": "~/whitelist.txt"}})
        self.assertTrue(config.whitelist_file.endswith("whitelist.txt"))
        self.assertTrue(os.path.isabs(config.whitelist_file))
        config = Config.from_overrides({"whitelist": {"file": "  "}})
        self.assertIsNone(config.whitelist_file)

    def test_inet_socket_forms(self):
        for spec in ("inet:11334", "inet:11334@127.0.0.1", "inet6:11334@::1"):
            config = Config.from_overrides({"milter": {"socket": spec}})
            self.assertEqual(config.socket, spec)
            self.assertIsNone(config.socket_path)

    def test_unix_socket_must_be_absolute(self):
        with self.assertRaises(ConfigError):
            Config.from_overrides({"milter": {"socket": "unix:relative.sock"}})
        with self.assertRaises(ConfigError):
            Config.from_overrides({"milter": {"socket": "tcp:11334"}})

    def test_bad_values(self):
        cases = [
            ({"milter": {"header_position": "middle"}}, ConfigError),
            ({"policy": {"key_source": "subject"}}, ConfigError),
            ({"policy": {"key_scope": "domainX"}}, ConfigError),
            ({"policy": {"trusted_networks": "10.0.0.0/99"}}, ConfigError),
            ({"policy": {"max_recipients": "0"}}, ConfigError),
            ({"policy": {"dormant_days": "-1"}}, ConfigError),
            ({"policy": {"dormant_days": "tanti"}}, ConfigError),
            ({"logging": {"level": "LOUD"}}, ConfigError),
            ({"milter": {"header_name": "X Bad"}}, ConfigError),
        ]
        for overrides, exc in cases:
            with self.assertRaises(exc):
                Config.from_overrides(overrides)

    def test_bad_boolean(self):
        config = Config.from_overrides({"policy": {"observe_only": "maybe"}})
        with self.assertRaises(ConfigError):
            config.observe_only


class LoadTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".ini")
        os.close(handle)

    def tearDown(self):
        os.unlink(self.path)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_load_overrides_and_warns(self):
        self.write(
            """
[milter]
header_name = X-Nuovo-Mittente
socket = inet:11334@127.0.0.1

[policy]
observe_only = yes
trusted_networks = 127.0.0.0/8, 192.168.0.0/24
sconosciuta = 1
"""
        )
        config = Config.load(self.path)
        self.assertEqual(config.header_name, "X-Nuovo-Mittente")
        self.assertEqual(config.socket, "inet:11334@127.0.0.1")
        self.assertTrue(config.observe_only)
        self.assertEqual(len(config.trusted_networks), 2)
        self.assertEqual(len(config.warnings), 1)
        self.assertIn("sconosciuta", config.warnings[0])

    def test_unknown_section_is_an_error(self):
        self.write("[sbagliata]\nx = 1\n")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            Config.load(os.path.join(os.path.dirname(self.path), "non-esiste.ini"))

    def test_broken_ini(self):
        self.write("questo non e' un ini\n")
        with self.assertRaises(ConfigError):
            Config.load(self.path)


if __name__ == "__main__":
    unittest.main()
