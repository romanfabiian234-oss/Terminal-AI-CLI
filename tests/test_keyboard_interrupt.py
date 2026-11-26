"""macOS Ctrl+C may raise KeyboardInterrupt from curses instead of returning ETX."""
import curses
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, read_input_event, run_ui
from terminal_ai.wheel import SGRWheelDecoder


class CtrlCInputTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path("/tmp/terminal-ai-nonexistent-ctrlc-tests")))
        self.app.ai_on = False
        self.decoder = SGRWheelDecoder()

    def tearDown(self):
        self.app.stop()

    def test_keyboard_interrupt_clears_input_and_queue_without_exiting(self):
        self.app.line = "half-written command"
        self.app.cursor = len(self.app.line)
        self.app.pending.append("echo should-not-execute")
        self.app.paste_review.append("echo unapproved")
        screen = Mock()
        screen.get_wch.side_effect = KeyboardInterrupt
        self.assertTrue(read_input_event(screen, self.app, 24, 90, False, self.decoder))
        self.assertEqual(self.app.line, "")
        self.assertEqual(self.app.cursor, 0)
        self.assertEqual(list(self.app.pending), [])
        self.assertEqual(list(self.app.paste_review), [])
        self.assertFalse(self.app.should_exit)

    def test_keyboard_interrupt_sends_sigint_to_active_child_not_app(self):
        block = self.app.add_block("sleep 10", "Running", "", "running")
        self.app.active = block
        self.app.pending.append("echo no")
        screen = Mock()
        screen.get_wch.side_effect = KeyboardInterrupt
        with patch.object(self.app, "process", SimpleNamespace(pid=12345)):
            with patch("terminal_ai.ui.os.killpg") as killpg:
                self.assertTrue(read_input_event(screen, self.app, 24, 90, False, self.decoder))
                killpg.assert_called_once_with(12345, signal.SIGINT)
        self.assertFalse(self.app.pending)
        self.assertFalse(self.app.should_exit)
        self.app.active = None

    def test_regular_input_still_works_after_keyboard_interrupt(self):
        screen = Mock()
        screen.get_wch.side_effect = [KeyboardInterrupt, "x", curses.error()]
        self.assertTrue(read_input_event(screen, self.app, 24, 90, False, self.decoder))
        self.assertTrue(read_input_event(screen, self.app, 24, 90, False, self.decoder))
        self.assertEqual(self.app.line, "x")
        self.assertFalse(read_input_event(screen, self.app, 24, 90, False, self.decoder))
        self.assertFalse(self.app.should_exit)

    def test_sigint_received_by_curses_main_loop_cancels_without_exit(self):
        cfg = load_config(Path("/tmp/terminal-ai-nonexistent-ctrlc-tests"))
        cfg.shell = "/bin/sh"
        app = Mock()
        app.should_exit = False
        app.raw_command = ""  # No fullscreen handoff pending.
        app.poll.return_value = False
        app.is_scrolling = False

        def cancel_input(key, _rows, _cols):
            self.assertEqual(key, "\x03")
            app.should_exit = True

        app.key.side_effect = cancel_input
        screen = Mock()
        screen.getmaxyx.return_value = (24, 90)
        installed = []
        original_handler = signal.default_int_handler

        def fake_signal(_sig, handler):
            installed.append(handler)
            return original_handler

        def send_signal():
            self.assertTrue(installed)
            installed[0](signal.SIGINT, None)
            raise curses.error("interrupted")

        screen.get_wch.side_effect = send_signal

        with patch("terminal_ai.ui.App", return_value=app), \
             patch("terminal_ai.ui.curses.wrapper", side_effect=lambda fn: fn(screen)), \
             patch("terminal_ai.ui.curses.start_color"), \
             patch("terminal_ai.ui.curses.use_default_colors"), \
             patch("terminal_ai.ui.curses.curs_set"), \
             patch("terminal_ai.ui.configure_colors"), \
             patch("terminal_ai.ui.configure_mouse", return_value=False), \
             patch("terminal_ai.ui.set_bracketed_paste"), \
             patch("terminal_ai.ui.signal.signal", side_effect=fake_signal):
            run_ui(cfg)

        app.key.assert_called_once_with("\x03", 24, 90)
        app.stop.assert_called_once()
        self.assertEqual(len(installed), 2)
        self.assertIs(installed[-1], original_handler)

    def test_curses_ctrl_c_key_still_cancels_normally(self):
        screen = Mock()
        screen.get_wch.return_value = "\x03"
        self.app.line = "unfinished"
        self.assertTrue(read_input_event(screen, self.app, 24, 90, True, self.decoder))
        self.assertEqual(self.app.line, "")
        self.assertFalse(self.app.should_exit)


if __name__ == "__main__":
    unittest.main()
