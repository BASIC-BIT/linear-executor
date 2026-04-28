# AGENTS.md — guidance for AI coding agents

Concise project context for any AI coding agent (Claude Code, opencode, codex, gemini, forge, …) working in this repo. Full project documentation lives in `README.md`; this file is the orientation map.

## Quick orientation

- Webhook entry: `app/main.py`; HMAC verify: `app/signature.py`
- State machine: `app/filter.py` (AI Implementation / In Review / Stop AI / AI Batch / Done)
- CLI selection: `app/cli_registry.py` (label `cli:<name>`, default `claude`)
- Job lifecycle: `app/orchestrator.py` + `app/queue.py` (SQLite)
- Worktree handling: `app/git_ops.py`
- Polling worker: `app/worker.py`
- Cancel via Stop AI status: `app/cancel.py` (SIGTERM)
- Linear REST/GraphQL: `app/linear_api.py`

## Conventions

- Python 3.13 with `uv` (no plain `pip`)
- Tests: `pytest` in `tests/`
- Run locally: `uvicorn app.main:app --reload`
- Webhook secret + Linear API key via `.env` (see `.env.example`)
- Do not commit: `.env`, `state/`, `proxy-outputs/`, `.claude/`, `originals/youtube-*.md` (already in `.gitignore`)

## Code style

- Follow existing patterns in `app/` — light, async, FastAPI-idiomatic
- Adding a new CLI: extend `CLI_REGISTRY` in `cli_registry.py` and `_FALLBACK_PATHS`
- New Linear state constants: `app/filter.py` (top of file, all constants centralised)

## Scope guardrails

- Surgical changes only — every modified line should be traceable to the requested task; no drive-by refactors or style fixes
- Don't introduce new dependencies without a clear reason; prefer the standard library and what's already in `pyproject.toml`
- Keep handlers small and testable; new behaviour belongs in a unit-tested module under `app/`, exercised via `tests/`
