"""Check outer and child alternate screens are not redundantly nested."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import pexpect
except ImportError:
    pexpect = None


@unittest.skipIf(pexpect is None, "pexpect is an optional test dependency")
class AlternateScreenPTY(unittest.TestCase):
    def test_one_screen_activation_per_interactive_program(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "codex"
            executable.write_text('#!/bin/sh\nprintf "\\033[?1049h\\033[2J\\033[HCHILD_TUI\\n"\nsleep 0.10\nprintf "\\033[?1049l"\n')
            executable.chmod(0o755)
            config = Path(temp) / "config.toml"
            config.write_text(
                '[app]\nshell = "/bin/bash"\n[models]\nactive = "local"\n'
                '[models.local]\nprovider = "ollama"\n'
                'base_url = "http://127.0.0.1:11434"\nmodel = "qwen3.5:2b"\n'
            )
            env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=str(root))
            process = pexpect.spawn(
                sys.executable, ["-m", "terminal_ai", "--config", str(config)],
                cwd=temp, env=env, dimensions=(24, 100), encoding=None, timeout=10
            )
            try:
                self.assertEqual(process.expect([b"\\x1b\\[\\?1049h", pexpect.EOF]), 0)
                time.sleep(.2)
                process.sendline(str(executable))
                time.sleep(.7)
                process.sendline("/exit")
                process.expect(pexpect.EOF)
                process.close()
                self.assertEqual(process.exitstatus, 0)
                self.assertIn(b"CHILD_TUI", process.before)
                self.assertEqual(process.before.count(b"\x1b[?1049h"), 2)
            finally:
                if process.isalive():
                    process.close(force=True)
