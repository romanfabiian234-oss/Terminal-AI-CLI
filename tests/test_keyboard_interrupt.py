"""macOS Ctrl+C may raise KeyboardInterrupt from curses instead of returning ETX."""
import curses
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, read_input_event
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

    def test_curses_ctrl_c_key_still_cancels_normally(self):
        screen = Mock()
        screen.get_wch.return_value = "\x03"
        self.app.line = "unfinished"
        self.assertTrue(read_input_event(screen, self.app, 24, 90, True, self.decoder))
        self.assertEqual(self.app.line, "")
        self.assertFalse(self.app.should_exit)


if __name__ == "__main__":
    unittest.main()
