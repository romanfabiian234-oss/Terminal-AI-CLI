"""Cancellation must be distinct from a command failure and never invoke AI."""
import signal
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from terminal_ai.core import Block, ScreenText, load_config
from terminal_ai.ui import App
from test_appearance import FakeWindow


class CancelledCommandTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path("/tmp/terminal-ai-nonexistent-interrupt-status")))
        self.app.ai_on = True

    def tearDown(self):
        self.app.stop()

    def test_ctrl_c_marks_child_interrupted_without_ai_request(self):
        block = self.app.add_block("sleep 10", "Команда выполняется", "", "running")
        self.app.active = block
        self.app.process = SimpleNamespace(pid=12345)
        with patch("terminal_ai.ui.os.killpg") as killpg:
            self.app.key("\x03", 24, 90)
            killpg.assert_called_once_with(12345, signal.SIGINT)
        self.assertTrue(block.cancel_requested)
        self.app.process = None
        with patch.object(self.app.worker, "submit") as submit:
            self.app._finish(-signal.SIGINT)
            submit.assert_not_called()
        self.assertEqual(block.exit_code, -signal.SIGINT)
        self.assertEqual(block.status, "interrupted")
        self.assertEqual(block.title, "Команда прервана")
        self.assertIn("Ctrl+C", block.explanation)
        self.assertIsNone(self.app.active)

    def test_cancelled_command_is_neutral_not_red_failure(self):
        block = self.app.add_block("sleep 10", "Запуск", "", "running")
        block.cancel_requested = True
        block.finish(-signal.SIGINT)
        screen = FakeWindow(24, 90)
        with patch("terminal_ai.ui.curses.color_pair", side_effect=lambda x: x * 256):
            self.app.render(screen)
        self.assertTrue(any("Команда прервана" in value for _, _, value, _ in screen.writes))
        self.assertTrue(any(value.startswith("– ") for _, _, value, _ in screen.writes))
        self.assertFalse(any("Команда завершилась с ошибкой" in value for _, _, value, _ in screen.writes))

    def test_unrelated_nonzero_exit_remains_failure(self):
        block = Block(1, "false", "/tmp", ScreenText())
        block.finish(1)
        self.assertEqual(block.status, "error")
        self.assertIn("ошибкой", block.title)

    def test_success_after_ignored_cancellation_remains_success(self):
        block = Block(1, "echo done", "/tmp", ScreenText())
        block.cancel_requested = True
        block.finish(0)
        self.assertEqual(block.status, "ok")
        self.assertEqual(block.exit_code, 0)


if __name__ == "__main__":
    unittest.main()
