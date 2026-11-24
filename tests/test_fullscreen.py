"""Fullscreen command routing: never flatten interactive redraws into blocks."""
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import load_config
from terminal_ai.ui import App, needs_fullscreen


class FullscreenRouting(unittest.TestCase):
    def test_standalone_interactive_programs(self):
        for command in ('codex', 'codex resume', 'codex fork', 'codex -m gpt-6', 'vim readme.md',
                        'nvim /tmp/test', 'nano file.txt', 'htop', 'btop'):
            self.assertTrue(needs_fullscreen(command), command)

    def test_noninteractive_and_compound_shell_commands(self):
        for command in ('codex exec "review this"', 'codex --help', 'codex --version',
                        'codex | cat', 'vim; echo done', 'nvim >log',
                        'echo codex', 'python -c "print(1)"',
                        '/bin/echo hello', 'git log', 'codex $(echo bad)',
                        'codex && echo done', 'codex `echo bad`', 'codex "unterminated'):
            self.assertFalse(needs_fullscreen(command), command)

    def test_auto_route_without_starting_captured_process(self):
        app = App(load_config(Path('/tmp/terminal-ai-not-a-real-config-59382')))
        with patch.object(app, 'start') as start:
            for char in 'codex\n':
                app.key(char, 24, 80)
            self.assertEqual(app.raw_command, 'codex')
            start.assert_not_called()
            self.assertIsNone(app.active)
            self.assertFalse(app.blocks)  # The session block is made by the raw handler.

    def test_pending_after_other_command_routes_to_fullscreen(self):
        app = App(load_config(Path('/tmp/terminal-ai-not-a-real-config-59382')))
        # A command in progress must finish before Codex opens its own TUI.
        with patch.object(app, 'start') as start:
            app.active = object()
            for char in 'codex\n':
                app.key(char, 24, 80)
            self.assertEqual(list(app.pending), ['codex'])
            self.assertFalse(hasattr(app, 'raw_command'))
            app.active = None
            app._run_pending()
            self.assertEqual(app.raw_command, 'codex')
            start.assert_not_called()

    def test_explicit_raw_still_works(self):
        app = App(load_config(Path('/tmp/terminal-ai-not-a-real-config-59382')))
        app.meta('/raw arbitrary-program --flag')
        self.assertEqual(app.raw_command, 'arbitrary-program --flag')


if __name__ == '__main__':
    unittest.main()
