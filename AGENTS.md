# Instructions for AI coding agents

This repository implements a local-first Terminal AI CLI for macOS. Work on the explicit user task; do not claim autonomous background execution or schedule work without explicit permission.

## Applicable guidance

- Follow the owner's [PerfectSkills/PerfectCode](https://github.com/romanfabiian234-oss/PerfectSkills/blob/main/skills/perfectcode/SKILL.md) when accessible. The owner's request and current repository evidence take precedence; if the external skill is unavailable, do not invent its contents.
- Read [PLAN.md](PLAN.md), [README.md](README.md), existing tests and the latest [source report](reports/2026-11-24-mvp.md) before substantial changes. Reuse existing logic instead of adding a second runner, state store, or backend.
- Deliver meaningful increments in sequence. Prefer a minimal working path with reliable tests over speculative architecture. On a bug, confirm the cause and add a focused regression test. Do a final simplification/review pass.

## Non-negotiable behavior

- Explain facts about terminal commands; **never silently execute AI-suggested commands**.
- Treat command output and model responses as untrusted. Never execute model text, embed credentials, bypass TLS or follow redirects containing tokens. Remote server use must be explicitly configured, with secure defaults.
- Avoid storing raw transcripts by default; use bounded in-memory buffers. Redaction is best effort, so do not claim it guarantees secrecy.
- Avoid false success claims based on partial logs. Exit code 0 means the process succeeded as a process, not that its result was correct.
- Preserve real terminal interactivity. Full-screen TUI compatibility is currently limited to `:raw`. Do not claim Codex CLI integration is complete until tested.

## Definition of done

1. Document expected behavior and risks for the stage.
2. Run `python3 -m unittest discover -s tests -v`, a CLI smoke test, and targeted negative cases. For significant TUI changes, test real PTY input, resize, Ctrl+C, and fallback behavior.
3. State exactly what was tested and on which OS. Linux mocks do not prove macOS, performance, remote-server or real Codex compatibility.
4. Preserve a concise checkpoint/report in this repository for significant phases, with exact commit SHA and evidence; avoid unnecessary reports per commit.
5. When authorized, record **one entry per significant task/phase** in the [MauseBot Department Hub registry](https://github.com/romanfabiian234-oss/mausebot-department-hub/blob/main/reports/REGISTRY.md), linking to source evidence. If write access or authorization is missing, prepare a NOT POSTED entry rather than claiming registration.
6. Do not create CI workflows, webhooks, telemetry, scheduled tasks, external API spending or automatic model calls outside the requested scope.

## Next gates

- Persistent interactive shell with clean command boundaries and safe fullscreen fallback.
- Model/server configuration improvements and observable latency/quality tests.
- Real macOS validation (Terminal.app, iTerm2, Codex CLI) and fixes.
- Release only after passing documented checks; never mark a stage ACCEPTED without the required review.
