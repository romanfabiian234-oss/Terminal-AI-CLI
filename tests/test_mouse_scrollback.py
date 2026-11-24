"""Mouse-wheel routing: scroll internal blocks, never erase shell history."""
import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, enable_mouse_wheel, enter_app_screen


class MouseScrollTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path('/tmp/terminal-ai-missing-config-wheel-tests')))
        self.app.line = 'draft command'

    def test_wheel_up_and_down_navigate_block_timeline(self):
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 10, 10, 0, curses.BUTTON4_PRESSED)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.assertEqual(self.app.scroll, 3)
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 10, 10, 0, curses.BUTTON5_PRESSED)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.assertEqual(self.app.scroll, 0)
        self.assertEqual(self.app.line, 'draft command')

    def test_mouse_failure_and_other_buttons_do_not_change_input(self):
        with patch('terminal_ai.ui.curses.getmouse', side_effect=curses.error):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 1, 1, 0, 0)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.assertEqual(self.app.scroll, 0)
        self.assertEqual(self.app.line, 'draft command')

    def test_wheel_registration_does_not_capture_clicks_or_drag(self):
        with patch('terminal_ai.ui.curses.mouseinterval') as interval, patch('terminal_ai.ui.curses.mousemask') as mask:
            enable_mouse_wheel()
        interval.assert_called_once_with(0)
        mask.assert_called_once_with(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)

    def test_private_screen_does_not_erase_main_scrollback(self):
        with patch('terminal_ai.ui.os.isatty', return_value=True), patch('terminal_ai.ui.os.write') as write:
            enter_app_screen()
        self.assertEqual(write.call_args.args[1], b'\x1b[?1049h')
        self.assertNotIn(b'\x1b[3J', write.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
