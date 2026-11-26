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


    def test_pasted_commands_are_all_visible_before_approval(self):
        from test_appearance import FakeWindow
        app = self.app
        app.hostname = "MacBook-Air-Fabiian"
        for key in "\x1b[200~pwd\nwhoami\ndate\nls\n\x1b[201~":
            app.key(key, 24, 100)
        screen = FakeWindow(24, 100)
        with patch("terminal_ai.ui.curses.color_pair", side_effect=lambda index: index * 256):
            app.render(screen)
        visible = [part for _, _, part, _ in screen.writes]
        self.assertTrue(any(part.startswith("MacBook-Air-Fabiian ") and part.endswith(" % pwd") for part in visible))
        self.assertIn("  2. whoami", visible)
        self.assertIn("  3. date", visible)
        self.assertIn("  4. ls", visible)
        prompt_y = next(y for y, _, text, _ in screen.writes if text.startswith("MacBook-Air-Fabiian "))
        self.assertEqual(prompt_y, 0)
        previews = [y for y, _, text, _ in screen.writes if text.startswith("  2.") or
                    text.startswith("  3.") or text.startswith("  4.")]
        self.assertEqual(previews, [prompt_y + 1, prompt_y + 2, prompt_y + 3])
        # During an unconfirmed paste the safety notice replaces normal status.
        self.assertTrue(app.paste_feedback)
        self.assertTrue(any(y == prompt_y + 4 and app.paste_feedback in text
                            for y, _, text, _ in screen.writes))
        self.assertFalse(app.blocks)
        self.assertFalse(app.pending)

    def test_preview_updates_after_approving_one_and_canceling_rest(self):
        from test_appearance import FakeWindow
        with patch.object(self.app, "start") as start:
            for key in "\x1b[200~pwd\nwhoami\ndate\nls\x1b[201~":
                self.app.key(key, 24, 90)
            self.app.key("\n", 24, 90)
            start.assert_called_once()
            screen = FakeWindow(24, 90)
            with patch("terminal_ai.ui.curses.color_pair", return_value=0):
                self.app.render(screen)
            visible = [part for _, _, part, _ in screen.writes]
            self.assertTrue(any(part.endswith(" % whoami") for part in visible))
            self.assertIn("  2. date", visible)
            self.assertIn("  3. ls", visible)
            self.assertNotIn("  2. whoami", visible)
            self.app.key("\x03", 24, 90)
            screen = FakeWindow(24, 90)
            with patch("terminal_ai.ui.curses.color_pair", return_value=0):
                self.app.render(screen)
            self.assertFalse(any("  2. date" in part or "  3. ls" in part for _, _, part, _ in screen.writes))

    def test_small_window_keeps_prompt_and_preview_summary(self):
        from test_appearance import FakeWindow
        self.app.hostname = "mac"
        self.app.cwd = Path.home()
        for key in "\x1b[200~" + "\n".join(f"echo {i}" for i in range(12)) + "\x1b[201~":
            self.app.key(key, 7, 45)
        screen = FakeWindow(7, 45)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(screen)
        lines = [part for _, _, part, _ in screen.writes]
        self.assertTrue(any(" % echo 0" in part for part in lines))
        self.assertTrue(any("ещё" in part for part in lines))
        self.assertIsNotNone(screen.cursor)

    def test_single_line_paste_also_waits_for_enter(self):
        with patch.object(self.app, "start") as start:
            for key in "\x1b[200~echo HELLO\x1b[201~":
                self.app.key(key, 24, 90)
            start.assert_not_called()
            self.assertEqual(self.app.line, "echo HELLO")
            self.app.key("\n", 24, 90)
            start.assert_called_once()

    def test_paste_at_caret_preserves_suffix_and_requires_enter(self):
        with patch.object(self.app, "start") as start:
            for key in "echo AB":
                self.app.key(key, 24, 90)
            self.app.key(curses.KEY_LEFT, 24, 90)
            for key in "\x1b[200~X\x1b[201~":
                self.app.key(key, 24, 90)
            self.assertEqual(self.app.line, "echo AXB")
            self.assertEqual(self.app.cursor, len("echo AX"))
            start.assert_not_called()
            self.app.key("\n", 24, 90)
            start.assert_called_once_with("echo AXB", 24, 90)

    def test_multiline_paste_at_caret_never_auto_executes(self):
        with patch.object(self.app, "start") as start:
            for key in "echo AB":
                self.app.key(key, 24, 90)
            self.app.key(curses.KEY_LEFT, 24, 90)
            for key in "\x1b[200~X\nwhoami\x1b[201~":
                self.app.key(key, 24, 90)
            self.assertEqual(self.app.line, "echo AX")
            self.assertEqual(self.app.cursor, len(self.app.line))
            self.assertEqual(list(self.app.paste_review), ["whoamiB"])
            start.assert_not_called()

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
