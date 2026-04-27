"""Shared test fixtures.

`runner.run_cli` and `runner.run_claude` are auto-mocked for every test so
the suite never spawns a real coding-CLI subprocess. `git_ops.is_git_repo`
defaults to False so orchestrator tests stay in the non-git path unless
they explicitly opt in. Every test gets a fresh SQLite queue file under
``tmp_path`` so webhook-level tests never see state from a previous run.
Tests that need different behavior can override via monkeypatch.
"""
import pytest

from app import git_ops, runner


@pytest.fixture(autouse=True)
def _isolated_queue_db(monkeypatch, tmp_path):
    """Point the executor's queue at a per-test SQLite file."""
    monkeypatch.setenv("LINEAR_EXECUTOR_DB", str(tmp_path / "jobs.db"))


@pytest.fixture(autouse=True)
def _no_real_cli(monkeypatch):
    def fake_run_cli(cli, prompt, cwd, *, timeout=runner.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None):
        cwd.mkdir(parents=True, exist_ok=True)
        return runner.RunResult(
            exit_code=0,
            stdout="(mocked claude output)",
            stderr="",
            timed_out=False,
            cli=cli,
        )

    def fake_run_claude(prompt, cwd, *, timeout=runner.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None):
        return fake_run_cli("claude", prompt, cwd, timeout=timeout, on_start=on_start)

    monkeypatch.setattr(runner, "run_cli", fake_run_cli)
    monkeypatch.setattr(runner, "run_claude", fake_run_claude)


@pytest.fixture(autouse=True)
def _git_disabled_by_default(monkeypatch, request):
    """Default to non-git so orchestrator tests don't accidentally invoke git.
    Tests that exercise real git ops live in test_git_ops.py and use a real
    temp repo — they bypass this by importing git_ops directly (the patch
    here only shadows is_git_repo; real subprocess git calls in test_git_ops
    still hit the actual binary)."""
    if "git_ops" in request.module.__name__:
        return  # let git_ops tests use the real implementation
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: False)
