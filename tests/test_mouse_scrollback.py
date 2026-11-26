"""Mouse-wheel routing: scroll internal blocks, never erase shell history."""
import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, enable_mouse_wheel, enter_app_screen
from test_appearance import FakeWindow


class MouseScrollTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path('/tmp/terminal-ai-missing-config-wheel-tests')))
        self.app.line = 'draft command'

    @unittest.skipUnless(getattr(curses, "BUTTON5_PRESSED", None),
                         "Native button 5 absent; SGR fallback has separate tests")
    def test_wheel_up_and_down_navigate_block_timeline(self):
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 10, 10, 0, curses.BUTTON4_PRESSED)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.assertEqual(self.app.scroll_target, 1)
        self.app.advance_scroll(100, now=1.0)
        self.assertEqual(self.app.scroll, 1)
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 10, 10, 0, curses.BUTTON5_PRESSED)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.app.advance_scroll(100, now=1.1)
        self.assertEqual(self.app.scroll, 0)
        self.assertEqual(self.app.line, 'draft command')

    def test_wheel_speed_scales_with_terminal_height(self):
        self.app.scroll_wheel("up", 24)
        self.assertEqual(self.app.scroll_target, 1)
        self.assertEqual(self.app.scroll, 0)

    @unittest.skipUnless(getattr(curses, "BUTTON5_PRESSED", None),
                         "Native button 5 absent; SGR fallback has separate tests")
    def test_overscrolling_top_never_makes_scrolling_down_stick(self):
        block = self.app.add_block("seq 1 150", "Готово", "Числа от 1 до 150")
        block.output.feed(("\n".join(map(str, range(1, 151))) + "\n").encode())
        block.finish(0)
        window = FakeWindow(16, 100)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(window)
            for _ in range(300):
                self.app.scroll_wheel("up", 16)
            for frame in range(1, 180):
                self.app.advance_scroll(500, now=frame * 0.025)
            self.app.render(window)  # Clamp target to the real boundary.
            top_offset = self.app.scroll
            self.assertGreater(top_offset, 0)
            self.assertIn("Готово", "\n".join(x[2] for x in window.writes))
            self.app.scroll_wheel("down", 16)
            self.app.advance_scroll(top_offset, now=10.0)
            self.assertEqual(self.app.scroll, top_offset - 1)
            self.assertEqual(self.app.line, "draft command")
            self.assertIsNotNone(window.cursor)

    def test_page_up_overscroll_and_resize_are_clamped(self):
        block = self.app.add_block("seq 1 150", "Готово", "Числа от 1 до 150")
        block.output.feed(("\n".join(map(str, range(1, 151))) + "\n").encode())
        block.finish(0)
        window = FakeWindow(15, 100)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(window)
            for _ in range(100):
                self.app.key(curses.KEY_PPAGE, 15, 100)
            self.app.render(window)
            top = self.app.scroll
            self.app.key(curses.KEY_NPAGE, 15, 100)
            self.app.render(window)
            self.assertLess(self.app.scroll, top)
            # A bigger window shortens the history's scroll range.
            self.app.scroll = 10000
            bigger_window = FakeWindow(65, 100)
            self.app.render(bigger_window)
            self.assertLess(self.app.scroll, top)
            self.assertIsNotNone(bigger_window.cursor)

    def test_arrow_key_wheel_fallback_moves_timeline_with_empty_prompt(self):
        block = self.app.add_block("seq 1 150", "Готово", "Числа")
        block.output.feed(("\n".join(map(str, range(1, 151))) + "\n").encode())
        block.finish(0)
        self.app.line = ""
        screen = FakeWindow(24, 100)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(screen)
            self.app.key(curses.KEY_UP, 24, 100)
            self.app.advance_scroll(150, now=1.0)
            self.app.render(screen)
            self.assertEqual(self.app.scroll, 1)
            self.assertEqual(self.app.scroll_arrow_events, 1)
            self.app.key(curses.KEY_DOWN, 24, 100)
            self.app.advance_scroll(150, now=1.1)
            self.app.render(screen)
            self.assertEqual(self.app.scroll, 0)

    def test_typed_prompt_keeps_arrow_history_and_ctrl_p_n_replaces_it(self):
        self.app.history = ["pwd", "whoami"]
        self.app.hist_pos = 2
        self.app.line = "draft command"
        self.app.key(curses.KEY_UP, 24, 100)
        self.assertEqual(self.app.line, "whoami")
        self.app.line = ""
        self.app.key("\x10", 24, 100)
        self.assertEqual(self.app.line, "pwd")
        self.app.key("\x0e", 24, 100)
        self.assertEqual(self.app.line, "whoami")

    def test_scroll_debug_shows_actual_input_counters(self):
        block = self.app.add_block("seq", "Готово", "Числа")
        block.output.feed(("\n".join(map(str, range(1, 151))) + "\n").encode())
        block.finish(0)
        self.app.meta("/scroll-debug on")
        self.app.line = ""
        self.app.key(curses.KEY_UP, 24, 100)
        screen = FakeWindow(24, 100)
        with patch("terminal_ai.ui.curses.color_pair", return_value=0):
            self.app.render(screen)
        status = "\n".join(x[2] for x in screen.writes)
        self.assertIn("стрелки=1", status)
        self.assertIn("колесо=0", status)
        self.app.meta("/scroll-debug off")
        self.assertFalse(self.app.scroll_debug)

    def test_mouse_failure_and_other_buttons_do_not_change_input(self):
        with patch('terminal_ai.ui.curses.getmouse', side_effect=curses.error):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        with patch('terminal_ai.ui.curses.getmouse', return_value=(0, 1, 1, 0, 0)):
            self.app.key(curses.KEY_MOUSE, 24, 100)
        self.assertEqual(self.app.scroll, 0)
        self.assertEqual(self.app.line, 'draft command')

    @unittest.skipUnless(getattr(curses, "BUTTON5_PRESSED", None),
                         "Native BUTTON5 unavailable; use SGR fallback")
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
