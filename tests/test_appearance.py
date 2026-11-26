"""Shell-like visual layout and transparent model response metadata."""
from __future__ import annotations

import time
import curses
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import Block, ScreenText, explain_rules, load_config
from terminal_ai.ui import App


class FakeWindow:
    def __init__(self, height: int = 24, width: int = 100):
        self.height, self.width = height, width
        self.writes: list[tuple[int, int, str, int]] = []
        self.cursor: tuple[int, int] | None = None
        self.redraw_count = 0

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.writes.clear()
        self.cursor = None

    def addnstr(self, y, x, text, limit, attr):
        self.writes.append((y, x, text[:limit], attr))

    def move(self, y, x):
        self.cursor = (y, x)

    def refresh(self):
        pass

    def redrawwin(self):
        self.redraw_count += 1


class ShellAppearance(unittest.TestCase):
    def setUp(self):
        cfg = load_config(Path('/tmp/terminal-ai-missing-config-7635'))
        self.app = App(cfg)
        self.app.hostname = 'MacBook-Air-Fabiian'

    def render(self, screen):
        with patch('terminal_ai.ui.curses.color_pair', side_effect=lambda n: n * 256):
            self.app.render(screen)

    def test_clean_start_only_real_shell_prompt(self):
        self.app.cwd = Path.home()
        screen = FakeWindow()
        self.render(screen)
        self.assertFalse(self.app.blocks)
        self.assertEqual(screen.writes, [(0, 0, 'MacBook-Air-Fabiian ~ % ', 256)])
        self.assertEqual(screen.cursor, (0, len('MacBook-Air-Fabiian ~ % ')))

    def test_left_right_edit_in_middle_and_update_visual_caret(self):
        screen = FakeWindow(24, 100)
        self.app.cwd = Path.home()
        for ch in "echo test":
            self.app.key(ch, 24, 100)
        self.assertEqual(self.app.cursor, len("echo test"))
        self.app.key(curses.KEY_LEFT, 24, 100)
        self.app.key(curses.KEY_LEFT, 24, 100)
        self.render(screen)
        self.assertEqual(screen.cursor, (0, len(self.app.prompt()) + 7))
        self.app.key("X", 24, 100)
        self.assertEqual(self.app.line, "echo teXst")
        self.assertEqual(self.app.cursor, 8)
        self.app.key(curses.KEY_BACKSPACE, 24, 100)
        self.assertEqual(self.app.line, "echo test")
        self.assertEqual(self.app.cursor, 7)
        self.app.key(curses.KEY_DC, 24, 100)
        self.assertEqual(self.app.line, "echo tet")
        self.assertEqual(self.app.cursor, 7)
        self.app.key(curses.KEY_RIGHT, 24, 100)
        self.assertEqual(self.app.cursor, 8)

    def test_home_end_bounds_and_history_caret(self):
        for ch in "echo test":
            self.app.key(ch, 24, 100)
        self.app.key(curses.KEY_HOME, 24, 100)
        self.app.key(curses.KEY_LEFT, 24, 100)
        self.assertEqual(self.app.cursor, 0)
        self.app.key("X", 24, 100)
        self.assertEqual(self.app.line, "Xecho test")
        self.app.key("\x05", 24, 100)  # Ctrl+E
        self.app.key(curses.KEY_RIGHT, 24, 100)
        self.assertEqual(self.app.cursor, len(self.app.line))
        self.app.history.append("pwd")
        self.app.hist_pos = 1
        self.app.key("\x10", 24, 100)  # Ctrl+P
        self.assertEqual(self.app.line, "pwd")
        self.assertEqual(self.app.cursor, 3)

    def test_caret_remains_visible_when_command_is_wider_than_window(self):
        screen = FakeWindow(24, 40)
        for ch in "echo " + "x" * 80:
            self.app.key(ch, 24, 40)
        self.render(screen)
        self.assertLessEqual(screen.cursor[1], 38)
        self.assertGreaterEqual(screen.cursor[1], len(self.app.prompt()))
        for _ in range(75):
            self.app.key(curses.KEY_LEFT, 24, 40)
        self.render(screen)
        self.assertLessEqual(screen.cursor[1], 38)
        self.assertTrue(any(line.startswith(self.app.prompt()) for _, _, line, _ in screen.writes))

    def test_blocks_are_inserted_above_following_prompt(self):
        screen = FakeWindow()
        block = self.app.add_block('echo hi', 'Заглушка', '')
        block.output.feed(b'hi\n')
        block.finish(0)
        self.render(screen)
        texts = [(y, x, s, attr) for y, x, s, attr in screen.writes]
        self.assertFalse(any('Terminal AI CLI' in s for _, _, s, _ in texts))
        heading = next((y, attr) for y, x, s, attr in texts if 'Команда завершилась' in s)
        self.assertEqual(heading[1], 1280 | curses.A_BOLD)
        self.assertEqual([(y, attr) for y, _, s, attr in texts if s == '✓'],
                         [(heading[0], 512 | curses.A_BOLD)])
        prompt_line = next(y for y, _, s, _ in texts if s.startswith('MacBook-Air-Fabiian '))
        self.assertLess(prompt_line, screen.height - 2)
        self.assertEqual(screen.cursor[0], prompt_line)
        self.assertLess(heading[0], prompt_line)
        self.assertTrue(any(y < prompt_line and 'hi' in s for y, _, s, _ in texts))
        self.assertTrue(any(y == prompt_line + 1 and 'isolated' in s
                            for y, _, s, _ in texts))


    def test_prompt_scrolls_out_of_view_when_browsing_earlier_output(self):
        b = self.app.add_block('seq 1 90', 'Готово', '')
        b.output.feed(('\n'.join(map(str, range(1, 91))) + '\n').encode())
        b.finish(0)
        for height in (9, 15, 35):
            screen = FakeWindow(height, 90)
            self.app.key(curses.KEY_PPAGE, height, 90)
            self.render(screen)
            self.assertIsNone(screen.cursor)
            self.assertFalse(any('MacBook-Air-Fabiian' in text
                                 for _, _, text, _ in screen.writes))
            self.assertTrue(all(0 <= y < height for y, _, _, _ in screen.writes))

    def test_prompt_moves_down_with_each_short_command_without_empty_gap(self):
        screen = FakeWindow(45, 100)
        self.render(screen)
        self.assertEqual(screen.cursor[0], 0)
        positions = []
        for n in range(1, 4):
            block = self.app.add_block(f"echo {n}", "Готово", f"Результат {n}")
            block.output.feed((str(n) + "\n").encode())
            block.finish(0)
            self.render(screen)
            positions.append(screen.cursor[0])
            self.assertLess(screen.cursor[0], screen.height - 2)
            self.assertTrue(any(y == screen.cursor[0] - 1 and text.strip() == str(n)
                                for y, _, text, _ in screen.writes))
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(set(positions)), 3)

    def test_prompt_stays_after_blocks_when_output_collapses(self):
        screen = FakeWindow(35, 100)
        b = self.app.add_block("seq 1 150", "Готово", "")
        b.output.feed(("\n".join(map(str, range(1, 151))) + "\n").encode())
        b.finish(0)
        self.render(screen)
        self.assertEqual(screen.cursor[0], screen.height - 2)
        self.app.key("\x14", 35, 100)
        self.render(screen)
        self.assertLess(screen.cursor[0], screen.height - 2)
        self.assertTrue(any("+147 строк" in text for _, _, text, _ in screen.writes))

    def test_external_terminal_clear_forces_repaint_without_erasing_history(self):
        screen = FakeWindow(24, 100)
        self.app.line = 'echo still typing'
        self.app.add_block('pwd', 'Текущая папка', '/tmp')
        count = len(self.app.blocks)
        self.render(screen)
        # Cmd+K is intercepted by Terminal.app: no key reaches App.key().
        # The next periodic forced redraw must reconstruct the same UI.
        screen.writes.clear()
        with patch('terminal_ai.ui.curses.color_pair', side_effect=lambda n: n * 256):
            self.app.render(screen, force_redraw=True)
        self.assertEqual(screen.redraw_count, 1)
        self.assertEqual(len(self.app.blocks), count)
        self.assertEqual(self.app.line, 'echo still typing')
        self.assertLess(screen.cursor[0], screen.height - 2)
        self.assertTrue(any('Текущая папка' in text for _, _, text, _ in screen.writes))
        self.assertTrue(any('echo still typing' in text for _, _, text, _ in screen.writes))

    def test_small_window_message_uses_forced_redraw(self):
        screen = FakeWindow(5, 24)
        with patch('terminal_ai.ui.curses.color_pair', return_value=0):
            self.app.render(screen, force_redraw=True)
        self.assertEqual(screen.redraw_count, 1)
        self.assertTrue(any('Увеличь окно' in text for _, _, text, _ in screen.writes))

    def test_model_name_and_response_time_are_visible(self):
        screen = FakeWindow()
        b = self.app.add_block('/explain', 'Объяснение ИИ', 'Приложение построено.')
        b.ai_profile = 'home'
        b.ai_model = 'qwen3.5:2b'
        b.ai_state = 'ready'
        b.ai_seconds = 1.234
        self.render(screen)
        joined = '\n'.join(s for _, _, s, _ in screen.writes)
        self.assertIn('qwen3.5:2b (home) · ответ за 1.23 с', joined)


    def test_long_output_visible_and_scrollable_without_ctrl_t(self):
        screen = FakeWindow(24, 90)
        b = self.app.add_block('seq 1 150', 'Команда завершилась', 'Выведены числа от 1 до 150')
        b.output.feed(('\n'.join(str(i) for i in range(1, 151)) + '\n').encode())
        b.finish(0)
        self.assertTrue(b.expanded)
        self.assertEqual(len(b.output.snapshot()), 150)
        self.render(screen)
        bottom_view = '\n'.join(text for _, _, text, _ in screen.writes)
        self.assertIn('150', bottom_view)
        self.assertNotIn('+147 строк', bottom_view)
        self.assertTrue(any(y == screen.cursor[0] - 1 and text.strip() == '150'
                            for y, _, text, _ in screen.writes))
        for _ in range(20):
            self.app.key(curses.KEY_PPAGE, 24, 90)
        self.render(screen)
        top_view = '\n'.join(text for _, _, text, _ in screen.writes)
        self.assertNotEqual(top_view, bottom_view)
        self.assertTrue(any(text.strip() == '1' for _, _, text, _ in screen.writes))
        self.assertFalse(any('MacBook-Air-Fabiian' in text
                             for _, _, text, _ in screen.writes))
        self.assertIsNone(screen.cursor)

    def test_typing_while_scrolled_up_returns_to_live_prompt(self):
        screen = FakeWindow(24, 90)
        b = self.app.add_block('seq 1 150', 'Готово', '')
        b.output.feed(('\n'.join(map(str, range(1, 151))) + '\n').encode())
        b.finish(0)
        for _ in range(20):
            self.app.key(curses.KEY_PPAGE, 24, 90)
        self.render(screen)
        self.assertFalse(any('MacBook-Air-Fabiian' in text
                             for _, _, text, _ in screen.writes))
        self.app.key('x', 24, 90)
        self.render(screen)
        self.assertEqual(self.app.scroll, 0)
        self.assertEqual(self.app.line, 'x')
        self.assertIsNotNone(screen.cursor)
        self.assertTrue(any('MacBook-Air-Fabiian' in text and text.endswith('% x')
                            for _, _, text, _ in screen.writes))

    def test_long_output_prompt_only_follows_final_line(self):
        screen = FakeWindow(24, 90)
        b = self.app.add_block('seq 1 150', 'Готово', '')
        b.output.feed(('\n'.join(map(str, range(1, 151))) + '\n').encode())
        b.finish(0)
        self.render(screen)
        prompt_y = next(y for y, _, text, _ in screen.writes if
                        text.startswith('MacBook-Air-Fabiian '))
        self.assertTrue(any(y == prompt_y - 1 and text.strip() == '150'
                            for y, _, text, _ in screen.writes))
        for _ in range(20):
            self.app.key(curses.KEY_PPAGE, 24, 90)
        self.render(screen)
        self.assertTrue(any(text.strip() == '1' for _, _, text, _ in screen.writes))
        self.assertFalse(any('MacBook-Air-Fabiian' in text
                             for _, _, text, _ in screen.writes))
        for _ in range(20):
            self.app.key(curses.KEY_NPAGE, 24, 90)
        self.render(screen)
        self.assertTrue(any(text.strip() == '150' for _, _, text, _ in screen.writes))
        self.assertTrue(any('MacBook-Air-Fabiian' in text
                             for _, _, text, _ in screen.writes))

    def test_ctrl_t_now_collapses_and_reopens_output(self):
        b = self.app.add_block('seq 1 150', 'Команда завершилась', 'Выведены числа от 1 до 150')
        b.output.feed(('\n'.join(str(i) for i in range(1, 151)) + '\n').encode())
        b.finish(0)
        self.assertTrue(b.expanded)
        self.app.key('\x14', 24, 90)
        self.assertFalse(b.expanded)
        screen = FakeWindow(24, 90)
        self.render(screen)
        self.assertTrue(any('+147 строк' in text for _, _, text, _ in screen.writes))
        self.app.key('\x14', 24, 90)
        self.assertTrue(b.expanded)
        self.render(screen)
        self.assertFalse(any('+147 строк' in text for _, _, text, _ in screen.writes))

    def test_full_height_keeps_input_visible_and_no_header(self):
        screen = FakeWindow(12, 90)
        b = self.app.add_block('seq 1 90', 'Выполняется команда', '')
        b.output.feed(('\n'.join(map(str, range(90))) + '\n').encode())
        b.expanded = True
        self.render(screen)
        self.assertEqual(screen.cursor[0], screen.height - 2)
        self.assertFalse(any('Terminal AI CLI' in s for _, _, s, _ in screen.writes))


class ModelMetadata(unittest.TestCase):
    def test_worker_emits_real_request_duration(self):
        cfg = load_config(Path('/tmp/terminal-ai-missing-config-7635'))
        app = App(cfg)
        b = app.add_block('python3 -c "print(1)"', 'OK', '')
        b.finish(0)
        with patch('terminal_ai.core.iter_explanation', side_effect=lambda *a: iter(['OK'])):
            app._queue_ai(b)
            for _ in range(150):
                app.poll()
                if b.ai_seconds is not None:
                    break
                time.sleep(.01)
        self.assertEqual(b.ai_model, cfg.profile().model)
        self.assertEqual(b.ai_profile, cfg.active)
        self.assertEqual(b.ai_state, 'ready')
        self.assertIsNotNone(b.ai_seconds)
        self.assertGreaterEqual(b.ai_seconds, 0)

    def test_specific_error_rules(self):
        self.assertEqual(explain_rules('python -c "import abc123"',
            "ModuleNotFoundError: No module named 'abc123'", 1),
            ('Не найден модуль Python', 'Python не нашёл модуль abc123.'))
        cases = [
            ('permission denied', 'Недостаточно прав'),
            ('FileNotFoundError: test', 'Файл не найден'),
            ('SyntaxError: invalid syntax', 'Ошибка синтаксиса'),
            ('fatal: not a git repository', 'Нет репозитория Git'),
        ]
        for output, title in cases:
            self.assertEqual(explain_rules('dummy', output, 1)[0], title)


if __name__ == '__main__':
    unittest.main()
