import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from terminal_ai.core import (Block, ModelProfile, ScreenText, Config, explain_rules,
                              iter_explanation, load_config, redact, CONFIG_SAMPLE)
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
    def test_cd_and_unknown_model(self):
        cfg = load_config(Path('/tmp/terminal-ai-unlikely-unexisting-file-8175'))
        app = App(cfg)
        app.meta(':model missing')
        self.assertEqual(app.blocks[-1].status, 'error')
        app.meta(':models')
        self.assertIn('local', app.blocks[-1].explanation)


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
