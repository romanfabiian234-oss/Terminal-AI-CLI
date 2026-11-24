"""Experimental persistent shell: commands share one PTY, markers never enter the UI.

Works with bash/zsh -ic; actual macOS zsh requires independent verification.
"""
from __future__ import annotations

import fcntl
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
from pathlib import Path


SHELL_SCRIPT = r'''
while IFS= read -r __tai_command; do
  printf '\033]777;%s;BEGIN\007' "$TAI_SESSION_TOKEN"
  eval "$__tai_command"
  __tai_exit=$?
  printf '\033]777;%s;END;%d;%s\007' "$TAI_SESSION_TOKEN" "$__tai_exit" "$PWD"
done
'''


class MarkerStripper:
    """A streaming byte parser for randomized OSC events, preserving output order."""
    def __init__(self, token: str):
        self.prefix = b"\x1b]777;" + token.encode("ascii") + b";"
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        self.buffer.extend(data)
        results: list[tuple[str, object]] = []
        while self.buffer:
            idx = self.buffer.find(self.prefix)
            if idx == -1:
                safe = max(0, len(self.buffer) - len(self.prefix) + 1)
                if safe:
                    results.append(("output", bytes(self.buffer[:safe])))
                    del self.buffer[:safe]
                break
            if idx:
                results.append(("output", bytes(self.buffer[:idx])))
                del self.buffer[:idx]
            end = self.buffer.find(b"\x07", len(self.prefix))
            if end < 0:
                if len(self.buffer) > 16_384:
                    # Malformed marker: do not let an untrusted child grow our buffer.
                    results.append(("output", bytes(self.buffer[:len(self.prefix)])))
                    del self.buffer[:len(self.prefix)]
                    continue
                break
            item = bytes(self.buffer[len(self.prefix):end]).decode("utf-8", "replace")
            del self.buffer[:end + 1]
            if item == "BEGIN":
                results.append(("begin", None))
            elif item.startswith("END;"):
                parts = item.split(";", 2)
                try:
                    code = int(parts[1])
                    if not -255 <= code <= 255:
                        raise ValueError
                    results.append(("end", (code, parts[2] if len(parts) == 3 else "")))
                except (ValueError, IndexError):
                    pass
        return results

    def flush(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


class PersistentShell:
    def __init__(self, shell: str, cwd: Path, rows: int = 24, cols: int = 80):
        if not Path(shell).is_file():
            raise ValueError("Shell executable not found")
        master, slave = pty.openpty()
        self.master = master
        self.rows, self.cols = rows, cols
        self.token = secrets.token_hex(12)
        self.parser = MarkerStripper(self.token)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 3), max(cols, 20), 0, 0))
        env = dict(os.environ, TAI_SESSION_TOKEN=self.token)

        def prepare() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.process = subprocess.Popen(
                [shell, "-ic", SHELL_SCRIPT], cwd=cwd, stdin=slave, stdout=slave,
                stderr=slave, env=env, close_fds=True, preexec_fn=prepare,
            )
            os.set_blocking(master, False)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)

    def send(self, command: str) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("Persistent shell currently accepts single-line commands only")
        self.send_raw((command + "\n").encode("utf-8"))

    def send_raw(self, data: bytes) -> None:
        if self.process.poll() is not None:
            raise OSError("Shell session has ended")
        os.write(self.master, data)

    def poll(self) -> list[tuple[str, object]]:
        events: list[tuple[str, object]] = []
        for _ in range(12):
            try:
                chunk = os.read(self.master, 16_384)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            events.extend(self.parser.feed(chunk))
        if self.process.poll() is not None:
            extra = self.parser.flush()
            if extra:
                events.append(("output", extra))
            events.append(("session_exited", self.process.returncode))
        return events

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", max(rows, 3), max(cols, 20), 0, 0))
        try:
            os.killpg(self.process.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def interrupt(self) -> None:
        # PTY control character is safer than indiscriminately killing the whole shell group.
        self.send_raw(b"\x03")

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass
            self.process.wait(timeout=1)
        os.close(self.master)
