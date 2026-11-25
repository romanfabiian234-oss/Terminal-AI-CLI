"""Decode xterm bracketed paste without interpreting pasted newlines as Enter."""
from __future__ import annotations


class PasteDecoder:
    START = "\x1b[200~"
    END = "\x1b[201~"
    MAX_CHARS = 16384

    def __init__(self) -> None:
        self.in_paste = False
        self.sequence = ""
        self.content: list[str] = []
        self.overflow = False

    def reset(self) -> None:
        self.in_paste = False
        self.sequence = ""
        self.content.clear()
        self.overflow = False

    def feed(self, key: str | int) -> list[tuple[str, str | int]]:
        """Return ('key', value), ('paste', text), or ('error', message)."""
        if isinstance(key, int):
            if self.in_paste:
                return []
            self.sequence = ""
            return [("key", key)]
        events: list[tuple[str, str | int]] = []
        for char in key:
            if self.sequence:
                candidate = self.sequence + char
                marker = self.END if self.in_paste else self.START
                if marker.startswith(candidate):
                    if candidate == marker:
                        if self.in_paste:
                            if self.overflow:
                                events.append(("error", "Вставка слишком длинная — ничего не выполнено."))
                            else:
                                events.append(("paste", "".join(self.content)))
                            self.reset()
                        else:
                            self.in_paste = True
                            self.content.clear()
                            self.overflow = False
                            self.sequence = ""
                    else:
                        self.sequence = candidate
                    continue
                if self.in_paste:
                    self._append(candidate)
                elif not candidate.startswith("\x1b["):
                    # A standalone Escape is not an Enter. Preserve subsequent
                    # printable typing while disregarding unknown control bytes.
                    if char.isprintable():
                        events.append(("key", char))
                self.sequence = ""
                continue
            if char == "\x1b":
                self.sequence = char
            elif self.in_paste:
                self._append(char)
            else:
                events.append(("key", char))
        return events

    def _append(self, value: str) -> None:
        if len(self.content) < self.MAX_CHARS:
            self.content.extend(value[: self.MAX_CHARS - len(self.content)])
        if len(self.content) >= self.MAX_CHARS:
            self.overflow = True


def prepare_pasted_commands(text: str, existing_line: str, max_commands: int = 32) -> list[str]:
    """Validate and split pasted commands for individual manual approval."""
    if any(ord(ch) < 32 and ch not in "\r\n\t" for ch in text) or "\x7f" in text:
        raise ValueError("Вставка содержит управляющие символы и отклонена.")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    commands = [line for line in text.split("\n") if line.strip()]
    if not commands:
        return []
    commands[0] = existing_line + commands[0]
    if len(commands) > max_commands:
        raise ValueError("Слишком много команд во вставке (максимум 32).")
    if any(len(cmd) > 4096 for cmd in commands):
        raise ValueError("Одна из команд слишком длинная (максимум 4096 символов).")
    return commands
