"""Inference worker must not keep accepting work after terminal exit."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from terminal_ai.core import AiWorker, Block, ScreenText, load_config
from terminal_ai.ui import App


def config():
    return load_config(Path("/tmp/terminal-ai-nonexistent-worker-shutdown"))


class WorkerShutdownTests(unittest.TestCase):
    def test_idle_worker_stops_and_rejects_new_requests(self):
        worker = AiWorker(config())
        worker.close()
        worker.thread.join(timeout=1)
        self.assertFalse(worker.thread.is_alive())
        worker.close()  # Shutdown is idempotent.
        self.assertFalse(worker.submit(Block(1, "pwd", "/tmp", ScreenText())))

    def test_close_while_worker_has_backlog_discards_queued_requests(self):
        started = threading.Event()
        release = threading.Event()

        def slow_response(*_args):
            started.set()
            release.wait(timeout=2)
            yield "ok"

        worker = AiWorker(config())
        try:
            with patch("terminal_ai.core.iter_explanation", side_effect=slow_response):
                self.assertTrue(worker.submit(Block(1, "echo first", "/tmp", ScreenText())))
                self.assertTrue(started.wait(1), "Model worker did not start")
                for i in range(2, 5):
                    self.assertTrue(worker.submit(Block(i, "echo queued", "/tmp", ScreenText())))
                worker.close()
                self.assertFalse(worker.submit(Block(5, "echo denied", "/tmp", ScreenText())))
                release.set()
                worker.thread.join(timeout=1)
                self.assertFalse(worker.thread.is_alive())
        finally:
            release.set()
            worker.close()

    def test_app_stop_stops_idle_worker(self):
        app = App(config())
        app.stop()
        app.worker.thread.join(timeout=1)
        self.assertFalse(app.worker.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
