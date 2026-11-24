"""Entrypoint with terminal UI, model configuration and diagnostics."""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

from .core import CONFIG_SAMPLE, DEFAULT_CONFIG, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-ai", description="Terminal explanations over real command output")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="TOML config file")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("init", help="Write safe example configuration")
    commands.add_parser("doctor", help="Check local configuration and environment")
    commands.add_parser("models", help="List configured models and servers")
    runner = commands.add_parser("run", help="Run command in basic non-interactive mode")
    runner.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.action == "init":
        path = args.config
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(CONFIG_SAMPLE)
        except FileExistsError:
            print(f"Configuration already exists: {path}", file=sys.stderr)
            return 1
        print(f"Created {path}")
        return 0
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    if args.action == "doctor":
        print(f"Python: {platform.python_version()} ({platform.system()})")
        print(f"Shell: {cfg.shell} {'OK' if Path(cfg.shell).exists() else 'NOT FOUND'}")
        print(f"Configuration: {args.config} {'found' if args.config.exists() else 'built-in defaults'}")
        print(f"Selected model: {cfg.active} ({cfg.profile().provider}: {cfg.profile().model})")
        print("Remote requests: disabled unless a profile explicitly permits them")
        return 0 if Path(cfg.shell).exists() else 1
    if args.action == "models":
        for name, profile in cfg.profiles.items():
            print(f"{'*' if name == cfg.active else ' '} {name}: {profile.provider} · {profile.model} · {profile.base_url}")
        return 0
    if args.action == "run":
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("run requires a command after --")
        # Explicitly avoid shell=True; arguments are passed unchanged.
        return subprocess.run(command, check=False).returncode
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive mode needs a terminal. Try 'terminal-ai run -- echo hello'.", file=sys.stderr)
        return 2
    try:
        from .ui import run_ui
        run_ui(cfg)
    except (OSError, ValueError) as exc:
        print(f"Terminal error: {exc}", file=sys.stderr)
        return 1
    return 0
