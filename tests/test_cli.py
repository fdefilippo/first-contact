import unittest

from firstcontact.cli import build_parser


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


if __name__ == "__main__":
    unittest.main()
