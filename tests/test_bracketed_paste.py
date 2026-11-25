"""Pasted commands require explicit Enter for each command."""
import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.paste import PasteDecoder, prepare_pasted_commands
from terminal_ai.ui import App, set_bracketed_paste


class PasteTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path("/tmp/terminal-ai-missing-paste-config")))
        self.app.ai_on = False

    def test_multiline_paste_does_not_execute_anything_until_enter(self):
        with patch.object(self.app, "start") as start:
            for key in "\x1b[200~pwd\nwhoami\ndate\nls\n\x1b[201~":
                self.app.key(key, 24, 90)
            self.assertEqual(start.call_count, 0)
            self.assertEqual(self.app.line, "pwd")
            self.assertEqual(list(self.app.paste_review), ["whoami", "date", "ls"])
            for expected in ("pwd", "whoami", "date", "ls"):
                self.assertEqual(self.app.line, expected)
                self.app.key("\n", 24, 90)
            self.assertEqual([x.args[0] for x in start.call_args_list],
                             ["pwd", "whoami", "date", "ls"])
            self.assertFalse(self.app.paste_review)

    def test_single_line_paste_also_waits_for_enter(self):
        with patch.object(self.app, "start") as start:
            for key in "\x1b[200~echo HELLO\x1b[201~":
                self.app.key(key, 24, 90)
            start.assert_not_called()
            self.assertEqual(self.app.line, "echo HELLO")
            self.app.key("\n", 24, 90)
            start.assert_called_once()

    def test_cancel_prevents_unconfirmed_execution(self):
        with patch.object(self.app, "start") as start:
            for key in "\x1b[200~echo FIRST\necho SECOND\x1b[201~":
                self.app.key(key, 24, 90)
            self.app.key("\x03", 24, 90)
            self.assertFalse(self.app.line)
            self.assertFalse(self.app.paste_review)
            start.assert_not_called()

    def test_typed_enter_still_executes_normally(self):
        with patch.object(self.app, "start") as start:
            for key in "pwd\n":
                self.app.key(key, 24, 90)
            start.assert_called_once()

    def test_active_direct_stdin_gets_pasted_text(self):
        self.app.active = object()
        self.app.direct_input = True
        with patch.object(self.app, "_send") as send:
            for key in "\x1b[200~hello\n\x1b[201~":
                self.app.key(key, 24, 90)
            send.assert_called_once_with(b"hello\n")

    def test_reject_bad_control_and_large_pastes(self):
        with self.assertRaises(ValueError):
            prepare_pasted_commands("ls\n\x1b[3J", "")
        with self.assertRaises(ValueError):
            prepare_pasted_commands("echo a\n" * 40, "")

    def test_oversized_decoder_never_emits_paste_event(self):
        decoder = PasteDecoder()
        result = decoder.feed(decoder.START + "x" * (decoder.MAX_CHARS + 1) + decoder.END)
        self.assertEqual(result[0][0], "error")

    def test_enable_and_disable_bracketed_mode(self):
        with patch("terminal_ai.ui.os.isatty", return_value=True), \
             patch("terminal_ai.ui.os.write") as write:
            set_bracketed_paste(True)
            set_bracketed_paste(False)
        self.assertEqual(write.call_args_list[0].args[1], b"\x1b[?2004h")
        self.assertEqual(write.call_args_list[1].args[1], b"\x1b[?2004l")


if __name__ == "__main__":
    unittest.main()
