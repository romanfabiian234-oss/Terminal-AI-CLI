"""Fallback SGR mouse parsing when Apple curses has no BUTTON5_PRESSED."""
import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.paste import PasteDecoder
from terminal_ai.ui import App, configure_mouse, wheel_scroll_step
from terminal_ai.wheel import SGRWheelDecoder


class OldMacSGRWheelTests(unittest.TestCase):
    def setUp(self):
        self.decoder = SGRWheelDecoder()

    def feed(self, chars):
        events = []
        for char in chars:
            events.extend(self.decoder.feed(char))
        return events

    def test_both_directions_and_fragmented_events(self):
        self.assertEqual(self.feed("\x1b[<64;2;3M"), [("wheel", "up")])
        self.assertEqual(self.decoder.feed("\x1b[<6"), [])
        self.assertEqual(self.decoder.feed("5;10;10M"), [("wheel", "down")])

    def test_modifiers_and_no_click_input_leak(self):
        self.assertEqual(self.feed("\x1b[<69;999;123M"), [("wheel", "down")])
        self.assertEqual(self.feed("\x1b[<80;23;60M"), [("wheel", "up")])
        self.assertEqual(self.feed("\x1b[<0;1;2M"), [])
        self.assertEqual(self.feed("\x1b[<65;1;2m"), [])

    def test_untrusted_malformed_mouse_event_dropped(self):
        self.assertEqual(self.feed("\x1b[<65;1;xM"), [])
        self.assertEqual(self.feed("\x1b[<65;1;2" + "9" * 80 + "M"), [])

    def test_bracketed_paste_stays_intact_without_auto_executing(self):
        paste = "\x1b[200~pwd\nwhoami\x1b[201~"
        downstream = PasteDecoder()
        events = []
        for kind, value in self.feed(paste):
            self.assertEqual(kind, "key")
            events.extend(downstream.feed(value))
        self.assertEqual(events, [("paste", "pwd\nwhoami")])

    def test_keyboard_is_preserved(self):
        self.assertEqual(self.decoder.feed(curses.KEY_UP), [("key", curses.KEY_UP)])
        self.assertEqual(self.feed("abc"), [("key", "a"), ("key", "b"), ("key", "c")])

    def test_sgr_fallback_selected_without_button_five_and_cleanly_disabled(self):
        with patch.object(curses, "BUTTON5_PRESSED", None, create=True), \
             patch("terminal_ai.ui.os.isatty", return_value=True), \
             patch("terminal_ai.ui.os.write") as write, \
             patch("terminal_ai.ui.curses.mousemask") as mask:
            self.assertTrue(configure_mouse(True))
            self.assertFalse(configure_mouse(False))
        self.assertEqual(write.call_args_list[0].args[1], b"\x1b[?1006h\x1b[?1000h")
        self.assertEqual(write.call_args_list[1].args[1], b"\x1b[?1000l\x1b[?1006l")
        self.assertEqual(mask.call_count, 2)

    @unittest.skipUnless(getattr(curses, "BUTTON5_PRESSED", None),
                         "Native button 5 absent; native branch covered elsewhere")
    def test_modern_curses_uses_native_mouse(self):
        with patch("terminal_ai.ui.enable_mouse_wheel") as enable, \
             patch("terminal_ai.ui.curses.mousemask") as mask:
            self.assertFalse(configure_mouse(True))
            self.assertFalse(configure_mouse(False))
        enable.assert_called_once()
        mask.assert_called_once_with(0)

    def test_wheel_directions_update_scroll_and_diagnostics(self):
        app = App(load_config(Path("/tmp/terminal-ai-missing-sgr-test")))
        app.scroll_wheel("up", 60)
        self.assertEqual(app.scroll, wheel_scroll_step(60))
        app.scroll_wheel("down", 60)
        self.assertEqual(app.scroll, 0)
        self.assertEqual(app.scroll_wheel_events, 2)


if __name__ == "__main__":
    unittest.main()
