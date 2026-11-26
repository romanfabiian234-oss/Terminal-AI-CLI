"""Streaming protocol and real persistent PTY regression tests (Linux bash)."""
import shutil
import time
import unittest
from pathlib import Path

from terminal_ai.core import load_config
from terminal_ai.session import MarkerStripper, PersistentShell
from terminal_ai.ui import App


class EventParsing(unittest.TestCase):
    def test_split_marker_and_order(self):
        token = 'randomtoken'
        s = MarkerStripper(token)
        sequence = b'abc\x1b]777;randomtoken;BEGIN\x07xyz\x1b]777;randomtoken;END;0;/tmp\x07done'
        events = []
        for i in range(0, len(sequence), 2):
            events.extend(s.feed(sequence[i:i + 2]))
        events.append(('output', s.flush()))
        output = b''.join(value for key, value in events if key == 'output')
        self.assertEqual(output, b'abcxyzdone')
        self.assertIn(('begin', None), events)
        self.assertIn(('end', (0, '/tmp')), events)

    def test_malformed_marker_cannot_grow_unbounded(self):
        s = MarkerStripper('t')
        s.feed(b'\x1b]777;t;' + b'A' * 50_000)
        self.assertLess(len(s.buffer), 17_000)


class PersistentPTY(unittest.TestCase):
    def setUp(self):
        self.shell = shutil.which('bash')
        if not self.shell:
            self.skipTest('bash unavailable')

    def test_session_state_and_cwd(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = self.shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.meta(':session on')
            self.assertIsNotNone(app.session)
            for cmd in ('export TAI_SHELL_TEST=preserved', 'printf "value=%s\\n" "$TAI_SHELL_TEST"', "alias taisay='echo alias-preserved'", 'taisay', 'cd /tmp', 'pwd'):
                app.start(cmd, 25, 100)
                for _ in range(300):
                    app.poll()
                    if app.active is None:
                        break
                    time.sleep(.01)
                self.assertIsNone(app.active, cmd)
                self.assertEqual(app.blocks[-1].exit_code, 0)
            self.assertIn('value=preserved', app.blocks[-5].output.excerpt())
            self.assertIn('alias-preserved', app.blocks[-3].output.excerpt())
            self.assertEqual(str(app.cwd), '/tmp')
            self.assertIn('/tmp', app.blocks[-1].output.excerpt())
            app.meta(':session off')
            self.assertIsNone(app.session)
        finally:
            app.stop()

    def test_queued_commands_preserve_session_state(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = self.shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.meta(':session on')
            self.assertIsNotNone(app.session)
            for char in 'export TAI_QUEUE_TEST=preserved\nprintf "session:%s\\n" "$TAI_QUEUE_TEST"\npwd\n':
                app.key(char, 25, 100)
            for _ in range(350):
                app.poll()
                if app.active is None and not app.pending:
                    break
                time.sleep(.01)
            blocks = [b for b in app.blocks if b.command in ('export TAI_QUEUE_TEST=preserved', 'printf "session:%s\\n" "$TAI_QUEUE_TEST"', 'pwd')]
            self.assertEqual(len(blocks), 3)
            self.assertEqual([b.exit_code for b in blocks], [0, 0, 0])
            self.assertIn('session:preserved', blocks[1].output.excerpt())
            self.assertNotIn('pwd', blocks[1].output.excerpt())
        finally:
            app.stop()

    def test_persistent_shell_disables_pagers_in_managed_blocks(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = self.shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.meta(':session on')
            self.assertIsNotNone(app.session)
            app.start('echo "PAGER=$PAGER GIT_PAGER=$GIT_PAGER"', 25, 90)
            for _ in range(300):
                app.poll()
                if app.active is None:
                    break
                time.sleep(.01)
            self.assertIsNone(app.active)
            self.assertIn('PAGER=cat GIT_PAGER=cat', app.blocks[-1].output.excerpt())
        finally:
            app.stop()

    def test_nonzero_exit_and_shell_survives(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = self.shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.meta(':session on')
            for cmd, code in [('false', 1), ('echo still-running', 0)]:
                app.start(cmd, 25, 90)
                for _ in range(300):
                    app.poll()
                    if app.active is None:
                        break
                    time.sleep(.01)
                self.assertIsNone(app.active)
                self.assertEqual(app.blocks[-1].exit_code, code)
            self.assertIn('still-running', app.blocks[-1].output.excerpt())
            self.assertEqual(app.blocks[-1].output.snapshot()[0].strip(), 'still-running')
        finally:
            app.stop()

    def test_interrupt_foreground_job_preserves_shell(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = self.shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.meta(':session on')
            app.start('sleep 8', 25, 90)
            time.sleep(0.25)
            app.key('\x03', 25, 90)
            for _ in range(300):
                app.poll()
                if app.active is None:
                    break
                time.sleep(.01)
            self.assertIsNone(app.active)
            self.assertIsNotNone(app.session)
            self.assertEqual(app.blocks[-1].status, 'interrupted')
            self.assertNotEqual(app.blocks[-1].exit_code, 0)
            self.assertEqual(app.blocks[-1].title, 'Команда прервана')
            app.start('echo shell-preserved', 25, 90)
            for _ in range(300):
                app.poll()
                if app.active is None:
                    break
                time.sleep(.01)
            self.assertIsNone(app.active)
            self.assertIn('shell-preserved', app.blocks[-1].output.excerpt())
        finally:
            app.stop()


if __name__ == '__main__':
    unittest.main()
