"""Regression tests for alternate-screen isolation and the terminal palette."""
import curses
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.ui import configure_colors, enter_app_screen, run_ui
from test_appearance import FakeWindow


class PaletteTests(unittest.TestCase):
    def test_colors_256_and_eight(self):
        with patch.object(curses, 'COLORS', 256, create=True), patch.object(curses, 'init_pair') as pairs:
            configure_colors(-1)
        self.assertEqual(pairs.call_args_list[0].args, (1, 75, -1))
        self.assertEqual(pairs.call_args_list[1].args, (2, curses.COLOR_GREEN, -1))
        self.assertEqual(pairs.call_args_list[-1].args, (5, 114, -1))
        with patch.object(curses, 'COLORS', 8, create=True), patch.object(curses, 'init_pair') as pairs:
            configure_colors(0)
        self.assertEqual(pairs.call_args_list[0].args, (1, curses.COLOR_BLUE, 0))
        self.assertEqual(pairs.call_args_list[-1].args, (5, curses.COLOR_GREEN, 0))

    def test_private_screen_only_when_stdout_is_tty(self):
        with patch('terminal_ai.ui.os.isatty', return_value=False), patch('terminal_ai.ui.os.write') as write:
            enter_app_screen()
            write.assert_not_called()
        with patch('terminal_ai.ui.os.isatty', return_value=True), patch('terminal_ai.ui.os.write') as write:
            enter_app_screen()
            write.assert_called_once_with(1, b'\x1b[?1049h')


class TerminalWindow(FakeWindow):
    def __init__(self, keys):
        super().__init__(24, 96)
        self.keys = iter(keys)

    def keypad(self, flag):
        pass

    def timeout(self, value):
        pass

    def get_wch(self):
        try:
            return next(self.keys)
        except StopIteration:
            raise curses.error()

    def clear(self):
        self.erase()


class RawLifecycle(unittest.TestCase):
    def test_one_fullscreen_handoff_without_nested_screen_switches(self):
        cfg = load_config(Path('/tmp/terminal-ai-unknown-screen-config-811'))
        cfg.shell = shutil.which('bash') or cfg.shell
        screen = TerminalWindow('codex\n/exit\n')
        calls = []

        def run_mock(*args):
            calls.append('child')
            return 0, None

        with patch('terminal_ai.ui.curses.wrapper', side_effect=lambda f: f(screen)), \
             patch('terminal_ai.ui.enter_app_screen', side_effect=lambda: calls.append('private')), \
             patch('terminal_ai.ui.curses.endwin', side_effect=lambda: calls.append('endwin')), \
             patch('terminal_ai.ui.curses.reset_prog_mode', side_effect=lambda: calls.append('resume')), \
             patch('terminal_ai.ui.child_screen', side_effect=lambda active: calls.append('child-screen-on' if active else 'child-screen-off')), \
             patch('terminal_ai.ui.curses.color_pair', return_value=0), \
             patch('terminal_ai.ui.curses.curs_set'), \
             patch('terminal_ai.ui.curses.init_pair'), \
             patch('terminal_ai.ui.curses.start_color'), \
             patch('terminal_ai.ui.curses.use_default_colors'), \
             patch.object(curses, 'COLORS', 256, create=True), \
             patch('terminal_ai.raw.run_raw_command', side_effect=run_mock):
            run_ui(cfg)
        self.assertEqual(calls, ['endwin', 'child-screen-on', 'child', 'child-screen-off', 'resume'])


if __name__ == '__main__':
    unittest.main()
