"""Opening a provider's sign-in when it's signed out, and telling "signed out" apart from other read failures."""
import json
import os
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from geekmagic.providers import PROVIDERS, claude, codex
from geekmagic.render.views import error as error_view
from geekmagic.errors import SignInNeeded, UsageError
from geekmagic.model import ErrorScreen
from geekmagic.system import login


class TerminalCommandTests(unittest.TestCase):
    def test_windows_gets_one_command_line_that_survives_spaces_in_the_path(self):
        command = login.terminal_command([r"C:\Program Files\Claude Code\claude.exe", "auth", "login"], "Claude sign-in", "win32")
        self.assertEqual(command, r'cmd.exe /c start "Claude sign-in" cmd.exe /k ""C:\Program Files\Claude Code\claude.exe" auth login"')

    def test_windows_command_without_spaces(self):
        command = login.terminal_command(["codex.cmd"], "Codex sign-in", "win32")
        self.assertEqual(command, 'cmd.exe /c start "Codex sign-in" cmd.exe /k "codex.cmd"')

    def test_macos_opens_terminal_and_quotes_what_it_runs(self):
        command = login.terminal_command(["/Users/me/my tools/claude", "auth", "login"], "t", "darwin")
        self.assertEqual(command[:2], ["osascript", "-e"])
        script = command[-1]
        self.assertIn('do script "', script)
        self.assertIn("'/Users/me/my tools/claude' auth login", script)

    def test_macos_script_cannot_be_broken_out_of(self):
        command = login.terminal_command(['/tmp/x"; do shell script "rm -rf ~"; "'], "t", "darwin")
        body = command[-1].split('do script "', 1)[1]
        self.assertNotIn('rm -rf ~"; "', body.replace('\\"', ""), "every quote inside the command is escaped")

    def test_linux_uses_the_first_terminal_it_finds(self):
        found = {"gnome-terminal", "xterm"}
        with patch.object(login.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}" if name in found else None):
            self.assertEqual(login.terminal_command(["codex"], "t", "linux"), ["gnome-terminal", "--", "codex"])
        with patch.object(login.shutil, "which", return_value=None):
            self.assertIsNone(login.terminal_command(["codex"], "t", "linux"))


class LaunchTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"GEEKMAGIC_NO_LOGIN": ""})  # (the suite turns it on; these tests are about it)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_provider_signs_in_with_its_own_command(self):
        self.assertEqual({key: provider.login_args for key, provider in PROVIDERS.items()},
                         {"claude": ("auth", "login"), "codex": ("login",)})
        for provider in PROVIDERS.values():
            self.assertTrue(provider.install_hint, f"{provider.key} says how to install its CLI")

    def test_the_kill_switch_opens_nothing(self):
        with patch.dict(os.environ, {"GEEKMAGIC_NO_LOGIN": "1"}), patch.object(login.subprocess, "Popen") as popen:
            self.assertEqual(login.launch("claude"), "failed")
        popen.assert_not_called()

    def test_a_missing_cli_is_reported_not_attempted(self):
        with patch.object(login, "executable", return_value=None), patch.object(login.subprocess, "Popen") as popen:
            self.assertEqual(login.launch("codex"), "missing")
        popen.assert_not_called()

    def test_it_opens_the_terminal_with_the_providers_command(self):
        with patch.object(login, "executable", return_value="/bin/codex"), \
                patch.object(login, "terminal_command", return_value=["term", "codex", "login"]) as build, \
                patch.object(login.subprocess, "Popen") as popen:
            self.assertEqual(login.launch("codex"), "started")
        build.assert_called_once_with(["/bin/codex", "login"], "Codex sign-in")
        self.assertEqual(popen.call_args.args[0], ["term", "codex", "login"])

    def test_no_terminal_or_a_failing_start_is_reported(self):
        with patch.object(login, "executable", return_value="/bin/claude"), patch.object(login, "terminal_command", return_value=None):
            self.assertEqual(login.launch("claude"), "failed")
        with patch.object(login, "executable", return_value="/bin/claude"), \
                patch.object(login, "terminal_command", return_value=["term"]), \
                patch.object(login.subprocess, "Popen", side_effect=OSError("no")):
            self.assertEqual(login.launch("claude"), "failed")

    def test_codex_is_looked_for_where_the_usage_query_looks(self):
        with patch.object(codex, "find_codex", return_value="/ext/codex"):
            self.assertEqual(login.executable("codex"), "/ext/codex")
        with patch.object(claude, "which", side_effect=lambda name: f"/bin/{name}"):
            self.assertEqual(login.executable("claude"), "/bin/claude")


class SignedOutDetectionTests(unittest.TestCase):
    def run_claude(self, stdout="", stderr="", returncode=0):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        with patch.object(claude.subprocess, "run", return_value=done):
            return claude.fetch_usage()

    def test_claude_signed_out_is_told_apart_from_other_failures(self):
        for text in ("Not logged in · Please run /login", "Invalid API key · Please run /login", "authentication_error"):
            with self.assertRaises(SignInNeeded, msg=text):
                self.run_claude(stderr=text, returncode=1)
            with self.assertRaises(SignInNeeded, msg=text):
                self.run_claude(stdout=json.dumps({"is_error": True, "result": text, "total_cost_usd": 0}))
        with self.assertRaises(UsageError) as caught:
            self.run_claude(stderr="segmentation fault", returncode=139)
        self.assertNotIsInstance(caught.exception, SignInNeeded)
        with self.assertRaises(UsageError) as caught:
            self.run_claude(stdout=json.dumps({"is_error": True, "result": "something odd", "total_cost_usd": 0}))
        self.assertNotIsInstance(caught.exception, SignInNeeded)

    def test_the_error_screen_asks_for_the_sign_in(self):
        from datetime import datetime
        screen = ErrorScreen("Codex", "Codex could not read account limits. Not signed in?", datetime.now().astimezone())
        plain = error_view.render_error_frame(screen)
        asking = error_view.render_error_frame(replace(screen, signin=True))
        self.assertEqual(plain.size, (240, 240))
        self.assertNotEqual(plain.tobytes(), asking.tobytes(), "the heading says what to do")


if __name__ == "__main__":
    unittest.main()
