"""Non-blocking, opt-in-by-installation completions using Microsoft's Inshellisense.

Uses the documented "is complete <input>" JSON CLI; never executes a suggestion.
The external program is only invoked locally, without shell interpolation.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Completion:
    name: str
    insert: str
    kind: str
    description: str = ""


_KINDS = {"folder", "file", "option", "subcommand", "arg", "command", "shortcut"}
_SENSITIVE = re.compile(r"(?i)(?:password|passphrase|secret|token|api[_-]?key|authorization)\s*[:=]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value)
    return "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n\t")[:limit]


def safe_to_complete(line: str) -> bool:
    return bool(line.strip() and len(line) <= 500 and
                not any(ch in line for ch in "\r\n\x00") and
                not _SENSITIVE.search(line))


def parse_completions(payload: str, limit: int = 7) -> tuple[Completion, ...]:
    try:
        raw = json.loads(payload)
    except (ValueError, TypeError):
        return ()
    if not isinstance(raw, dict) or not isinstance(raw.get("suggestions"), list):
        return ()
    result: list[Completion] = []
    seen: set[str] = set()
    for value in raw["suggestions"][:75]:
        if not isinstance(value, dict):
            continue
        name = _clean(value.get("name"), 120)
        insert = _clean(value.get("insertValue"), 200) or name
        if not name or not insert or insert in seen:
            continue
        seen.add(insert)
        kind = value.get("type")
        result.append(Completion(
            name=name, insert=insert,
            kind=kind if isinstance(kind, str) and kind in _KINDS else "command",
            description=_clean(value.get("description"), 180),
        ))
        if len(result) >= limit:
            break
    return tuple(result)


def matching_completions(line: str, choices: tuple[Completion, ...], limit: int = 7) -> tuple[Completion, ...]:
    """Match the current token, then apply narrow command-specific filtering."""
    if not line:
        return choices[:limit]

    # Keep Inshellisense's original order for unrelated commands.
    parts = line.strip().split(maxsplit=1)
    command = parts[0].rsplit("/", 1)[-1] if parts else ""
    has_arguments = len(line.lstrip().split(maxsplit=1)) > 1 or line.endswith(" ")
    argument = line.strip().split(maxsplit=1)[1] if len(parts) > 1 else ""
    context = None
    if has_arguments and command == "cd" and not argument.startswith("-"):
        context = "directory"
    elif (has_arguments and re.fullmatch(r"python(?:2|3)?(?:\.\d+)?", command)
          and not argument.startswith("-") and len(argument.split()) <= 1):
        context = "python_script"

    if line[-1].isspace():
        matched = choices
    else:
        partial = line.split()[-1].lstrip("'\\\"")
        basename = partial.rsplit("/", 1)[-1].casefold()
        matched = tuple(choice for choice in choices
                        if (not basename or choice.name.casefold().startswith(basename)
                            or choice.insert.casefold().startswith(partial.casefold())
                            or choice.insert.rsplit("/", 1)[-1].casefold().startswith(basename)))

    if context == "directory":
        return tuple(choice for choice in matched
                     if choice.kind == "folder" or choice.insert.endswith("/"))[:limit]
    if context == "python_script":
        scripts = tuple(choice for choice in matched
                        if choice.kind != "folder" and
                        (choice.name.casefold().endswith(".py") or
                         choice.insert.casefold().endswith(".py")))
        folders = tuple(choice for choice in matched
                        if choice.kind == "folder" or choice.insert.endswith("/"))
        return (scripts + folders)[:limit]
    return matched[:limit]


def replace_completion(line: str, choice: Completion) -> str:
    """Replace the unfinished final token, never press Enter for the user."""
    last_word = re.search(r"[^ \t]*$", line)
    if last_word is None or not choice.insert:
        return line
    return line[:last_word.start()] + choice.insert


def _invoke(binary: str, line: str, cwd: Path) -> tuple[Completion, ...]:
    try:
        completed = subprocess.run(
            [binary, "complete", line], cwd=cwd, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ()
    if completed.returncode or len(completed.stdout) > 256_000:
        return ()
    return matching_completions(line, parse_completions(completed.stdout, limit=75))


class CompletionWorker:
    """One in-flight completion at a time; stale responses are discarded."""
    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary if binary is not None else (shutil.which("is") or shutil.which("inshellisense"))
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-ai-complete") if self.binary else None
        self._future: Future[tuple[Completion, ...]] | None = None
        self._query = ""
        self._last_query = ""

    def request(self, line: str, cwd: Path) -> None:
        if not self._pool or not self.binary or not safe_to_complete(line):
            return
        query = f"{cwd}\x00{line}"
        if self._future is not None or query == self._last_query:
            return
        self._last_query = query
        self._query = query
        self._future = self._pool.submit(_invoke, self.binary, line, cwd)

    def collect(self, line: str, cwd: Path) -> tuple[Completion, ...] | None:
        if self._future is None or not self._future.done():
            return None
        future, query = self._future, self._query
        self._future = None
        try:
            result = future.result()
        except Exception:
            result = ()
        return result if query == f"{cwd}\x00{line}" else None

    def close(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
