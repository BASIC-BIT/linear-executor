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
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger("linear-executor")


CLI_LABEL_PREFIX = "cli:"
MODEL_LABEL_PREFIX = "model:"
TIMEOUT_LABEL_PREFIX = "timeout:"
AUTH_LABEL_PREFIX = "auth:"
DEFAULT_CLI = "claude"

AuthMode = Literal["oauth", "apikey"]
VALID_AUTH_MODES: tuple[AuthMode, ...] = ("oauth", "apikey")


# argv prefix per CLI; the prompt is appended as the final element.
# Note: opencode's `--model` is NOT here anymore — it depends on auth_mode
# (Go-Plan model for OAuth vs provider model for API-key). build_argv()
# injects the right one from AUTH_REGISTRY when no explicit model is given.
CLI_REGISTRY: dict[str, list[str]] = {
    "claude":   ["claude", "--print", "--dangerously-skip-permissions"],
    "opencode": ["opencode", "run"],
    # Codex' Sandbox (vendored bubblewrap) braucht User-Namespace
    # Network-Caps die auf Hostinger-VPS gesperrt sind (RTM_NEWADDR auf
    # loopback) — `bash` und andere Tools failen damit deterministisch.
    # Im Executor-Kontext ist die Sandbox auch konzeptionell redundant:
    # kein Mensch am Terminal, jeder Run lebt in einem Worktree, das
    # `--dangerously-skip-permissions`-Pendant für Claude ist genau das.
    # Bypass komplett, sonst ist Codex auf diesem VPS nur Read+Web-Search.
    "codex":    ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
    "gemini":   ["gemini", "--prompt"],
    "forge":    ["forge", "--prompt"],
}


@dataclass(frozen=True)
class AuthSpec:
    """Per-CLI auth metadata — used by the runner to filter env-vars and by
    build_argv() to pick auth-mode-specific default models.

    Attributes:
        api_key_env_vars: Env vars that activate API-key mode in this CLI.
            In OAuth mode the runner strips these from the subprocess env so
            the CLI falls back to its login state (e.g. ``~/.claude/.credentials.json``,
            ``~/.codex/auth.json``). In API-key mode they're left in place.
        default_oauth_model: Default model id when auth_mode=oauth and no
            explicit model is requested. None = let CLI pick.
        default_apikey_model: Default model id when auth_mode=apikey and no
            explicit model is requested. None = let CLI pick (or fail if
            CLI requires explicit).
        default_mode: Which auth mode this CLI uses if neither label nor
            per-CLI env override sets one. "oauth" for everything we own.
    """
    api_key_env_vars: tuple[str, ...] = field(default_factory=tuple)
    default_oauth_model: str | None = None
    default_apikey_model: str | None = None
    default_mode: AuthMode = "oauth"


# Per-CLI auth specifications.
# Sources for env-var lists:
#   * claude → ANTHROPIC_API_KEY (Anthropic console credits vs claude.ai/Max OAuth)
#   * codex → OPENAI_API_KEY / OPENAI_BASE_URL (OpenAI API credits vs ChatGPT Plus/Pro OAuth)
#   * gemini → GEMINI_API_KEY / GOOGLE_API_KEY (AI Studio billing vs Code Assist OAuth)
#   * opencode → all provider keys (OAuth uses Go-Plan login, API-key uses
#     whichever provider key + model namespace; we filter all to avoid
#     opencode silently routing via a stale provider key when user wanted Go-Plan)
AUTH_REGISTRY: dict[str, AuthSpec] = {
    "claude": AuthSpec(
        api_key_env_vars=("ANTHROPIC_API_KEY",),
    ),
    "codex": AuthSpec(
        api_key_env_vars=("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    ),
    "gemini": AuthSpec(
        api_key_env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
    "opencode": AuthSpec(
        api_key_env_vars=(
            "OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GROQ_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        ),
        # Default: DeepSeek V4 Flash via Go-Plan — released 2026-04-24, ~5x höhere
        # Quota als MiniMax M2.5 (31k/5h vs 6k/5h), 1M context vs 128k, in Reviews
        # mindestens auf Augenhöhe für Standard-Coding-Tasks. Pro-Variante via
        # `model:opencode-go/deepseek-v4-pro`-Label für Heavy-Lifting.
        default_oauth_model="opencode-go/deepseek-v4-flash",
        default_apikey_model="openrouter/anthropic/claude-haiku-4.5",
    ),
    # Forge: auth pattern not yet documented in this codebase. Empty spec
    # means no env filtering and no auto-model — fill in when actually used.
    "forge": AuthSpec(),
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


def resolve_auth(labels, cli: str) -> AuthMode:
    """Pick an auth mode for a CLI from labels / env / spec default.

    Precedence (high → low):

    1. Per-ticket Linear label ``auth:oauth`` or ``auth:apikey``. First
       match alphabetically wins; multiple labels log a warning. Unknown
       values are ignored with a warning (fall through to next tier).
    2. Per-CLI env override ``LINEAR_EXECUTOR_<CLI>_AUTH=oauth|apikey``
       (e.g. ``LINEAR_EXECUTOR_CLAUDE_AUTH=apikey``). Lets you flip a
       single CLI globally without touching tickets.
    3. ``AUTH_REGISTRY[cli].default_mode`` (typically ``"oauth"``).

    Unknown / unregistered CLIs default to ``"oauth"`` and log a warning.
    """
    auth_labels = sorted(
        n for n in _label_names(labels) if n.startswith(AUTH_LABEL_PREFIX)
    )
    if auth_labels:
        if len(auth_labels) > 1:
            logger.warning(
                "resolve_auth — multiple auth:* labels %r; picking %r",
                auth_labels, auth_labels[0],
            )
        chosen = auth_labels[0][len(AUTH_LABEL_PREFIX):].strip().lower()
        if chosen in VALID_AUTH_MODES:
            return chosen  # type: ignore[return-value]
        logger.warning(
            "resolve_auth — unknown auth mode %r; falling through to env/default",
            chosen,
        )

    env_var = f"LINEAR_EXECUTOR_{cli.upper()}_AUTH"
    env_value = os.environ.get(env_var, "").strip().lower()
    if env_value in VALID_AUTH_MODES:
        return env_value  # type: ignore[return-value]
    if env_value:
        logger.warning(
            "resolve_auth — invalid %s=%r; falling through to spec default",
            env_var, env_value,
        )

    spec = AUTH_REGISTRY.get(cli)
    if spec is None:
        logger.warning(
            "resolve_auth — no AuthSpec for cli=%r; defaulting to oauth", cli,
        )
        return "oauth"
    return spec.default_mode


def resolve_timeout(labels) -> int | None:
    """Pick a timeout (seconds) from a ``timeout:<sec>`` Linear label, or None.

    Accepts plain integer seconds (``timeout:1800``) or with a trailing ``s``
    suffix (``timeout:1800s``) for symmetry with how humans tend to type it.
    Invalid / non-positive values are ignored with a warning so a typo doesn't
    silently apply zero-timeout.

    Returns the parsed integer seconds, or None if no recognisable label is
    present. The caller decides which default to apply when None is returned
    and is also responsible for clamping against a hard cap.
    """
    timeout_labels = sorted(
        n for n in _label_names(labels) if n.startswith(TIMEOUT_LABEL_PREFIX)
    )
    if not timeout_labels:
        return None
    if len(timeout_labels) > 1:
        logger.warning(
            "resolve_timeout — multiple timeout:* labels %r; picking %r",
            timeout_labels, timeout_labels[0],
        )
    raw = timeout_labels[0][len(TIMEOUT_LABEL_PREFIX):].strip().rstrip("sS")
    if not raw:
        return None
    try:
        secs = int(raw)
    except ValueError:
        logger.warning(
            "resolve_timeout — cannot parse %r as int seconds; ignoring",
            timeout_labels[0],
        )
        return None
    if secs <= 0:
        logger.warning(
            "resolve_timeout — non-positive timeout %r; ignoring", timeout_labels[0],
        )
        return None
    return secs


def build_argv(
    cli_name: str,
    prompt: str,
    *,
    model: str | None = None,
    auth_mode: AuthMode = "oauth",
) -> list[str]:
    """Construct the full argv for invoking ``cli_name`` with ``prompt``.

    Model resolution (high → low):
        1. Explicit ``model`` argument (from ``model:<id>`` label).
        2. ``AUTH_REGISTRY[cli].default_oauth_model`` if auth_mode=oauth.
        3. ``AUTH_REGISTRY[cli].default_apikey_model`` if auth_mode=apikey.
        4. Whatever the CLI itself decides (no ``--model`` flag injected).

    If a model is chosen, ``--model X`` is appended before the prompt
    (or replaced in-place if the registry prefix already contains it).
    """
    if cli_name not in CLI_REGISTRY:
        raise ValueError(f"unknown cli {cli_name!r}; known: {sorted(CLI_REGISTRY)}")
    spec = CLI_REGISTRY[cli_name]
    argv = [resolve_bin(cli_name), *spec[1:]]

    chosen_model = model
    if chosen_model is None:
        auth_spec = AUTH_REGISTRY.get(cli_name)
        if auth_spec is not None:
            if auth_mode == "oauth":
                chosen_model = auth_spec.default_oauth_model
            else:
                chosen_model = auth_spec.default_apikey_model

    if chosen_model:
        if "--model" in argv:
            i = argv.index("--model")
            argv[i + 1] = chosen_model
        else:
            argv.extend(["--model", chosen_model])
    argv.append(prompt)
    return argv
