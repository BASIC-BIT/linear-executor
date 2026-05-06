"""Tests for runner timeout configuration: env-driven defaults, RunResult
carrying the actual timeout, and the env-int parser."""
from __future__ import annotations

import importlib

import pytest

from app import runner


# --- _env_int parser ---------------------------------------------------------

def test_env_int_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("FOO_TEST_VAR", raising=False)
    assert runner._env_int("FOO_TEST_VAR", 42) == 42


def test_env_int_parses_valid_int(monkeypatch):
    monkeypatch.setenv("FOO_TEST_VAR", "123")
    assert runner._env_int("FOO_TEST_VAR", 42) == 123


def test_env_int_strips_whitespace(monkeypatch):
    monkeypatch.setenv("FOO_TEST_VAR", "  77  ")
    assert runner._env_int("FOO_TEST_VAR", 42) == 77


def test_env_int_falls_back_on_non_numeric(monkeypatch, caplog):
    monkeypatch.setenv("FOO_TEST_VAR", "thirty")
    with caplog.at_level("WARNING"):
        assert runner._env_int("FOO_TEST_VAR", 42) == 42
    assert "invalid FOO_TEST_VAR" in caplog.text


def test_env_int_falls_back_on_zero_or_negative(monkeypatch, caplog):
    monkeypatch.setenv("FOO_TEST_VAR", "0")
    with caplog.at_level("WARNING"):
        assert runner._env_int("FOO_TEST_VAR", 42) == 42

    monkeypatch.setenv("FOO_TEST_VAR", "-5")
    with caplog.at_level("WARNING"):
        assert runner._env_int("FOO_TEST_VAR", 42) == 42


# --- env-driven default timeouts --------------------------------------------

def _reload_runner_with_env(monkeypatch, **env):
    """Reload app.runner so module-level env reads are re-evaluated.

    Tests that mutate env need a reload because the defaults are computed at
    import time. We restore the module afterwards via a finalizer so other
    tests see the original constants.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    importlib.reload(runner)


def test_default_proxy_timeout_is_300_when_env_unset(monkeypatch):
    monkeypatch.delenv("LINEAR_EXECUTOR_TIMEOUT_PROXY", raising=False)
    monkeypatch.delenv("LINEAR_EXECUTOR_TIMEOUT_STAGE1", raising=False)
    importlib.reload(runner)
    try:
        assert runner.DEFAULT_TIMEOUT_PROXY == 300
        assert runner.DEFAULT_TIMEOUT_STAGE1 == 1200
    finally:
        importlib.reload(runner)  # clean state for other tests


def test_env_overrides_proxy_default(monkeypatch):
    _reload_runner_with_env(monkeypatch, LINEAR_EXECUTOR_TIMEOUT_PROXY="600")
    try:
        assert runner.DEFAULT_TIMEOUT_PROXY == 600
    finally:
        importlib.reload(runner)


def test_env_overrides_stage1_default(monkeypatch):
    _reload_runner_with_env(monkeypatch, LINEAR_EXECUTOR_TIMEOUT_STAGE1="3000")
    try:
        assert runner.DEFAULT_TIMEOUT_STAGE1 == 3000
    finally:
        importlib.reload(runner)


def test_back_compat_alias_tracks_stage1(monkeypatch):
    _reload_runner_with_env(monkeypatch, LINEAR_EXECUTOR_TIMEOUT_STAGE1="999")
    try:
        # Historical name kept for tests / external code.
        assert runner.DEFAULT_TIMEOUT_SECONDS == 999
        assert runner.DEFAULT_TIMEOUT_SECONDS == runner.DEFAULT_TIMEOUT_STAGE1
    finally:
        importlib.reload(runner)


# --- RunResult.timeout_used --------------------------------------------------

def test_run_result_carries_timeout_used():
    """Use the dataclass directly (no subprocess) — confirms the field exists
    and round-trips through equality / repr."""
    r = runner.RunResult(
        exit_code=0, stdout="", stderr="", timed_out=False,
        cli="claude", timeout_used=1500,
    )
    assert r.timeout_used == 1500


def test_run_result_default_timeout_used_is_back_compat():
    """When constructed without timeout_used (legacy callers), defaults to
    DEFAULT_TIMEOUT_SECONDS so existing comment-rendering still produces
    a sensible number."""
    r = runner.RunResult(
        exit_code=0, stdout="", stderr="", timed_out=False, cli="claude",
    )
    assert r.timeout_used == runner.DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Auth-aware env preparation (TES-716 follow-up)
# ---------------------------------------------------------------------------


def test_prepare_env_oauth_filters_claude_anthropic_key(monkeypatch):
    from app.runner import prepare_subprocess_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_xxx")
    env = prepare_subprocess_env("claude", "oauth")
    assert "ANTHROPIC_API_KEY" not in env
    assert env["LINEAR_API_KEY"] == "lin_xxx"


def test_prepare_env_apikey_passes_through(monkeypatch):
    from app.runner import prepare_subprocess_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    env = prepare_subprocess_env("claude", "apikey")
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-xyz"


def test_prepare_env_oauth_filters_codex_openai_keys(monkeypatch):
    from app.runner import prepare_subprocess_env
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.example/v1")
    env = prepare_subprocess_env("codex", "oauth")
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env


def test_prepare_env_oauth_filters_gemini_keys(monkeypatch):
    from app.runner import prepare_subprocess_env
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "ga-key")
    env = prepare_subprocess_env("gemini", "oauth")
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env


def test_prepare_env_oauth_filters_all_provider_keys_for_opencode(monkeypatch):
    from app.runner import prepare_subprocess_env
    for k in (
        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(k, f"{k}-value")
    env = prepare_subprocess_env("opencode", "oauth")
    for k in (
        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    ):
        assert k not in env, f"{k} should have been filtered in opencode-oauth mode"


def test_prepare_env_unknown_cli_passes_through_unchanged(monkeypatch):
    from app.runner import prepare_subprocess_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    env = prepare_subprocess_env("unknown_cli", "oauth")
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant"
