"""Verify line-by-line wheel animation without relying on macOS timing."""
import curses
import unittest
from pathlib import Path

from terminal_ai.core import load_config
from terminal_ai.ui import App, smooth_scroll_step


class SmoothScrollTests(unittest.TestCase):
    def setUp(self):
        self.app = App(load_config(Path("/tmp/terminal-ai-smooth-scroll-test")))

    def test_frame_limit_and_single_row_finish(self):
        self.assertEqual(smooth_scroll_step(0, 100, 0.0, 0.01), (0, 0.0))
        self.assertEqual(smooth_scroll_step(0, 100, 0.0, 0.025), (2, 0.025))
        self.assertEqual(smooth_scroll_step(10, 11, 0.0, 0.025), (11, 0.025))
        self.assertEqual(smooth_scroll_step(100, 0, 0.0, 0.025), (98, 0.025))

    def test_one_wheel_event_is_one_target_row(self):
        self.app.scroll_wheel("up", 65)
        self.assertEqual((self.app.scroll, self.app.scroll_target), (0, 1))
        self.app.advance_scroll(100, now=1.0)
        self.assertEqual(self.app.scroll, 1)
        self.assertFalse(self.app.is_scrolling)

    def test_full_scroll_uses_many_small_steps(self):
        for _ in range(300):
            self.app.scroll_wheel("up", 60)
        self.assertEqual(self.app.scroll_target, 300)
        for frame in range(1, 51):
            before = self.app.scroll
            self.app.advance_scroll(100, now=frame * 0.025)
            self.assertLessEqual(self.app.scroll - before, 2)
        self.assertEqual(self.app.scroll, 100)
        self.assertFalse(self.app.is_scrolling)

    def test_reverse_drops_stale_inertia(self):
        for _ in range(50):
            self.app.scroll_wheel("up", 60)
        for frame in range(1, 6):
            self.app.advance_scroll(100, now=frame * 0.025)
        before = self.app.scroll
        self.app.scroll_wheel("down", 60)
        self.assertEqual(self.app.scroll_target, before - 1)
        self.app.advance_scroll(100, now=1.0)
        self.assertEqual(self.app.scroll, before - 1)

    def test_pageup_overrides_wheel_animation(self):
        for _ in range(50):
            self.app.scroll_wheel("up", 40)
        self.app.key(curses.KEY_PPAGE, 40, 100)
        self.assertIsNone(self.app.scroll_target)
        self.assertEqual(self.app.scroll, 20)

    def test_repaint_after_resize_clamps_motion_target(self):
        for _ in range(100):
            self.app.scroll_wheel("up", 40)
        self.app.advance_scroll(10, now=1.0)
        self.assertLessEqual(self.app.scroll_target, 10)
        self.assertLessEqual(self.app.scroll, 10)


if __name__ == "__main__":
    unittest.main()
