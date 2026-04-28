"""Run a coding CLI as a subprocess in a target folder.

Originally Claude-Code-only (``claude --print --dangerously-skip-permissions``).
Since TES-646 the executor routes to any registered CLI based on the
ticket's ``cli:<name>`` Linear label — see :mod:`app.cli_registry`. The
non-interactive / skip-permissions flag is required because no human is
at the terminal to answer prompts; risk is mitigated by the worktree
isolation pattern and conservative system-prompt rules.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.cli_registry import DEFAULT_CLI, build_argv, resolve_bin


logger = logging.getLogger("linear-executor")


# Hard cap on per-ticket timeout overrides (label-driven). Anything above this
# is clamped — runaway jobs are a pile-up risk for the worker, and tickets that
# legitimately need >2h should be split instead. Adjust if you have a workload
# that truly needs longer single-shot runs.
TIMEOUT_HARD_CAP_SECONDS = 7200  # 2 hours


def _env_int(name: str, default: int) -> int:
    """Parse ``name`` from env as int, fall back to ``default`` on missing /
    invalid. Logs a warning on invalid values so misconfig is visible."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError("must be positive")
        return v
    except (TypeError, ValueError) as exc:
        logger.warning("invalid %s=%r (%s); using default %ds", name, raw, exc, default)
        return default


# Default timeouts per execution path. The two paths have very different
# expected workloads — Proxy is mobile-style Q&A (transcribe / scrape /
# summarise), Stage 1 is real coding / multi-step research. Override either
# via env vars; per-ticket override via the ``timeout:<sec>`` Linear label.
DEFAULT_TIMEOUT_PROXY = _env_int("LINEAR_EXECUTOR_TIMEOUT_PROXY", 300)         # 5 min
DEFAULT_TIMEOUT_STAGE1 = _env_int("LINEAR_EXECUTOR_TIMEOUT_STAGE1", 1200)      # 20 min

# Back-compat alias for callers / tests that reference the historical
# constant. Treat it as the Stage-1 default — that was the original semantic
# for non-trivial coding tasks. New code should pick the right path-specific
# default explicitly.
DEFAULT_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_STAGE1


# Back-compat: external code (and the original Phase-2 code path) referenced
# CLAUDE_BIN at import time. Kept resolved against the claude binary so any
# diagnostic that printed it still makes sense.
CLAUDE_BIN = resolve_bin("claude")


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    cli: str = DEFAULT_CLI  # which backend produced this result
    timeout_used: int = DEFAULT_TIMEOUT_SECONDS  # actual seconds the run was allowed


def run_cli(
    cli: str,
    prompt: str,
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    on_start: Optional[Callable[[subprocess.Popen], None]] = None,
    model: Optional[str] = None,
) -> RunResult:
    """Invoke ``cli`` headless in ``cwd`` and return captured output.

    Internally uses ``Popen`` with ``start_new_session=True`` so the CLI
    process and any of its descendants form an isolated process group
    (TES-596 cancel-by-Linear-status: an external thread can call
    ``proc.terminate()`` / ``proc.kill()`` on the group leader and take
    down all children in one go).

    Args:
        cli: registry key from :data:`app.cli_registry.CLI_REGISTRY`.
        on_start: optional callback invoked synchronously with the Popen
            object immediately after spawn. orchestrate_* uses it to
            register the Popen in ``job_registry`` so cancel webhooks can
            find and terminate it. Exceptions inside the callback are
            logged but do not abort the run.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = build_argv(cli, prompt, model=model)
    logger.info(
        "%s subprocess starting — bin=%s cwd=%s timeout=%ds",
        cli, cmd[0], cwd, timeout,
    )

    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    if on_start is not None:
        try:
            on_start(proc)
        except Exception as exc:
            logger.warning("run_cli on_start callback raised: %s", exc)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(
            "%s subprocess timed out after %ds — killing process group",
            cli, timeout,
        )
        try:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", f"TIMEOUT after {timeout}s (kill failed)"
        return RunResult(
            exit_code=-1,
            stdout=stdout or "",
            stderr=stderr or f"TIMEOUT after {timeout}s",
            timed_out=True,
            cli=cli,
            timeout_used=timeout,
        )

    logger.info(
        "%s subprocess done — exit=%d stdout_len=%d",
        cli, proc.returncode, len(stdout or ""),
    )
    return RunResult(
        exit_code=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=False,
        cli=cli,
        timeout_used=timeout,
    )


def run_claude(
    prompt: str,
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    on_start: Optional[Callable[[subprocess.Popen], None]] = None,
) -> RunResult:
    """Backwards-compat wrapper — delegates to :func:`run_cli` with ``cli="claude"``."""
    return run_cli("claude", prompt, cwd, timeout=timeout, on_start=on_start)
