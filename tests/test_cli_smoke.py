"""Offline CLI smoke checks: argument passing, status and model listings."""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.cli import main


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.config_path = Path(self.root.name) / "missing-config.toml"

    def test_models_lists_default_profile_without_a_model_server(self):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            result = main(["--config", str(self.config_path), "models"])
        self.assertEqual(result, 0)
        self.assertIn("local:", capture.getvalue())

    def test_explicit_run_passes_argv_without_shell_interpretation(self):
        args = ["run", "--", "/bin/echo", "hello; no extra command"]
        with patch("terminal_ai.cli.subprocess.run",
                   return_value=subprocess.CompletedProcess(args, 0)) as run:
            result = main(["--config", str(self.config_path), *args])
        self.assertEqual(result, 0)
        run.assert_called_once_with(["/bin/echo", "hello; no extra command"], check=False)

    def test_real_noninteractive_run_returns_process_exit_status(self):
        with contextlib.redirect_stdout(io.StringIO()):
            result = main(["--config", str(self.config_path), "run", "--",
                           sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(result, 7)

    def test_invalid_config_is_reported_as_error(self):
        self.config_path.write_text("[models\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as capture:
            result = main(["--config", str(self.config_path), "models"])
        self.assertEqual(result, 2)
        self.assertIn("Config error:", capture.getvalue())


if __name__ == "__main__":
    unittest.main()
