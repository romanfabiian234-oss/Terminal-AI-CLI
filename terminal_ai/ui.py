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


# The xterm alternate screen separates the TUI from regular shell scrollback.
# Re-enter after curses.endwin() or a fullscreen child switches buffers.
_ALT_SCREEN_ON = b"\\x1b[?1049h"


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


SLASH_COMMANDS = frozenset({"help", "quit", "exit", "session", "models", "model",
                            "server", "ai", "explain", "language", "expand", "clear", "raw"})


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
        self.direct_input = False  # Ctrl+G enables stdin forwarding for interactive commands.
        self.history: list[str] = []
        self.hist_pos = 0
        self.scroll = 0
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
        return b

    def start(self, command: str, rows: int, cols: int) -> None:
        b = Block(self.next_id, command, str(self.cwd), ScreenText(self.cfg.max_output_lines))
        self.next_id += 1
        b.title, b.explanation = explain_rules(command, "", None)
        self.blocks.append(b)
        self.active = b
        self.scroll = 0
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

    def key(self, key: str | int, rows: int, cols: int) -> None:
        self.last_rows, self.last_cols = rows, cols
        if key == curses.KEY_RESIZE:
            self.resize(rows, cols)
            return
        if key == curses.KEY_PPAGE:
            self.scroll += max(1, rows // 2)
            return
        if key == curses.KEY_NPAGE:
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
            return
        if key == "\x03":  # Ctrl+C: stop current process and cancel pending commands.
            self.pending.clear()
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
            self.line = ""
            if not command:
                return
            if len(self.pending) >= 32:
                self.add_block(command, "Очередь заполнена", "Допускается до 32 команд в ожидании.", "error")
                return
            self.history.append(command)
            self.hist_pos = len(self.history)
            self.pending.append(command)
            self._run_pending()
        elif key in ("\x08", "\x7f", curses.KEY_BACKSPACE):
            self.line = self.line[:-1]
        elif key == curses.KEY_UP and self.history:
            self.hist_pos = max(0, self.hist_pos - 1)
            self.line = self.history[self.hist_pos]
        elif key == curses.KEY_DOWN and self.history:
            self.hist_pos = min(len(self.history), self.hist_pos + 1)
            self.line = self.history[self.hist_pos] if self.hist_pos < len(self.history) else ""
        elif key == "\x04" and not self.line and self.active is None:
            self.should_exit = True
        elif isinstance(key, str) and key.isprintable() and len(self.line) < 4096:
            self.line += key

    def meta(self, command: str) -> None:
        parts = command.split()
        name = "/" + parts[0][1:]
        if name in ("/quit", "/exit"):
            self.should_exit = True
        elif name == "/help":
            self.add_block(command, "Команды приложения", "/session on/off · /models · /model NAME · /server NAME · /language ru/uk/en · /explain · /ai on/off · /expand · /clear · /exit · /raw COMMAND")
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
        elif name == "/raw":
            self.raw_command = command[len(parts[0]):].strip()
            if not self.raw_command:
                self.add_block(command, "Нет команды", "Напиши /raw codex или /raw vim", "error")
        else:
            self.add_block(command, "Неизвестная команда", "Используй /help", "error")

    def render(self, stdscr: curses.window) -> None:
        h, w = stdscr.getmaxyx()
        if h < 7 or w < 25:
            stdscr.erase()
            self._line(stdscr, 0, 0, "Увеличь окно терминала", 0)
            stdscr.refresh()
            return
        stdscr.erase()
        # Keep the terminal empty on startup. Blocks appear only after commands.
        viewport: list[tuple[str, int, bool]] = []
        for block in self.blocks:
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
            viewport.append(("", 0, False))

        # Like a shell: prompt follows the last block, moving to the bottom only
        # when content needs more vertical space. No permanent header or separator.
        prompt_y = min(h - 2, len(viewport)) if viewport else 0
        start = max(0, len(viewport) - prompt_y - self.scroll)
        for y, (line, attr, success) in enumerate(viewport[start:start + prompt_y]):
            if success and line.startswith("✓ "):
                self._line(stdscr, y, 0, "✓", curses.color_pair(2) | curses.A_BOLD)
                self._line(stdscr, y, 1, line[1:], attr)
            else:
                self._line(stdscr, y, 0, line, attr)

        prompt = self.prompt()
        if self.active:
            mode_hint = "stdin" if self.direct_input else f"очередь {len(self.pending)}"
            prompt += f"[{mode_hint}] "
        if not self.direct_input:
            prompt += self.line
        self._line(stdscr, prompt_y, 0, prompt, curses.color_pair(1))
        if viewport and prompt_y + 1 < h:
            mode = "session" if self.session is not None else "isolated"
            status = (f" {self.cfg.active} · {mode} · {self.ai_state if self.ai_on else 'AI off'}"
                      "   Ctrl+G: stdin  Ctrl+C: отмена  Ctrl+T: детали  /help")
            self._line(stdscr, prompt_y + 1, 0, status, curses.color_pair(4))
        if not self.direct_input:
            cursor_x = min(w - 2, len(prompt))
            try:
                stdscr.move(prompt_y, cursor_x)
            except curses.error:
                pass
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
        enter_app_screen()  # Do not expose original shell scrollback inside TUI.
        curses.curs_set(1)
        stdscr.keypad(True)
        stdscr.timeout(65)
        try:
            while not app.should_exit:
                h, w = stdscr.getmaxyx()
                try:
                    key = stdscr.get_wch()
                    app.key(key, h, w)
                except curses.error:
                    pass
                app.poll()
                if getattr(app, "raw_command", ""):
                    command = app.raw_command
                    app.raw_command = ""
                    if command:
                        from .raw import run_raw_command
                        b = app.add_block(command, "Интерактивная программа", "Открыта в полном терминале.", "running")
                        curses.endwin()
                        # endwin() can return to the original shell buffer.
                        enter_app_screen()
                        try:
                            code, _ = run_raw_command(config.shell, command, app.cwd, config.max_output_lines)
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
                            # Interactive programs may exit the alternate buffer.
                            # Restore the private screen before curses redraws.
                            enter_app_screen()
                            curses.reset_prog_mode()
                            stdscr.clear()
                            stdscr.refresh()
                        app._run_pending()
                app.render(stdscr)
        finally:
            app.stop()

    curses.wrapper(main)
