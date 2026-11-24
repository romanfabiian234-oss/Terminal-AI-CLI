"""Verify Codex is isolated from previous terminal output without global config edits."""
import shlex
import unittest

from terminal_ai.ui import fullscreen_launch_command


class CodexAlternateScreenTests(unittest.TestCase):
    def test_interactive_codex_gets_per_launch_alternate_buffer(self):
        for command in ("codex", "codex resume", "codex fork", "codex -m gpt-6"):
            with self.subTest(command=command):
                args = shlex.split(fullscreen_launch_command(command))
                self.assertEqual(args[:3], ["codex", "-c", 'tui.alternate_screen="always"'])

    def test_user_override_and_non_codex_commands_preserved(self):
        for command in (
            "codex --no-alt-screen",
            "codex -c 'tui.alternate_screen=\"never\"'",
            'codex exec "summarize this"',
            "vim README.md",
            "codex | cat",
            "codex \"unterminated",
            "codex $EXTRA_ARGS",
        ):
            with self.subTest(command=command):
                self.assertEqual(fullscreen_launch_command(command), command)


if __name__ == "__main__":
    unittest.main()
