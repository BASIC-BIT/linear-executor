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
