"""Transparent PTY mode for full-screen applications such as Codex CLI or Vim."""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import sys
import termios
import tty
from pathlib import Path

from .core import ScreenText


def run_raw_command(shell: str, command: str, cwd: Path, max_lines: int) -> tuple[int, ScreenText]:
    """Keep the child's original terminal rendering; collect only a bounded transcript.

    AI explanations are displayed after returning to block mode. This avoids
    corrupting fullscreen interfaces with unrelated status messages.
    """
    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        raise ValueError("Direct PTY mode requires an interactive terminal")
    output = ScreenText(max_lines)
    parent, slave = pty.openpty()
    old_attrs = termios.tcgetattr(sys.stdin.fileno())
    winsz = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\0' * 8)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)
    pid = os.fork()
    if pid == 0:
        try:
            os.close(parent)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            if slave > 2:
                os.close(slave)
            os.chdir(cwd)
            os.execv(shell, [shell, '-lc', command])
        except BaseException:
            os._exit(127)
    os.close(slave)
    old_handler = signal.getsignal(signal.SIGWINCH)

    def resize(*_args: object) -> None:
        try:
            dims = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\0' * 8)
            fcntl.ioctl(parent, termios.TIOCSWINSZ, dims)
            os.killpg(pid, signal.SIGWINCH)
        except OSError:
            pass

    status = 1
    try:
        signal.signal(signal.SIGWINCH, resize)
        tty.setraw(sys.stdin.fileno())
        os.set_blocking(parent, False)
        while True:
            readable, _, _ = select.select([parent, sys.stdin.fileno()], [], [], 0.1)
            if sys.stdin.fileno() in readable:
                user_input = os.read(sys.stdin.fileno(), 4096)
                if user_input:
                    try:
                        os.write(parent, user_input)
                    except OSError:
                        pass
            if parent in readable:
                try:
                    data = os.read(parent, 16384)
                except (BlockingIOError, OSError):
                    data = b''
                if not data:
                    break
                output.feed(data)
                os.write(sys.stdout.fileno(), data)
        _, status = os.waitpid(pid, 0)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attrs)
        signal.signal(signal.SIGWINCH, old_handler)
        os.close(parent)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    return os.waitstatus_to_exitcode(status), output
