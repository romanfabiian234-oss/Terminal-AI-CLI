"""Decode xterm SGR wheel events on curses builds without button 5.

Mouse reports use ESC [ < button ; column ; row M, where 64 is wheel
up and 65 is wheel down, possibly with modifier bits. Other mouse
reports are ignored (not passed into the shell input).
"""
from __future__ import annotations


class SGRWheelDecoder:
    PREFIX = "\x1b[<"
    MAX_SEQUENCE = 64

    def __init__(self) -> None:
        self.buffer = ""
        self.discard = False

    def reset(self) -> None:
        self.buffer = ""
        self.discard = False

    def feed(self, key: str | int) -> list[tuple[str, str | int]]:
        if isinstance(key, int):
            self.reset()
            return [("key", key)]
        results: list[tuple[str, str | int]] = []
        for char in key:
            if not self.buffer:
                if char == "\x1b":
                    self.buffer = char
                else:
                    results.append(("key", char))
                continue

            candidate = self.buffer + char
            if self.PREFIX.startswith(candidate):
                self.buffer = candidate
                continue
            if candidate.startswith(self.PREFIX):
                if char in "Mm":
                    # Click reports are discarded: only wheel is supported.
                    params = candidate[len(self.PREFIX):-1].split(";")
                    if not self.discard and len(params) == 3 and all(
                            p.isdecimal() and len(p) <= 8 for p in params):
                        code, x, y = map(int, params)
                        # Wheel codes 64 and 65 plus shift/alt/ctrl bits.
                        if char == "M" and 1 <= x <= 100000 and 1 <= y <= 100000:
                            code &= ~28
                            if code == 64:
                                results.append(("wheel", "up"))
                            elif code == 65:
                                results.append(("wheel", "down"))
                    self.reset()
                    continue
                if char == "\x1b":
                    self.buffer = char
                    self.discard = False
                    continue
                if len(candidate) < self.MAX_SEQUENCE and char not in "\r\n":
                    self.buffer = candidate
                    if not (char.isascii() and (char.isdigit() or char == ";")):
                        self.discard = True
                    continue
                # Malformed mouse packet: never add it to the command input.
                self.reset()
                continue
            # Not a mouse packet: replay the complete sequence for bracketed
            # paste or normal input. The paste decoder handles its own escapes.
            results.extend(("key", c) for c in candidate)
            self.reset()
        return results
