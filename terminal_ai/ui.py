"""A minimal Codex-like timeline over explicit shell commands (Python stdlib curses)."""
from __future__ import annotations

import curses
import fcntl
import os
import pty
import socket
import shlex
import signal
import subprocess
import struct
import sys
import termios
import textwrap
import time
from collections import deque
from pathlib import Path

from .core import AiWorker, Block, Config, ScreenText, explain_rules, simple_read_command
from .session import PersistentShell
from .paste import PasteDecoder, prepare_pasted_commands
from .wheel import SGRWheelDecoder


# The xterm alternate screen separates the TUI from regular shell scrollback.
# Re-enter after curses.endwin() or a fullscreen child switches buffers.
_ALT_SCREEN_ON = b"\x1b[?1049h"


def enter_app_screen() -> None:
    """Restore the private terminal screen after returning from child TUI."""
    if os.isatty(sys.stdout.fileno()):
        os.write(sys.stdout.fileno(), _ALT_SCREEN_ON)


def configure_colors(background: int) -> None:
    """Blue shell prompt and slightly richer green headings."""
    prompt_blue = 75 if curses.COLORS >= 256 else curses.COLOR_BLUE
    heading_green = 114 if curses.COLORS >= 256 else curses.COLOR_GREEN
    curses.init_pair(1, prompt_blue, background)
    curses.init_pair(2, curses.COLOR_GREEN, background)  # Bright checkmark.
    curses.init_pair(3, curses.COLOR_RED, background)
    curses.init_pair(4, curses.COLOR_WHITE, background)
    curses.init_pair(5, heading_green, background)


def enable_mouse_wheel() -> None:
    """Request wheel events without capturing clicks or dragging when supported.

    macOS Terminal may instead navigate its own scrollback; its profile's
    `Scroll alternate screen` setting controls that native behavior.
    """
    try:
        curses.mouseinterval(0)
        wheel = getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0)
        if wheel:
            curses.mousemask(wheel)
    except (curses.error, AttributeError):
        pass


def configure_mouse(enabled: bool) -> bool:
    """Use native curses wheel on modern builds, SGR reports on older macOS.

    Older Apple curses lacks BUTTON5_PRESSED, so the normal native mask
    cannot request scrolling down. SGR encodes wheel up/down as 64/65.
    Returns True only when the raw SGR decoder should be used.
    """
    if getattr(curses, "BUTTON5_PRESSED", None) is not None:
        if enabled:
            enable_mouse_wheel()
        else:
            try:
                curses.mousemask(0)
            except curses.error:
                pass
        return False
    try:
        curses.mousemask(0)  # Avoid two competing mouse decoders.
    except curses.error:
        pass
    if not os.isatty(sys.stdout.fileno()):
        return False
    os.write(sys.stdout.fileno(),
             b"\x1b[?1006h\x1b[?1000h" if enabled else b"\x1b[?1000l\x1b[?1006l")
    return enabled


def set_bracketed_paste(enabled: bool) -> None:
    """Tell the terminal to mark pasted text instead of sending bare newlines."""
    if os.isatty(sys.stdout.fileno()):
        os.write(sys.stdout.fileno(), b"\x1b[?2004h" if enabled else b"\x1b[?2004l")


def smooth_scroll_step(current: int, target: int, last_at: float,
                       now: float) -> tuple[int, float]:
    """Advance at most two text rows every 20 ms; no pixel scrolling."""
    delta = target - current
    if delta == 0 or now - last_at < 0.02:
        return current, last_at
    step = min(abs(delta), 1 if abs(delta) < 6 else 2)
    return current + (step if delta > 0 else -step), now


SLASH_COMMANDS = frozenset({"help", "quit", "exit", "session", "models", "model",
                            "server", "ai", "explain", "language", "expand",
                            "clear", "raw", "scroll-debug"})


def is_meta_command(command: str) -> bool:
    """Keep absolute executables like /bin/echo working normally."""
    parts = command.split(maxsplit=1)
    if not parts:
        return False
    name = parts[0]
    return name.startswith(":") or (name.startswith("/") and name[1:] in SLASH_COMMANDS)


# Full-screen programs must control the terminal directly.  Treat only a
# standalone invocation as interactive: pipelines and compound commands keep
# their normal shell behavior, and `codex exec` remains non-interactive.
def needs_fullscreen(command: str) -> bool:
    if any(ch in command for ch in (";", "|", "<", ">", "`", "\n", "\r")) or "$(" in command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    executable = Path(parts[0]).name
    if executable == "codex":
        if len(parts) == 1 or parts[1] in {"resume", "fork"}:
            return True
        # Flags such as `codex -m model` start the interactive CLI.  Keep
        # `codex exec` and plain help/version commands in the block runner.
        return parts[1].startswith("-") and parts[1] not in {"-h", "--help", "-V", "--version"}
    return executable in {"vim", "nvim", "nano", "htop", "btop"}

def fullscreen_launch_command(command: str) -> str:
    """Use Codex's familiar inline layout inside a separately isolated screen.

    Do not rewrite shell expressions or explicit user choices; unlike an edit to
    ~/.codex/config.toml this only affects one interactive launch.
    """
    if not needs_fullscreen(command):
        return command
    if any(x in command for x in ("$", "`", "\\", "*", "?", "\n", "\r")):
        return command
    try:
        args = shlex.split(command)
    except ValueError:
        return command
    if Path(args[0]).name != "codex":
        return command
    if "--no-alt-screen" in args or any("tui.alternate_screen" in arg for arg in args):
        return command
    return shlex.join([args[0], "--no-alt-screen", *args[1:]])


def codex_inline_screen(command: str) -> bool:
    """Only interactive Codex in inline mode needs a private outer screen."""
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return bool(args and Path(args[0]).name == "codex" and
                "--no-alt-screen" in args and needs_fullscreen(command))


def child_screen(enter: bool) -> None:
    """Isolate inline Codex without clearing the user's normal scrollback."""
    if os.isatty(sys.stdout.fileno()):
        os.write(sys.stdout.fileno(),
                 b"\x1b[?1049h\x1b[2J\x1b[H" if enter else b"\x1b[?1049l")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.worker = AiWorker(cfg)
        self.cwd = Path.cwd()
        # The shell-like prompt is derived from the actual host and directory.
        self.hostname = socket.gethostname().split(".")[0] or "mac"
        self.blocks: deque[Block] = deque(maxlen=cfg.max_blocks)
        self.next_id = 1
        self.line = ""
        self.pending: deque[str] = deque()
        self.paste_decoder = PasteDecoder()
        self.paste_review: deque[str] = deque()
        self.paste_feedback = ""
        self.direct_input = False  # Ctrl+G enables stdin forwarding for interactive commands.
        self.history: list[str] = []
        self.hist_pos = 0
        self.scroll = 0
        self.scroll_target: int | None = None
        self.scroll_direction = ""
        self.scroll_last_step = 0.0
        # Terminal.app may report alternate-screen wheel gestures as arrows.
        self.scroll_wheel_events = 0
        self.scroll_arrow_events = 0
        self.scroll_debug = False
        self.ai_on = True
        self.ai_state = "local / idle"
        self.session: PersistentShell | None = None
        self.last_rows, self.last_cols = 24, 80
        self.process: subprocess.Popen[bytes] | None = None
        self.master: int | None = None
        self.active: Block | None = None
        self.last_redraw = 0.0
        self.last_inference = 0.0
        self.should_exit = False

    def add_block(self, command: str, title: str, message: str, status: str = "ok") -> Block:
        b = Block(self.next_id, command, str(self.cwd), ScreenText(self.cfg.max_output_lines))
        self.next_id += 1
        b.title, b.explanation, b.status = title, message, status
        self.blocks.append(b)
        self.scroll = 0
        self.scroll_target = None
        return b

    def start(self, command: str, rows: int, cols: int) -> None:
        b = Block(self.next_id, command, str(self.cwd), ScreenText(self.cfg.max_output_lines))
        self.next_id += 1
        b.title, b.explanation = explain_rules(command, "", None)
        self.blocks.append(b)
        self.active = b
        self.scroll = 0
        self.scroll_target = None
        if self.session is not None:
            try:
                self.session.send(command)
            except (OSError, ValueError) as exc:
                b.finish(127)
                b.explanation = f"Ошибка постоянной сессии: {exc}"
                self.active = None
            return
        master, slave = pty.openpty()
        winsz = struct.pack("HHHH", max(3, rows - 3), max(20, cols), 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsz)

        def prepare_child() -> None:
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.process = subprocess.Popen(
                [self.cfg.shell, "-lc", command], stdin=slave, stdout=slave, stderr=slave,
                cwd=self.cwd, preexec_fn=prepare_child, close_fds=True,
                env={**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"},
            )
            os.set_blocking(master, False)
            self.master = master
        except Exception as exc:
            os.close(master)
            b.finish(127)
            b.explanation = f"Не удалось запустить программу: {type(exc).__name__}."
            self.process, self.active = None, None
        finally:
            os.close(slave)

    def _finish(self, exit_code: int) -> None:
        if not self.active:
            return
        block = self.active
        if self.session is not None and block.output.lines:
            # Interactive shells echo our command. Keep the source below the
            # explanation without duplicating the input line.
            first = block.output.lines[0].strip()
            if first == block.command.strip():
                block.output.lines.popleft()
        block.finish(exit_code)
        block.analysis_epoch += 1  # Cancel pending incomplete explanations.
        self._queue_ai(block)
        self.active = None
        self.direct_input = False

    def poll(self) -> bool:
        changed = False
        if self.session is not None:
            for event, value in self.session.poll():
                if event == "output":
                    if self.active:
                        self.active.output.feed(value)
                        changed = True
                elif event == "end":
                    exit_code, cwd = value
                    if cwd and Path(cwd).is_dir():
                        self.cwd = Path(cwd)
                    self._finish(exit_code)
                    changed = True
                elif event == "session_exited":
                    self._finish(value if isinstance(value, int) else 127)
                    self.session.stop()
                    self.session = None
                    self.add_block("", "Сессия завершилась", "Для повторного запуска введи /session on.", "error")
                    changed = True
        if self.master is not None and self.active:
            for _ in range(12):
                try:
                    data = os.read(self.master, 8192)
                except BlockingIOError:
                    break
                except OSError:
                    data = b""
                if not data:
                    break
                self.active.output.feed(data)
                changed = True
            if self.process and self.process.poll() is not None:
                # PTY may still contain output; defer completion until drained.
                while True:
                    try:
                        data = os.read(self.master, 8192)
                        if not data:
                            break
                        self.active.output.feed(data)
                        changed = True
                    except (BlockingIOError, OSError):
                        break
                self._finish(self.process.returncode)
                os.close(self.master)
                self.master, self.process = None, None
                changed = True
            # Display running output immediately, but defer model analysis until
            # a real exit code exists. A pager or active process is not an error.
        for event in self.worker.drain():
            block_id, epoch, phase, text = event
            b = next((item for item in self.blocks if item.id == block_id), None)
            if not b or b.analysis_epoch != epoch:
                continue
            manual = b.command in ("/explain", ":explain")
            if b.status == "running" and not manual:
                continue
            if phase in ("partial", "done") and text.strip():
                b.explanation = text.strip().replace("\n", " ")[:220]
                b.ai_state = "ready" if phase == "done" else "streaming"
                if manual:
                    b.title = "Объяснение ИИ" if phase == "done" else "ИИ отвечает"
                    b.status = "ok" if phase == "done" else "running"
                self.ai_state = "streaming" if phase == "partial" else "ready"
            elif phase == "unavailable":
                b.ai_state = "unavailable"
                if manual:
                    b.status = "error"
                    b.title = "ИИ не ответил"
                    b.explanation = text or "Не удалось получить ответ модели."
                self.ai_state = "model unavailable / rules active"
            elif phase == "timing":
                try:
                    b.ai_seconds = max(0, float(text))
                except (ValueError, OverflowError):
                    pass
            changed = True
        if self._run_pending():
            changed = True
        return changed

    def _queue_ai(self, b: Block, *, force: bool = False) -> None:
        if not self.ai_on or b.exit_code is None:
            return
        # Deterministic explanations are faster for common read-only commands;
        # /explain still allows an explicit model request.
        if not force and b.exit_code in (None, 0) and simple_read_command(b.command):
            self.ai_state = "ready / rules"
            return
        if self.worker.submit(b):
            self._track_model(b)
            b.analysed = True
            self.ai_state = "thinking"

    def _track_model(self, block: Block) -> None:
        profile = self.cfg.profile()
        block.ai_profile = profile.name
        block.ai_model = profile.model
        block.ai_seconds = None
        block.ai_state = "thinking"

    def prompt(self) -> str:
        """Real hostname and directory, like a standard zsh prompt."""
        try:
            relative = self.cwd.relative_to(Path.home())
            directory = "~" if not relative.parts else "~/" + relative.as_posix()
        except ValueError:
            directory = str(self.cwd)
        return f"{self.hostname} {directory} % "

    def resize(self, rows: int, cols: int) -> None:
        self.last_rows, self.last_cols = rows, cols
        if self.session is not None:
            self.session.resize(rows - 3, cols)
        if self.master is not None:
            try:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", max(3, rows - 3), max(20, cols), 0, 0))
                if self.process:
                    os.killpg(self.process.pid, signal.SIGWINCH)
            except (OSError, ProcessLookupError):
                pass

    def _send(self, data: bytes) -> None:
        if self.session is not None:
            try:
                self.session.send_raw(data)
            except OSError:
                pass
            return
        if self.master is not None:
            try:
                os.write(self.master, data)
            except OSError:
                pass

    def _run_pending(self) -> bool:
        """Run queued lines in order, never writing pasted commands to a busy PTY."""
        changed = False
        while not self.active and self.pending and not self.should_exit:
            command = self.pending.popleft()
            changed = True
            if is_meta_command(command):
                self.meta(command)
                # /raw needs to run in the main loop with curses suspended.
                if getattr(self, "raw_command", ""):
                    break
            elif needs_fullscreen(command):
                # Codex/Vim cannot render through the line-based block parser.
                # Launch the existing transparent PTY path instead.
                self.raw_command = command
                break
            elif self.session is None and (command == "cd" or command.startswith("cd ")):
                try:
                    args = shlex.split(command)
                    dest = Path(os.path.expandvars(os.path.expanduser(args[1] if len(args) > 1 else "~")))
                    if not dest.is_absolute():
                        dest = self.cwd / dest
                    dest = dest.resolve(strict=True)
                    if not dest.is_dir():
                        raise NotADirectoryError(dest)
                    self.cwd = dest
                    self.add_block(command, "Папка изменена", str(dest))
                except (ValueError, OSError, IndexError) as exc:
                    self.add_block(command, "Не удалось перейти в папку", str(exc), "error")
            else:
                self.start(command, self.last_rows, self.last_cols)
        return changed

    def _stage_paste(self, text: str) -> None:
        """Stage clipboard commands for manual Enter approval one by one."""
        if self.active and self.direct_input:
            # The user explicitly enabled stdin passthrough with Ctrl+G.
            self._send(text.encode("utf-8", "replace"))
            return
        if self.paste_review:
            self.paste_feedback = "Сначала подтверди или отмени предыдущую вставку (Ctrl+C)."
            return
        try:
            commands = prepare_pasted_commands(text, self.line)
        except ValueError as exc:
            self.paste_feedback = str(exc)
            return
        if not commands:
            return
        self.line = commands[0]
        self.scroll = 0
        self.scroll_target = None
        self.paste_review.extend(commands[1:])
        self.paste_feedback = f"Вставлено {len(commands)} команд: Enter — выполнить одну; Ctrl+C — отменить."

    def _queue_scroll(self, direction: str) -> None:
        """Change the desired position, not the currently visible row."""
        if direction not in ("up", "down"):
            return
        # Reverse immediately instead of draining a stale gesture backlog.
        base = (self.scroll if self.scroll_target is None or
                direction != self.scroll_direction else self.scroll_target)
        self.scroll_target = max(0, base + (1 if direction == "up" else -1))
        self.scroll_direction = direction

    def scroll_wheel(self, direction: str, rows: int) -> None:
        if direction in ("up", "down"):
            self._queue_scroll(direction)
            self.scroll_wheel_events += 1

    def advance_scroll(self, max_scroll: int, now: float | None = None) -> None:
        self.scroll = min(max_scroll, max(0, self.scroll))
        if self.scroll_target is None:
            return
        self.scroll_target = min(max_scroll, max(0, self.scroll_target))
        self.scroll, self.scroll_last_step = smooth_scroll_step(
            self.scroll, self.scroll_target, self.scroll_last_step,
            time.monotonic() if now is None else now,
        )
        if self.scroll == self.scroll_target:
            self.scroll_target = None

    @property
    def is_scrolling(self) -> bool:
        return self.scroll_target is not None

    def key(self, key: str | int, rows: int, cols: int) -> None:
        # Bracketed paste is not a sequence of Enter keypresses. Never execute
        # text received between ESC[200~ and ESC[201~.
        for event, value in self.paste_decoder.feed(key):
            if event == "paste":
                self._stage_paste(value)
            elif event == "error":
                self.paste_feedback = str(value)
            else:
                self._key(value, rows, cols)

    def _key(self, key: str | int, rows: int, cols: int) -> None:
        self.last_rows, self.last_cols = rows, cols
        if key == curses.KEY_MOUSE:
            try:
                _, _, _, _, buttons = curses.getmouse()
                if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
                    self.scroll_wheel("up", rows)
                elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                    self.scroll_wheel("down", rows)
            except curses.error:
                pass
            return
        if key == curses.KEY_RESIZE:
            self.resize(rows, cols)
            return
        if key == curses.KEY_PPAGE:
            self.scroll_target = None
            self.scroll += max(1, rows // 2)
            return
        if key == curses.KEY_NPAGE:
            self.scroll_target = None
            self.scroll = max(0, self.scroll - max(1, rows // 2))
            return
        if key == "\x14":  # Ctrl+T
            if self.blocks:
                self.blocks[-1].expanded = not self.blocks[-1].expanded
            return
        if key == "\x0c":  # Ctrl+L
            if self.active is None:
                self.blocks.clear()
                self.scroll = 0
                self.scroll_target = None
            return
        if key == "\x03":  # Ctrl+C: stop current process and cancel pending commands.
            self.pending.clear()
            self.paste_review.clear()
            self.paste_decoder.reset()
            self.paste_feedback = ""
            self.line = ""
            if self.active:
                if self.session is not None:
                    try:
                        self.session.interrupt()
                    except OSError:
                        pass
                elif self.process:
                    try:
                        os.killpg(self.process.pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
            return
        if self.active and key == "\x07":  # Ctrl+G: toggle interactive stdin.
            self.direct_input = not self.direct_input
            return
        if self.active and self.direct_input:
            if isinstance(key, str):
                self._send(b"\x7f" if key in ("\x08", "\x7f") else key.encode("utf-8", "replace"))
            else:
                special = {curses.KEY_UP: b"\x1b[A", curses.KEY_DOWN: b"\x1b[B", curses.KEY_LEFT: b"\x1b[D", curses.KEY_RIGHT: b"\x1b[C", curses.KEY_BACKSPACE: b"\x7f", curses.KEY_ENTER: b"\n"}
                if key in special:
                    self._send(special[key])
            return
        if key in ("\n", "\r", curses.KEY_ENTER):
            command = self.line.strip()
            if not command:
                return
            if len(self.pending) >= 32:
                self.paste_feedback = "Очередь заполнена. Дождись завершения команд."
                return
            # Only a real Enter here approves this command. Pasted newlines
            # never reach this branch, even with an active shell process.
            self.line = self.paste_review.popleft() if self.paste_review else ""
            self.paste_feedback = (
                f"Осталось {len(self.paste_review) + 1} команд: Enter — следующая; Ctrl+C — отменить."
                if self.line else ""
            )
            self.history.append(command)
            self.hist_pos = len(self.history)
            self.pending.append(command)
            self._run_pending()
        elif key in ("\x08", "\x7f", curses.KEY_BACKSPACE):
            self.line = self.line[:-1]
            self.scroll = 0
            self.scroll_target = None
        elif key in (curses.KEY_UP, curses.KEY_DOWN) and not self.line and self.blocks:
            # Apple Terminal's "Scroll alternate screen" can turn the trackpad
            # into repeated arrow keys rather than KEY_MOUSE. While the prompt
            # is empty, arrows navigate the output; Ctrl+P/N access history.
            self._queue_scroll("up" if key == curses.KEY_UP else "down")
            self.scroll_arrow_events += 1
        elif key in (curses.KEY_UP, "\x10") and self.history:
            self.hist_pos = max(0, self.hist_pos - 1)
            self.line = self.history[self.hist_pos]
            self.scroll = 0
            self.scroll_target = None
        elif key in (curses.KEY_DOWN, "\x0e") and self.history:
            self.hist_pos = min(len(self.history), self.hist_pos + 1)
            self.line = self.history[self.hist_pos] if self.hist_pos < len(self.history) else ""
            self.scroll = 0
            self.scroll_target = None
        elif key == "\x04" and not self.line and self.active is None:
            self.should_exit = True
        elif isinstance(key, str) and key.isprintable() and len(self.line) < 4096:
            self.line += key
            self.scroll = 0
            self.scroll_target = None

    def meta(self, command: str) -> None:
        parts = command.split()
        name = "/" + parts[0][1:]
        if name in ("/quit", "/exit"):
            self.should_exit = True
        elif name == "/help":
            self.add_block(command, "Команды приложения", "/session on/off · /models · /model NAME · /server NAME · /language ru/uk/en · /explain · /ai on/off · /expand · /scroll-debug on/off · /clear · /exit · /raw COMMAND")
        elif name == "/session" and len(parts) == 2 and parts[1] in ("on", "off"):
            if parts[1] == "on" and self.session is None:
                try:
                    self.session = PersistentShell(self.cfg.shell, self.cwd, self.last_rows - 3, self.last_cols)
                    self.add_block(command, "Постоянная сессия включена", "Переменные, функции, алиасы и папка сохраняются между командами. Однострочный ввод. Экспериментальный режим.")
                except (OSError, ValueError) as exc:
                    self.add_block(command, "Не удалось открыть сессию", str(exc), "error")
            elif parts[1] == "off" and self.session is not None:
                self.session.stop()
                self.session = None
                self.add_block(command, "Постоянная сессия выключена", "Обычные команды снова выполняются отдельно.")
            else:
                self.add_block(command, "Режим сессии", "Уже включён" if self.session else "Уже выключен")
        elif name == "/scroll-debug" and len(parts) == 2 and parts[1] in ("on", "off"):
            self.scroll_debug = parts[1] == "on"
            self.add_block(command, "Диагностика прокрутки",
                           "Включена: счётчики событий видны под строкой ввода."
                           if self.scroll_debug else "Выключена.")
        elif name == "/models":
            names = ", ".join(f"{n} ({p.provider}: {p.model})" for n, p in self.cfg.profiles.items())
            self.add_block(command, "Доступные модели", names)
        elif name in ("/model", "/server") and len(parts) == 2:
            if parts[1] in self.cfg.profiles:
                self.cfg.active = parts[1]
                self.add_block(command, "Модель переключена", f"Профиль: {parts[1]}")
            else:
                self.add_block(command, "Неизвестная модель", "Используй /models", "error")
        elif name == "/ai" and len(parts) == 2 and parts[1] in ("on", "off"):
            self.ai_on = parts[1] == "on"
            self.add_block(command, "Режим анализа", "ИИ включён" if self.ai_on else "ИИ отключён. Работают только правила.")
        elif name == "/explain":
            source = next((b for b in reversed(self.blocks)
                           if b.exit_code is not None and b.command
                           and not is_meta_command(b.command)), None)
            if not self.ai_on:
                self.add_block(command, "ИИ выключен", "Включи его командой /ai on.", "error")
            elif source is None:
                self.add_block(command, "Нет выполненной команды", "Сначала выполни команду терминала.", "error")
            else:
                # A separate visible block prevents an invisible update of a
                # historical item and keeps the original output untouched.
                request = self.add_block("/explain", "ИИ анализирует",
                                         f"Проверяю команду: {source.command[:120]}", "running")
                if self.worker.submit(request, source=source):
                    self._track_model(request)
                    self.ai_state = "thinking"
                else:
                    request.title = "ИИ занят"
                    request.explanation = "Очередь модели заполнена. Попробуй /explain ещё раз."
                    request.status = "error"
        elif name == "/language" and len(parts) == 2:
            if parts[1] in ("ru", "uk", "en"):
                self.cfg.language = parts[1]
                self.add_block(command, "Язык объяснений изменён", parts[1])
            else:
                self.add_block(command, "Неизвестный язык", "Доступно: ru, uk, en", "error")
        elif name == "/expand":
            if self.blocks:
                self.blocks[-1].expanded = not self.blocks[-1].expanded
        elif name == "/clear":
            self.blocks.clear()
            self.scroll = 0
            self.scroll_target = None
        elif name == "/raw":
            self.raw_command = command[len(parts[0]):].strip()
            if not self.raw_command:
                self.add_block(command, "Нет команды", "Напиши /raw codex или /raw vim", "error")
        else:
            self.add_block(command, "Неизвестная команда", "Используй /help", "error")

    def render(self, stdscr: curses.window, force_redraw: bool = False) -> None:
        h, w = stdscr.getmaxyx()
        if h < 7 or w < 25:
            stdscr.erase()
            self._line(stdscr, 0, 0, "Увеличь окно терминала", 0)
            if force_redraw:
                stdscr.redrawwin()
            stdscr.refresh()
            return
        stdscr.erase()
        # Keep the terminal empty on startup. Blocks appear only after commands.
        viewport: list[tuple[str, int, bool]] = []
        for index, block in enumerate(self.blocks):
            heading = curses.color_pair(3 if block.status == "error" else 5) | curses.A_BOLD
            symbol = "●" if block.status == "running" else "✓" if block.status == "ok" else "!"
            viewport.append((f"{symbol} {block.title}", heading, block.status == "ok"))
            viewport.extend(("  " + line, 0, False) for line in self._wrap(block.explanation, w - 3))
            if block.ai_model:
                detail = f"  ИИ: {block.ai_model} ({block.ai_profile})"
                if block.ai_seconds is not None:
                    detail += f" · ответ за {block.ai_seconds:.2f} с"
                elif block.ai_state in ("thinking", "streaming"):
                    detail += " · анализ..."
                if block.ai_state == "unavailable":
                    detail += " · недоступен"
                viewport.extend((line, curses.color_pair(4), False)
                                for line in self._wrap(detail, w - 3))
            viewport.extend(("  └ " + line, curses.color_pair(4), False)
                            for line in self._wrap(block.command, w - 6))
            output = block.output.snapshot()
            if block.expanded:
                selected = output
            elif block.status == "running":
                selected = output[-5:]
            else:
                selected = output[:3]
            for line in selected:
                viewport.extend(("    " + item, curses.color_pair(4), False)
                                for item in self._wrap(line, w - 6))
            if not block.expanded and len(output) > len(selected):
                viewport.append((f"    +{len(output) - len(selected)} строк (Ctrl+T — развернуть)",
                                 curses.color_pair(4), False))
            # Space between command blocks, but not between the latest
            # command's output and the live shell prompt.
            if index < len(self.blocks) - 1:
                viewport.append(("", 0, False))

        # Put the prompt in the SAME scrollable timeline as command output.
        # When viewing earlier output, the prompt must scroll out of view:
        # it cannot appear after line 55 of a 150-line result.
        review = list(self.paste_review)
        preview_slots = max(0, h - 4)
        visible_count = min(len(review), preview_slots)
        if len(review) > preview_slots:
            visible_count = max(0, preview_slots - 1)
        remaining = len(review) - visible_count
        footer = bool(viewport or self.paste_feedback or self.scroll_debug)

        prompt = self.prompt()
        if self.active:
            mode_hint = "stdin" if self.direct_input else f"очередь {len(self.pending)}"
            prompt += f"[{mode_hint}] "
        if not self.direct_input:
            prompt += self.line
        prompt_index = len(viewport)
        viewport.append((prompt, curses.color_pair(1), False))
        for idx, command in enumerate(review[:visible_count], start=2):
            viewport.append((f"  {idx}. {command}", curses.color_pair(4), False))
        if remaining:
            viewport.append((f"  … ещё {remaining} команд", curses.color_pair(4), False))
        if footer:
            viewport.append(("", curses.color_pair(4), False))

        max_scroll = max(0, len(viewport) - h)
        self.advance_scroll(max_scroll)
        if footer:
            mode = "session" if self.session is not None else "isolated"
            status = (self.paste_feedback or
                      f" {self.cfg.active} · {mode} · {self.ai_state if self.ai_on else 'AI off'}"
                      "   Ctrl+G: stdin  Ctrl+C: отмена  Ctrl+T: свернуть/развернуть  /help")
            if self.scroll_debug and not self.paste_feedback:
                status = (f" скролл: колесо={self.scroll_wheel_events} "
                          f"стрелки={self.scroll_arrow_events} "
                          f"позиция={self.scroll}/{max_scroll} · Ctrl+P/N: история")
            viewport[-1] = (status, curses.color_pair(4), False)

        start = max_scroll - self.scroll
        for y, (line, attr, success) in enumerate(viewport[start:start + h]):
            if success and line.startswith("✓ "):
                self._line(stdscr, y, 0, "✓", curses.color_pair(2) | curses.A_BOLD)
                self._line(stdscr, y, 1, line[1:], attr)
            else:
                self._line(stdscr, y, 0, line, attr)

        # Only display the editing cursor when its prompt is in view.
        prompt_y = prompt_index - start
        prompt_visible = 0 <= prompt_y < h
        if not self.direct_input:
            try:
                curses.curs_set(1 if prompt_visible else 0)
            except curses.error:
                pass
            if prompt_visible:
                try:
                    stdscr.move(prompt_y, min(w - 2, len(prompt)))
                except curses.error:
                    pass
        # Cmd+K is handled by Terminal.app, not sent as input. A forced
        # refresh makes curses repaint after the host clears its screen.
        if force_redraw:
            stdscr.redrawwin()
        stdscr.refresh()

    @staticmethod
    def _line(stdscr: curses.window, y: int, x: int, text: str, attr: int) -> None:
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addnstr(y, x, text.replace("\x00", ""), max(0, w - x - 1), attr)
            except curses.error:
                pass

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        # AI/server output must not contain terminal control characters.
        cleaned = "".join(ch if ch in "\n\t" or ch.isprintable() else " " for ch in text)
        lines = cleaned.splitlines() or [""]
        result = []
        for line in lines:
            result.extend(textwrap.wrap(line, width=max(10, width), break_long_words=True, replace_whitespace=True) or [""])
        return result[:35]

    def stop(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session = None
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
        if self.master is not None:
            os.close(self.master)
            self.master = None


def run_ui(config: Config) -> None:
    app = App(config)
    if not Path(config.shell).exists():
        raise ValueError(f"Shell not found: {config.shell}; edit {config.shell!r} in config.toml")

    def main(stdscr: curses.window) -> None:
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        configure_colors(bg)
        # curses.wrapper has already entered the alternate screen. Do not
        # manually re-enter it; xterm screen switching is not nestable.
        raw_sgr = configure_mouse(True)
        wheel_decoder = SGRWheelDecoder()
        set_bracketed_paste(True)
        curses.curs_set(1)
        stdscr.keypad(True)
        stdscr.timeout(20)
        needs_redraw = True
        last_render_at = 0.0
        last_resync_at = time.monotonic()
        try:
            while not app.should_exit:
                h, w = stdscr.getmaxyx()
                try:
                    key = stdscr.get_wch()
                    needs_redraw = True
                    if raw_sgr:
                        for kind, value in wheel_decoder.feed(key):
                            if kind == "wheel":
                                app.scroll_wheel(str(value), h)
                            else:
                                app.key(value, h, w)
                    else:
                        app.key(key, h, w)
                except curses.error:
                    pass
                needs_redraw = app.poll() or needs_redraw
                if getattr(app, "raw_command", ""):
                    command = app.raw_command
                    app.raw_command = ""
                    if command:
                        from .raw import run_raw_command
                        b = app.add_block(command, "Интерактивная программа", "Открыта в полном терминале.", "running")
                        # Release mouse reporting before Codex/Vim takes over.
                        configure_mouse(False)
                        wheel_decoder.reset()
                        set_bracketed_paste(False)
                        curses.endwin()  # Release curses' screen before the child starts.
                        child_command = fullscreen_launch_command(command)
                        inline_screen = codex_inline_screen(child_command)
                        try:
                            if inline_screen:
                                child_screen(True)
                            code, _ = run_raw_command(config.shell, child_command, app.cwd, config.max_output_lines)
                            # A full-screen TUI has redraws, not a meaningful text
                            # transcript. Never paste those frames into a block
                            # or send them to a model as command output.
                            b.exit_code = code
                            b.status = "ok" if code == 0 else "error"
                            b.title = "Интерактивная программа закрыта" if code == 0 else "Программа завершилась с ошибкой"
                            b.explanation = f"{command.split()[0]} завершён (код {code})."
                        except (OSError, ValueError) as exc:
                            b.exit_code = 127
                            b.status = "error"
                            b.title, b.explanation = "Не удалось открыть программу", str(exc)
                        finally:
                            if inline_screen:
                                child_screen(False)
                            # Restore curses only after Codex leaves its own screen.
                            curses.reset_prog_mode()
                            raw_sgr = configure_mouse(True)
                            wheel_decoder.reset()
                            set_bracketed_paste(True)
                            stdscr.clear()
                            stdscr.refresh()
                        app._run_pending()
                        needs_redraw = True
                # Batch fragmented SGR packets; repaint at most 50 fps.
                now = time.monotonic()
                # Terminal.app's Cmd+K clears its display without notifying
                # curses. Periodically invalidate its cached screen so the UI
                # recovers without a resize or another keypress.
                # Avoid a full repaint during active wheel animation.
                resync = not app.is_scrolling and now - last_resync_at >= 1.0
                if (needs_redraw or app.is_scrolling or resync) and now - last_render_at >= 0.02:
                    app.render(stdscr, force_redraw=resync)
                    last_render_at = now
                    if resync:
                        last_resync_at = now
                    needs_redraw = False
        finally:
            configure_mouse(False)
            set_bracketed_paste(False)
            app.stop()

    curses.wrapper(main)
