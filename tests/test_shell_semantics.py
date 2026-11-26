"""Daily shell input: never swallow compound commands or execute them on parse."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, is_meta_command, simple_cd_target


class ShellSemantics(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path("/tmp/terminal-ai-nonexistent-shell-semantics")))
        self.app.ai_on = False

    def tearDown(self):
        self.app.stop()

    def queue(self, command):
        self.app.pending.append(command)
        self.app._run_pending()

    def test_standalone_cd_parsing(self):
        for command, target in (
            ("cd", "~"),
            ("cd ~/Documents", "~/Documents"),
            ('cd "folder with spaces"', "folder with spaces"),
            ('cd "a; b"', "a; b"),
            ('cd "a && b"', "a && b"),
            ("cd -- /tmp", "/tmp"),
            ("cd -", "-"),
            ("cd /tmp # comment", "/tmp"),
        ):
            with self.subTest(command=command):
                self.assertEqual(simple_cd_target(command), target)

    def test_compound_redirections_expansions_and_flags_use_real_shell(self):
        unsafe_to_intercept = (
            "cd /tmp && pwd", "cd /tmp; pwd", "cd /tmp | cat",
            "cd /tmp > log", "cd /tmp || echo no",
            "cd /tmp && echo second", "cd /tmp\npwd",
            'cd "$(pwd)"', "cd /tmp/*.txt", "cd -P /tmp",
            "cd /tmp /var", 'cd "unterminated',
        )
        for command in unsafe_to_intercept:
            with self.subTest(command=command):
                self.assertIsNone(simple_cd_target(command))
                with patch.object(self.app, "start") as start:
                    self.queue(command)
                    start.assert_called_once_with(command, self.app.last_rows, self.app.last_cols)
                self.assertFalse(self.app.pending)

    def test_quoted_metacharacters_are_just_directory_names(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            folder = base / "A; B && C"
            folder.mkdir()
            self.app.cwd = base
            self.queue('cd "A; B && C"')
            self.assertEqual(self.app.cwd, folder)
            self.assertEqual(self.app.blocks[-1].status, "ok")

    def test_cd_dash_toggles_previous_directory(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            folder = base / "child"
            folder.mkdir()
            self.app.cwd = base
            self.queue("cd child")
            self.assertEqual(self.app.cwd, folder)
            self.assertEqual(self.app.previous_cwd, base)
            self.queue("cd -")
            self.assertEqual(self.app.cwd, base)
            self.queue("cd -")
            self.assertEqual(self.app.cwd, folder)

    def test_cd_dash_without_history_fails_without_changing_directory(self):
        prior = self.app.cwd
        self.queue("cd -")
        self.assertEqual(self.app.cwd, prior)
        self.assertEqual(self.app.blocks[-1].status, "error")

    def test_cd_failure_does_not_corrupt_navigation_state(self):
        with tempfile.TemporaryDirectory() as root:
            self.app.cwd = Path(root)
            previous = self.app.cwd
            self.queue("cd folder-that-does-not-exist")
            self.assertEqual(self.app.cwd, previous)
            self.assertIsNone(self.app.previous_cwd)
            self.assertEqual(self.app.blocks[-1].status, "error")

    def test_posix_colon_is_a_shell_builtin_not_app_command(self):
        self.assertFalse(is_meta_command(":"))
        self.assertFalse(is_meta_command(": unknown"))
        self.assertTrue(is_meta_command(":help"))
        self.assertTrue(is_meta_command("/help"))
        self.assertFalse(is_meta_command("/bin/echo hello"))
        with patch.object(self.app, "start") as start:
            self.queue(":")
            start.assert_called_once_with(":", self.app.last_rows, self.app.last_cols)


if __name__ == "__main__":
    unittest.main()
