"""Coding-CLI registry + Linear-label-driven router (TES-646).

The Linear-Executor was originally hard-wired to ``claude --print``. From
TES-646 onwards a CLI can be selected per-ticket via a Linear label of
the form ``cli:<name>`` (e.g. ``cli:opencode``, ``cli:forge``). When no
such label is present the registry default ``"claude"`` is used.

Adding a new backend means a single entry in :data:`CLI_REGISTRY` plus
the matching ``cli:<name>`` label in Linear. The runner appends the
prompt as the final argv element — every entry below is shaped so that
form works (positional last arg or last-flag-value, both fine).

Caveats per backend live in TES-646's "Open Questions" section:
auth state per CLI (~/.claude vs ~/.opencode vs ChatGPT-Login etc.),
non-interactive approval flags, and MCP availability differ. First-pass
implementation uses the simplest invocation that yields output; tuning
happens during the smoke-tests called out in the ticket's success criteria.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("linear-executor")


CLI_LABEL_PREFIX = "cli:"
MODEL_LABEL_PREFIX = "model:"
DEFAULT_CLI = "claude"


# argv prefix per CLI; the prompt is appended as the final element.
CLI_REGISTRY: dict[str, list[str]] = {
    "claude":   ["claude", "--print", "--dangerously-skip-permissions"],
    # opencode `run` ignores the user-config default model and falls back
    # to a hardcoded anthropic/claude-opus-4-6 placeholder; pass model
    # explicitly so non-TTY runs work. Model override per ticket TBD.
    "opencode": ["opencode", "run", "--model", "opencode-go/minimax-m2.5"],
    "codex":    ["codex", "exec"],
    "gemini":   ["gemini", "--prompt"],
    "forge":    ["forge", "--prompt"],
}

# Fallback paths checked when shutil.which() misses — systemd --user
# services and similar non-login environments often lack ~/.local/bin/
# and ~/.npm-global/bin/ on PATH.
_FALLBACK_PATHS: dict[str, list[str]] = {
    "claude":   ["~/.local/bin/claude"],
    "opencode": ["~/.opencode/bin/opencode", "~/.local/bin/opencode"],
    "codex":    ["~/.npm-global/bin/codex", "~/.local/bin/codex"],
    "gemini":   ["~/.npm-global/bin/gemini", "~/.local/bin/gemini"],
    "forge":    ["~/.local/bin/forge"],
}


def resolve_bin(cli_name: str) -> str:
    """Return the absolute path of the CLI binary (or bare name as last resort)."""
    if cli_name not in CLI_REGISTRY:
        raise ValueError(f"unknown cli {cli_name!r}; known: {sorted(CLI_REGISTRY)}")
    bin_name = CLI_REGISTRY[cli_name][0]
    found = shutil.which(bin_name)
    if found:
        return found
    for fallback in _FALLBACK_PATHS.get(cli_name, ()):
        p = Path(fallback).expanduser()
        if p.exists():
            return str(p)
    return bin_name  # last resort — will fail loudly with FileNotFoundError


def _label_names(labels) -> list[str]:
    """Coerce Linear payload labels to a flat list[str].

    Linear sends labels either as ``[{"id": ..., "name": "cli:forge"}, ...]``
    or as a list of strings depending on context — accept both.
    """
    if not labels:
        return []
    out: list[str] = []
    for l in labels:
        if isinstance(l, str):
            out.append(l)
        elif isinstance(l, dict):
            n = l.get("name")
            if n:
                out.append(n)
    return out


def resolve_cli(labels) -> str:
    """Pick a registered CLI from a list of Linear labels.

    The first ``cli:<name>`` label (alphabetically) wins — deterministic
    when several have been added by accident. Unknown values fall back
    to :data:`DEFAULT_CLI` with a warning. Empty / missing labels →
    :data:`DEFAULT_CLI` silently.
    """
    cli_labels = sorted(
        n for n in _label_names(labels) if n.startswith(CLI_LABEL_PREFIX)
    )
    if not cli_labels:
        return DEFAULT_CLI
    if len(cli_labels) > 1:
        logger.warning(
            "resolve_cli — multiple cli:* labels %r; picking %r",
            cli_labels, cli_labels[0],
        )
    chosen = cli_labels[0][len(CLI_LABEL_PREFIX):]
    if chosen not in CLI_REGISTRY:
        logger.warning(
            "resolve_cli — unknown cli %r; falling back to %r",
            chosen, DEFAULT_CLI,
        )
        return DEFAULT_CLI
    return chosen


def resolve_model(labels) -> str | None:
    """Pick a model id from ``model:<id>`` Linear labels, or None.

    First match alphabetically wins (deterministic). Multiple labels
    log a warning. Returns the part after ``model:`` verbatim — the
    caller is responsible for matching it against the chosen CLI's
    accepted model namespace.
    """
    model_labels = sorted(
        n for n in _label_names(labels) if n.startswith(MODEL_LABEL_PREFIX)
    )
    if not model_labels:
        return None
    if len(model_labels) > 1:
        logger.warning(
            "resolve_model — multiple model:* labels %r; picking %r",
            model_labels, model_labels[0],
        )
    return model_labels[0][len(MODEL_LABEL_PREFIX):] or None


def build_argv(cli_name: str, prompt: str, *, model: str | None = None) -> list[str]:
    """Construct the full argv for invoking ``cli_name`` with ``prompt``.

    If ``model`` is provided, it overrides the registry's default
    model: the existing ``--model X`` pair in the spec is replaced with
    ``--model <model>``; if no ``--model`` flag is present, one is
    appended before the prompt.
    """
    if cli_name not in CLI_REGISTRY:
        raise ValueError(f"unknown cli {cli_name!r}; known: {sorted(CLI_REGISTRY)}")
    spec = CLI_REGISTRY[cli_name]
    argv = [resolve_bin(cli_name), *spec[1:]]
    if model:
        if "--model" in argv:
            i = argv.index("--model")
            argv[i + 1] = model
        else:
            argv.extend(["--model", model])
    argv.append(prompt)
    return argv
