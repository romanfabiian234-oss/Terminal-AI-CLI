import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from terminal_ai.core import (Block, ModelProfile, ScreenText, Config, explain_rules,
                              iter_explanation, load_config, redact, CONFIG_SAMPLE, simple_read_command)
from terminal_ai.ui import App


class ParserTests(unittest.TestCase):
    def test_csi_osc_and_partial_utf8(self):
        screen = ScreenText(10)
        data = 'build\r\x1b[2KOK\n\x1b]0;secret-title\x07Привет\n'.encode('utf-8')
        for byte in data:
            screen.feed(bytes([byte]))
        self.assertEqual(screen.snapshot(), ['OK', 'Привет'])

    def test_carriage_return_progress_no_duplication(self):
        s = ScreenText(3)
        s.feed(b'0%\r40%\r100%\n')
        self.assertEqual(s.snapshot(), ['100%'])
        for i in range(5):
            s.feed(f'{i}\n'.encode())
        self.assertEqual(s.snapshot(), ['2', '3', '4'])

    def test_csi_clear(self):
        s = ScreenText()
        s.feed(b'abcde\rX\x1b[K\n')
        self.assertEqual(s.snapshot(), ['X'])


class ConfigTests(unittest.TestCase):
    def test_builtin_config(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        self.assertEqual(cfg.active, 'local')
        self.assertEqual(cfg.profile().provider, 'ollama')

    def test_remote_requires_explicit_consent_and_https(self):
        with self.assertRaisesRegex(ValueError, 'allow_remote'):
            ModelProfile('remote', 'openai', 'https://example.org/v1', 'small').validate()
        with self.assertRaisesRegex(ValueError, 'allow_insecure_http'):
            ModelProfile('remote', 'openai', 'http://10.0.0.8/v1', 'small', allow_remote=True).validate()
        ModelProfile('remote', 'openai', 'https://example.org/v1', 'small', allow_remote=True).validate()

    def test_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text(CONFIG_SAMPLE.replace('max_blocks = 100', 'max_blocks = 0'))
            with self.assertRaises(ValueError):
                load_config(path)


class RedactionTests(unittest.TestCase):
    def test_tokens(self):
        result = redact('Authorization: Bearer ABCDEFGH password=hello sk-12345678901234567890 ghp_AAAAAABBBBBBCCCCCCDDDD')
        for secret in ('ABCDEFGH', 'hello', 'sk-123456', 'ghp_'):
            self.assertNotIn(secret, result)
        self.assertIn('[REDACTED]', result)

    def test_private_key(self):
        self.assertNotIn('secret', redact('-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----'))


class RuleTests(unittest.TestCase):
    def test_no_false_success(self):
        self.assertIn('пока неизвестен', explain_rules('build', 'PASS', None)[1])
        self.assertIn('кодом 0', explain_rules('true', '', 0)[1])
        self.assertIn('код 9', explain_rules('test', '', 9)[1])

    def test_rule_build(self):
        self.assertIn('Сборка', explain_rules('npm run build', 'Finished', 0)[0])

    def test_git_read_only_rules_and_compound_safety(self):
        for command, kind in (("git status", "git status"),
                              ("git log -3 --oneline", "git log"),
                              ("git --no-pager log -3 --oneline", "git log")):
            self.assertEqual(simple_read_command(command), kind)
            self.assertNotIn("ошиб", explain_rules(command, "sample", 0)[1])
        for command in ("git status; echo x", "git log | head", "git log --help", "git commit -m x"):
            self.assertIsNone(simple_read_command(command))
        self.assertEqual(explain_rules("git log", "fatal error", 1)[0], "Команда завершилась с ошибкой")

    def test_simple_commands_do_not_need_ai(self):
        for command, title in (("ls", "Содержимое папки"), ("ls -la", "Содержимое папки"),
                               ("pwd", "Текущая папка"), ("whoami", "Текущий пользователь")):
            self.assertIsNotNone(simple_read_command(command))
            self.assertEqual(explain_rules(command, "", 0)[0], title)
        for command in ("lsof", "ls; echo second", "ls && echo second", "ls $(echo secret)",
                        "ls --help", "ls > output.txt", "ls | head"):
            self.assertIsNone(simple_read_command(command))
        self.assertEqual(explain_rules("ls", "error", 2)[0], "Команда завершилась с ошибкой")



class ModelTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append((self.path, payload))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if self.path == '/api/chat':
                    for fragment in ({'message': {'content': 'Готово'}, 'done': False}, {'done': True}):
                        self.wfile.write((json.dumps(fragment) + '\n').encode())
                else:
                    for chunk in ('Проверка', ' прошла'):
                        self.wfile.write(('data: ' + json.dumps({'choices': [{'delta': {'content': chunk}}]}) + '\n\n').encode())
                    self.wfile.write(b'data: [DONE]\n\n')

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_ollama(self):
        model = ModelProfile('local', 'ollama', self.url, 'tiny')
        result = ''.join(iter_explanation(model, 'echo hi', 'password=SECRET', 0))
        self.assertEqual(result, 'Готово')
        request = self.requests[0]
        self.assertEqual(request[0], '/api/chat')
        self.assertNotIn('SECRET', json.dumps(request[1]))

    def test_refuses_redirect_before_sending_secrets_to_new_host(self):
        original = self.server
        other = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        observed = []

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header('Location', f'http://127.0.0.1:{other.server_port}/steal')
                self.end_headers()
            def log_message(self, *_):
                pass

        class Trap(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass

        other.RequestHandlerClass = Trap
        redirector = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        threading.Thread(target=redirector.serve_forever, daemon=True).start()
        os.environ['TAI_TEST_SECRET'] = 'test-value'
        try:
            profile = ModelProfile('safe', 'openai', f'http://127.0.0.1:{redirector.server_port}', 'test', 'TAI_TEST_SECRET')
            with self.assertRaises(ValueError):
                list(iter_explanation(profile, 'echo x', 'output', 0))
            self.assertEqual(observed, [])
        finally:
            del os.environ['TAI_TEST_SECRET']
            redirector.shutdown()
            other.shutdown()
            redirector.server_close()
            other.server_close()

    def test_openai_compatible(self):
        model = ModelProfile('local', 'openai', self.url + '/v1', 'model-id')
        self.assertEqual(''.join(iter_explanation(model, 'ls', 'result', 0)), 'Проверка прошла')
        self.assertEqual(self.requests[0][0], '/v1/chat/completions')


class AppTests(unittest.TestCase):
    def test_read_only_commands_skip_ai_unless_requested(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        app = App(cfg)
        calls = []
        app.worker.submit = lambda block: calls.append(block.command) or True
        b = app.add_block('ls', 'temporary', '')
        b.finish(0)
        app._queue_ai(b)
        self.assertEqual(calls, [])
        self.assertEqual(b.title, 'Содержимое папки')
        running = app.add_block('ls', 'temporary', '', 'running')
        app._queue_ai(running)
        self.assertEqual(calls, [])
        app.meta(':explain')  # A still-running command cannot be analyzed as finished.
        self.assertEqual(calls, [])
        self.assertEqual(app.blocks[-1].status, 'error')
        app.blocks.pop()  # Remove the explanatory error block before another test.
        running.finish(0)
        app.meta(':explain')
        self.assertEqual(calls, ['ls'])
        b2 = app.add_block('ls', 'temporary', '')
        b2.finish(2)
        app._queue_ai(b2)
        self.assertEqual(calls, ['ls', 'ls'])

    def test_do_not_run_ai_or_apply_updates_while_process_is_running(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        submitted = []
        app.worker.submit = lambda block: submitted.append(block.id) or True
        try:
            app.start('sleep 0.35', 25, 90)
            b = app.active
            b.output.feed(b'progress ' * 25)
            b.started -= 5  # Force the old 1.5s heuristic to run.
            app.poll()
            self.assertEqual(submitted, [])
            self.assertEqual(b.status, 'running')
            self.assertIn('ещё работает', b.explanation)
            # Delayed model results must not turn an active command into a false error.
            app.worker.events.put((b.id, b.analysis_epoch, 'done', 'Команда завершилась с ошибкой still running'))
            app.poll()
            self.assertIn('ещё работает', b.explanation)
            for _ in range(150):
                app.poll()
                if app.active is None:
                    break
                time.sleep(.01)
            self.assertIsNone(app.active)
            self.assertEqual(b.exit_code, 0)
            self.assertEqual(submitted, [b.id])
        finally:
            app.stop()

    def test_cd_and_unknown_model(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        app = App(cfg)
        app.meta(':model missing')
        self.assertEqual(app.blocks[-1].status, 'error')
        app.meta(':models')
        self.assertIn('local', app.blocks[-1].explanation)
        app.meta(':language uk')
        self.assertEqual(cfg.language, 'uk')
        app.meta(':server local')
        self.assertEqual(cfg.active, 'local')


if __name__ == '__main__':
    unittest.main()

# Real PTY smoke tests run locally (with bash if zsh is unavailable).
class PTYTests(unittest.TestCase):
    def test_runs_shell_command(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.start('printf "PTY_TEST_OK\\n"', 25, 90)
            for _ in range(200):
                app.poll()
                if app.active is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(app.active)
            self.assertEqual(app.blocks[-1].exit_code, 0)
            self.assertIn('PTY_TEST_OK', app.blocks[-1].output.excerpt())
        finally:
            app.stop()

    def test_multiline_paste_creates_separate_blocks(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        app.ai_on = False
        try:
            # Simulate pasting several newline-separated commands faster than
            # the first process can finish. Each needs its own block.
            for ch in 'pwd\nwhoami\ndate\n':
                app.key(ch, 25, 90)
            self.assertEqual([b.command for b in app.blocks], ['pwd'])
            self.assertEqual(list(app.pending), ['whoami', 'date'])
            for _ in range(400):
                app.poll()
                if app.active is None and not app.pending:
                    break
                time.sleep(0.01)
            self.assertEqual([b.command for b in app.blocks], ['pwd', 'whoami', 'date'])
            self.assertTrue(all(b.exit_code == 0 for b in app.blocks))
            # Later command text must never be forwarded to the active PTY.
            self.assertNotIn('whoami', app.blocks[0].output.excerpt())
            self.assertNotIn('date', app.blocks[0].output.excerpt())
            self.assertNotIn('date', app.blocks[1].output.excerpt())
        finally:
            app.stop()

    def test_ctrl_c_clears_queued_commands(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.start('sleep 8', 25, 90)
            for ch in 'echo SHOULD_NOT_RUN\n':
                app.key(ch, 25, 90)
            self.assertEqual(list(app.pending), ['echo SHOULD_NOT_RUN'])
            app.key('\x03', 25, 90)
            self.assertEqual(list(app.pending), [])
            for _ in range(300):
                app.poll()
                if app.active is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(app.active)
            self.assertEqual([b.command for b in app.blocks], ['sleep 8'])
            self.assertEqual(app.line, '')
        finally:
            app.stop()

    def test_ctrl_g_allows_explicit_interactive_stdin(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.start('read name; printf "received:%s\\n" "$name"', 25, 90)
            app.key('\x07', 25, 90)
            self.assertTrue(app.direct_input)
            for ch in 'hello\n':
                app.key(ch, 25, 90)
            for _ in range(300):
                app.poll()
                if app.active is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(app.active)
            self.assertIn('received:hello', app.blocks[-1].output.excerpt())
            self.assertFalse(app.direct_input)
        finally:
            app.stop()

    def test_git_log_no_interactive_pager(self):
        import shutil
        import subprocess
        import tempfile
        import time
        from unittest.mock import patch
        shell = shutil.which('bash') or shutil.which('zsh')
        git = shutil.which('git')
        if not shell or not git:
            self.skipTest('shell or git unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([git, 'init', '-q', tmp], check=True)
            subprocess.run([git, '-C', tmp, '-c', 'user.name=Test',
                            '-c', 'user.email=local@example.org', '-c', 'commit.gpgsign=false',
                            'commit', '--quiet', '--allow-empty', '-m', 'PAGER_TEST'], check=True)
            cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
            cfg.shell = shell
            app = App(cfg)
            app.cwd = Path(tmp)
            app.ai_on = False
            try:
                with patch.dict(os.environ, {'GIT_PAGER': 'less', 'PAGER': 'less'}):
                    app.start('printf "PAGER=%s GIT_PAGER=%s\n" "$PAGER" "$GIT_PAGER"; git log -3 --oneline', 25, 90)
                for _ in range(300):
                    app.poll()
                    if app.active is None:
                        break
                    time.sleep(.01)
                self.assertIsNone(app.active, 'Git log was stuck in an interactive pager')
                b = app.blocks[-1]
                self.assertEqual(b.exit_code, 0)
                self.assertIn('PAGER=cat GIT_PAGER=cat', b.output.excerpt())
                self.assertIn('PAGER_TEST', b.output.excerpt())
                self.assertNotIn('(END)', b.output.excerpt())
            finally:
                app.stop()

    def test_nonzero_exit_status(self):
        import shutil
        import time
        shell = shutil.which('bash') or shutil.which('zsh')
        if not shell:
            self.skipTest('shell unavailable')
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        cfg.shell = shell
        app = App(cfg)
        app.ai_on = False
        try:
            app.start('exit 7', 25, 90)
            for _ in range(200):
                app.poll()
                if app.active is None:
                    break
                time.sleep(0.01)
            self.assertIsNone(app.active)
            self.assertEqual(app.blocks[-1].exit_code, 7)
            self.assertEqual(app.blocks[-1].status, 'error')
        finally:
            app.stop()
