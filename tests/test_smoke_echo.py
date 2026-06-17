"""End-to-end smoke test for the echo backend (pure echo, no AI).

The echo backend is a Python one-liner that writes its argv as UTF-8 — it's always
available (Python on PATH) and never makes network calls. This test validates
the full Linear-Executor pipeline end-to-end:

  * CLI resolution via :func:`app.cli_registry.build_argv`
  * Subprocess env preparation via :func:`app.runner.prepare_subprocess_env`
  * Subprocess spawn, output capture, and exit-code handling via :func:`app.runner.run_cli`

Unlike other tests, this does NOT mock ``run_cli`` — it exercises the real
subprocess machinery with a trivially deterministic backend.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.cli_registry import CLI_REGISTRY, build_argv
from app.runner import prepare_subprocess_env, RunResult, run_cli


pytestmark = pytest.mark.smoke


def test_echo_backend_registry_entry():
    """The echo backend must stay registered with its python one-liner."""
    assert "echo" in CLI_REGISTRY
    entry = CLI_REGISTRY["echo"]
    assert entry[0] == "python"  # bare name, resolved at runtime by resolve_bin
    assert entry[1] == "-c"
    assert "sys.stdout.buffer.write" in entry[2]


def test_build_argv_echo_constructs_correct_command():
    """build_argv('echo', ...) resolves the python binary and appends the prompt."""
    prompt = "This is a smoke test prompt"
    argv = build_argv("echo", prompt)
    assert argv[-1] == prompt
    # argv[0] is resolved to full path; match case-insensitively on Windows
    assert argv[0].lower().endswith("python.exe"), f"unexpected bin: {argv[0]}"
    assert argv[1] == "-c"
    assert "sys.stdout.buffer.write" in argv[2]


def test_echo_subprocess_roundtrip():
    """Drive the echo CLI directly via subprocess — no mocking.

    Build the argv, prepare the env, spawn, capture, verify.
    """
    prompt = "BAS-6 smoke test: verify echo back-end round-trips correctly"
    argv = build_argv("echo", prompt)
    env = prepare_subprocess_env("echo", "oauth")

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 0, f"echo exited {proc.returncode}: stderr={stderr!r}"
    assert stdout.strip() == prompt, (
        f"echo output mismatch\n"
        f"  expected: {prompt!r}\n"
        f"  actual:   {stdout.strip()!r}"
    )
    assert stderr == ""


def test_echo_run_cli_end_to_end(tmp_path):
    """Run the echo backend through runner.run_cli() end-to-end.

    This tests the full path: argv construction → env preparation → subprocess
    spawn → stdout/stderr capture → RunResult construction.
    """
    prompt = "run_cli end-to-end: this should be echoed back verbatim"
    result = run_cli("echo", prompt, tmp_path, timeout=30)

    assert result.exit_code == 0, (
        f"exit code {result.exit_code}; stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == prompt, (
        f"stdout mismatch\n"
        f"  expected: {prompt!r}\n"
        f"  actual:   {result.stdout.strip()!r}"
    )
    assert result.stderr == ""
    assert not result.timed_out
    assert result.cli == "echo"
    assert result.timeout_used == 30


def test_echo_run_cli_multiple_prompts(tmp_path):
    """Different prompts all echo back correctly.

    The echo backend joins all args with spaces (``' '.join(sys.argv[1:])``),
    so multiline prompts come back as single-line space-joined text.
    """
    prompts = [
        "short",
        "a bit longer prompt with numbers 123",
        "   leading and trailing whitespace   ",
    ]
    for prompt in prompts:
        result = run_cli("echo", prompt, tmp_path, timeout=30)
        assert result.exit_code == 0, f"prompt={prompt!r} exit={result.exit_code}"
        assert result.stdout.strip() == prompt.strip(), (
            f"prompt={prompt!r}\n  stdout={result.stdout.strip()!r}"
        )


def test_echo_on_start_callback_invoked(tmp_path):
    """Verify the on_start callback fires during run_cli (used by orchestrator
    to register Popen for cancel-via-Linear-status)."""
    callback_called = False
    callback_proc = None

    def on_start(proc):
        nonlocal callback_called, callback_proc
        callback_called = True
        callback_proc = proc

    result = run_cli("echo", "on_start test", tmp_path, timeout=30, on_start=on_start)

    assert callback_called, "on_start was never invoked"
    assert callback_proc is not None, "on_start received None as proc"
    assert callback_proc.returncode is not None, "subprocess should have terminated"


def test_echo_reasonably_long_prompt(tmp_path):
    """A reasonably long prompt is echoed back correctly.

    Windows has a ~8191-char command-line limit for CreateProcess, so keep the
    prompt well within that bound.
    """
    long_prompt = "x" * 3_000
    result = run_cli("echo", long_prompt, tmp_path, timeout=30)

    assert result.exit_code == 0
    assert result.stdout.strip() == long_prompt


def test_echo_unicode_prompt_roundtrips_without_mojibake(tmp_path):
    prompt = "unicode smoke: progress 🔄 emoji — no mojibake"
    result = run_cli("echo", prompt, tmp_path, timeout=30)

    assert result.exit_code == 0
    assert result.stdout.strip() == prompt
    assert "ðŸ" not in result.stdout
    assert "â€”" not in result.stdout


def test_echo_prepare_env_oauth_mode(tmp_path):
    """In OAuth mode the echo backend's env has no API keys filtered (echo has
    no api_key_env_vars in its AuthSpec)."""
    result = run_cli("echo", "env test", tmp_path, timeout=30, auth_mode="oauth")
    assert result.exit_code == 0
    assert result.stdout.strip() == "env test"


def test_echo_prepare_env_apikey_mode(tmp_path):
    """In API-key mode the env is passed through unchanged."""
    result = run_cli("echo", "env apikey test", tmp_path, timeout=30, auth_mode="apikey")
    assert result.exit_code == 0
    assert result.stdout.strip() == "env apikey test"
