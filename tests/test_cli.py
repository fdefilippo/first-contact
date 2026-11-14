import unittest
import contextlib
import io
import os
import tempfile
from unittest import mock

from firstcontact.cli import build_parser, main
from firstcontact.store import ContactStore


class ParserTest(unittest.TestCase):
    def test_config_before_subcommand(self):
        args = build_parser().parse_args(["--config", "/tmp/a.ini", "serve"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.config, "/tmp/a.ini")

    def test_config_after_subcommand(self):
        # e' la forma usata dalla unit systemd
        args = build_parser().parse_args(["serve", "--config", "/tmp/b.ini"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.config, "/tmp/b.ini")

    def test_config_default_when_absent(self):
        args = build_parser().parse_args(["serve"])
        self.assertEqual(args.config, "/etc/first-contact/config.ini")

    def test_options_of_subcommand_still_work(self):
        args = build_parser().parse_args(
            [
                "check",
                "--config",
                "/tmp/c.ini",
                "--sender",
                "mario@example.com",
                "--recipient",
                "anna@example.it",
            ]
        )
        self.assertEqual(args.command, "check")
        self.assertEqual(args.config, "/tmp/c.ini")
        self.assertEqual(args.sender, "mario@example.com")


class DiagnosticStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="first-contact-cli-")
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "contacts.db")
        self.config = os.path.join(self.temp.name, "config.ini")
        with open(self.config, "w") as handle:
            handle.write("[store]\npath = " + self.path + "\n")
        # Identity switching is qualified separately with real root/service UIDs.
        patcher = mock.patch("firstcontact.cli.pwd.getpwnam", side_effect=KeyError)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, command):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["--config", self.config] + command)

    def test_queries_do_not_initialize_missing_database(self):
        for command in (["stats"], ["list"],
                        ["check", "--sender", "a@x.it", "--recipient", "b@y.it"],
                        ["prune", "--days", "10", "--dry-run"], ["doctor"]):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command), 1)
                self.assertFalse(os.path.exists(self.path))
                self.assertFalse(os.path.exists(self.path + "-wal"))
                self.assertFalse(os.path.exists(self.path + "-shm"))

    def test_queries_preserve_existing_contacts(self):
        with ContactStore(self.path) as store:
            store.first_contact("a@x.it", "b@y.it")
        for command in (["stats"], ["list"],
                        ["check", "--sender", "a@x.it", "--recipient", "b@y.it"]):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command), 0)
        with ContactStore(self.path, readonly=True) as store:
            self.assertEqual(store.lookup("a@x.it", "b@y.it")["messages"], 1)


if __name__ == "__main__":
    unittest.main()
