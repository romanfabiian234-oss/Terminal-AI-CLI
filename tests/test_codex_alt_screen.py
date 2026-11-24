"""Codex should retain native inline layout inside an isolated clean screen."""
import shlex
import unittest
from unittest.mock import patch

from terminal_ai.ui import codex_inline_screen, child_screen, fullscreen_launch_command


class CodexNativeLayoutTests(unittest.TestCase):
    def test_interactive_codex_uses_inline_layout(self):
        for command in ("codex", "codex resume", "codex fork", "codex -m gpt-6"):
            with self.subTest(command=command):
                args = shlex.split(fullscreen_launch_command(command))
                self.assertEqual(args[:2], ["codex", "--no-alt-screen"])
                self.assertTrue(codex_inline_screen(fullscreen_launch_command(command)))
                self.assertNotIn('tui.alternate_screen="always"', args)

    def test_explicit_user_override_and_other_commands_unchanged(self):
        for command in (
            "codex --no-alt-screen",
            "codex -c 'tui.alternate_screen=\"always\"'",
            'codex exec "summarize"',
            "vim README.md",
            "codex | cat",
            'codex "unterminated',
            "codex $EXTRA_ARGS",
        ):
            with self.subTest(command=command):
                self.assertEqual(fullscreen_launch_command(command), command)

    def test_child_screen_isolated_and_not_destructive(self):
        with patch("terminal_ai.ui.os.isatty", return_value=True), \
             patch("terminal_ai.ui.os.write") as write:
            child_screen(True)
            child_screen(False)
        self.assertEqual(write.call_args_list[0].args[1], b"\x1b[?1049h\x1b[2J\x1b[H")
        self.assertEqual(write.call_args_list[1].args[1], b"\x1b[?1049l")
        self.assertNotIn(b"\x1b[3J", b"".join(c.args[1] for c in write.call_args_list))


if __name__ == "__main__":
    unittest.main()
