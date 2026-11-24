"""Exercise the uninstaller with every system mutation replaced by a stub."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class UninstallTest(unittest.TestCase):
    def run_uninstaller(self, current, apply=True):
        with tempfile.TemporaryDirectory(prefix="first-contact-uninstall-") as directory:
            log = os.path.join(directory, "commands.jsonl")
            stub = '''import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.environ["FC_TEST_LOG"], "a") as handle:
    handle.write(json.dumps([name] + sys.argv[1:]) + "\\n")
if name == "id":
    print("0")
if name == "postconf" and "-h" in sys.argv:
    print(os.environ["FC_TEST_MILTERS"])
'''
            for command in ("id", "postconf", "cp", "postfix", "systemctl", "rm"):
                path = os.path.join(directory, command)
                with open(path, "w") as handle:
                    handle.write("#!" + sys.executable + "\n" + stub)
                os.chmod(path, 0o755)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ["PATH"],
                       FC_TEST_LOG=log, FC_TEST_MILTERS=current)
            script = Path(__file__).resolve().parents[1] / "scripts" / "uninstall.sh"
            args = ["bash", str(script)] + (["--apply"] if apply else [])
            result = subprocess.run(args, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(log) as handle:
                return [json.loads(line) for line in handle]

    def test_only_milter_can_be_removed_and_data_is_preserved(self):
        commands = self.run_uninstaller("unix:/first-contact/milter.sock")
        self.assertIn(["postconf", "-e", "smtpd_milters = "], commands)
        self.assertIn(["systemctl", "disable", "--now", "first-contact-milter"], commands)
        self.assertNotIn(["rm", "-rf", "/var/lib/first-contact"], commands)

    def test_other_milters_remain_in_order(self):
        commands = self.run_uninstaller(
            "inet:127.0.0.1:11332 unix:/first-contact/milter.sock unix:/dkim/milter.sock")
        self.assertIn(["postconf", "-e", "smtpd_milters = inet:127.0.0.1:11332 unix:/dkim/milter.sock"], commands)

    def test_comma_separated_milter_is_removed_before_service_is_stopped(self):
        first_contact = "unix:/first-contact/milter.sock"
        rspamd = "inet:127.0.0.1:11332"
        dkim = "unix:/dkim/milter.sock"
        grouped_dkim = "{ " + dkim + ", connect_timeout=10s, default_action=accept }"
        cases = (
            (first_contact + "," + rspamd, rspamd),
            (rspamd + ", " + first_contact + " " + dkim, rspamd + " " + dkim),
            (rspamd + " ," + first_contact, rspamd),
            (grouped_dkim + "," + first_contact, grouped_dkim),
            (rspamd + ", { " + first_contact + ", default_action=accept }", rspamd),
        )
        for current, remaining in cases:
            with self.subTest(current=current):
                commands = self.run_uninstaller(current)
                edit = commands.index(["postconf", "-e", "smtpd_milters = " + remaining])
                reload = commands.index(["systemctl", "reload", "postfix"])
                stop = commands.index(["systemctl", "disable", "--now", "first-contact-milter"])
                self.assertLess(edit, reload)
                self.assertLess(reload, stop)

    def test_empty_or_absent_token_does_not_reconfigure_postfix(self):
        for current in ("", "inet:127.0.0.1:11332"):
            with self.subTest(current=current):
                commands = self.run_uninstaller(current)
                self.assertFalse(any(command[:2] == ["postconf", "-e"] for command in commands))
                self.assertIn(["systemctl", "disable", "--now", "first-contact-milter"], commands)

    def test_dry_run_does_not_mutate_system(self):
        commands = self.run_uninstaller("unix:/first-contact/milter.sock", apply=False)
        self.assertEqual(commands, [["postconf", "-h", "smtpd_milters"]])


if __name__ == "__main__":
    unittest.main()
