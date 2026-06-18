"""Unit tests for the cli_registry router (TES-646)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.cli_registry import (
    CLI_LABEL_PREFIX,
    CLI_REGISTRY,
    DEFAULT_CLI,
    DEFAULT_OPENCODE_MODEL,
    build_argv,
    resolve_context_mode,
    resolve_cli,
    resolve_bin,
    resolve_reasoning,
    resolve_timeout,
)


def test_no_labels_returns_default():
    assert resolve_cli(None) == DEFAULT_CLI
    assert resolve_cli([]) == DEFAULT_CLI


def test_labels_without_cli_prefix_return_default():
    assert resolve_cli(["Bug", "Feature", "Coding Agent"]) == DEFAULT_CLI


@pytest.mark.parametrize("name", sorted(CLI_REGISTRY))
def test_each_registered_cli_resolves(name):
    assert resolve_cli([f"{CLI_LABEL_PREFIX}{name}"]) == name


def test_dict_label_shape_supported():
    """Linear webhook payload sends labels as ``[{name: ...}, ...]``."""
    labels = [{"id": "abc", "name": "cli:forge"}, {"id": "def", "name": "Feature"}]
    assert resolve_cli(labels) == "forge"


def test_unknown_cli_falls_back_to_default(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_cli(["cli:nonexistent"]) == DEFAULT_CLI
    assert any("unknown cli" in r.message for r in caplog.records)


def test_multiple_cli_labels_pick_alphabetical_first(caplog):
    """Deterministic when several cli:* labels are attached by accident."""
    with caplog.at_level("WARNING"):
        chosen = resolve_cli(["cli:opencode", "cli:forge", "cli:claude"])
    assert chosen == "claude"  # 'cli:claude' < 'cli:forge' < 'cli:opencode'
    assert any("multiple cli:* labels" in r.message for r in caplog.records)


def test_mixed_string_and_dict_labels():
    labels = ["Feature", {"name": "cli:gemini"}, {"name": "Coding Agent"}]
    assert resolve_cli(labels) == "gemini"


def test_build_argv_appends_prompt_for_each_cli():
    for name in CLI_REGISTRY:
        argv = build_argv(name, "hello world")
        assert argv[-1] == "hello world"
        # bin path may be expanded — check basename matches the registry's first token.
        assert Path(argv[0]).stem.lower() == CLI_REGISTRY[name][0]
        # Static prefix from the registry must appear in order in argv.
        # (build_argv may inject `--model X` from AUTH_REGISTRY defaults
        # in addition to the registry prefix — so argv is a superset.)
        prefix = CLI_REGISTRY[name][1:]
        assert argv[1:1 + len(prefix)] == prefix


def test_build_argv_unknown_cli_raises():
    with pytest.raises(ValueError, match="unknown cli"):
        build_argv("cursor", "hi")


def test_resolve_bin_prefers_opencode_exe_over_windows_cmd_shim(monkeypatch, tmp_path):
    shim = tmp_path / "opencode.cmd"
    shim.write_text("@echo off\n%*\n", encoding="utf-8")
    real = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")

    monkeypatch.setattr("app.cli_registry.shutil.which", lambda name: str(shim) if name == "opencode" else None)

    assert resolve_bin("opencode") == str(real)


def test_forge_in_registry():
    """TES-646: forge added in this iteration — guard against accidental removal."""
    assert "forge" in CLI_REGISTRY
    assert CLI_REGISTRY["forge"][0] == "forge"


# --- resolve_timeout ---------------------------------------------------------

def test_resolve_timeout_no_labels_returns_none():
    assert resolve_timeout(None) is None
    assert resolve_timeout([]) is None


def test_resolve_timeout_no_matching_label_returns_none():
    assert resolve_timeout(["cli:claude", "model:opus"]) is None


def test_resolve_timeout_parses_plain_int():
    assert resolve_timeout(["timeout:1800"]) == 1800


def test_resolve_timeout_parses_with_s_suffix():
    assert resolve_timeout(["timeout:1800s"]) == 1800
    assert resolve_timeout(["timeout:60S"]) == 60


def test_resolve_timeout_dict_label_shape_supported():
    labels = [{"id": "x", "name": "timeout:90"}]
    assert resolve_timeout(labels) == 90


def test_resolve_timeout_invalid_string_returns_none(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_timeout(["timeout:forever"]) is None
    assert "cannot parse" in caplog.text


def test_resolve_timeout_zero_or_negative_returns_none(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_timeout(["timeout:0"]) is None
        assert resolve_timeout(["timeout:-30"]) is None
    assert "non-positive" in caplog.text


def test_resolve_timeout_empty_value_returns_none():
    assert resolve_timeout(["timeout:"]) is None
    assert resolve_timeout(["timeout:s"]) is None


def test_resolve_timeout_multiple_labels_pick_alphabetical_first(caplog):
    with caplog.at_level("WARNING"):
        # alphabetical: "timeout:120" before "timeout:300" (string comparison)
        result = resolve_timeout(["timeout:300", "timeout:120"])
    assert result == 120
    assert "multiple timeout:* labels" in caplog.text


def test_resolve_timeout_caller_responsible_for_clamping():
    """resolve_timeout itself does not clamp — that's the orchestrator's job
    (the helper there compares against TIMEOUT_HARD_CAP_SECONDS)."""
    # Returns the parsed value verbatim even if absurdly high.
    assert resolve_timeout(["timeout:99999"]) == 99999


# --- resolve_context_mode ----------------------------------------------------


def test_resolve_context_mode_defaults_to_fresh():
    assert resolve_context_mode(None) == "fresh"
    assert resolve_context_mode([]) == "fresh"


def test_resolve_context_mode_parses_fresh_and_fork():
    assert resolve_context_mode(["context:fresh"]) == "fresh"
    assert resolve_context_mode([{"name": "context:fork"}]) == "fork"


def test_resolve_context_mode_unsupported_falls_back_to_fresh(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_context_mode(["context:resume"]) == "fresh"
    assert "unsupported context mode" in caplog.text


def test_resolve_context_mode_multiple_labels_pick_alphabetical_first(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_context_mode(["context:fresh", "context:fork"]) == "fork"
    assert "multiple context:* labels" in caplog.text


# --- resolve_reasoning -------------------------------------------------------


def test_resolve_reasoning_no_labels_returns_none():
    assert resolve_reasoning(None) is None
    assert resolve_reasoning([]) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("reasoning:minimal", "minimal"),
        ("reasoning:low", "low"),
        ("reasoning:medium", "medium"),
        ("reasoning:high", "high"),
        ("reasoning:X-High", "x-high"),
        ("reasoning:xhigh", "x-high"),
        ("reasoning:x_high", "x-high"),
        ("reasoning:default", "default"),
    ],
)
def test_resolve_reasoning_normalizes_supported_values(label, expected):
    assert resolve_reasoning([label]) == expected


def test_resolve_reasoning_dict_label_shape_supported():
    assert resolve_reasoning([{"name": "reasoning:high"}]) == "high"


def test_resolve_reasoning_invalid_value_raises():
    with pytest.raises(ValueError, match="unsupported reasoning level"):
        resolve_reasoning(["reasoning:turbo"])


def test_resolve_reasoning_multiple_labels_pick_alphabetical_first(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_reasoning(["reasoning:medium", "reasoning:high"]) == "high"
    assert "multiple reasoning:* labels" in caplog.text


# ---------------------------------------------------------------------------
# Auth resolution (TES-716 follow-up — multi-CLI auth)
# ---------------------------------------------------------------------------


def test_resolve_auth_default_is_oauth_for_known_clis(monkeypatch):
    from app.cli_registry import resolve_auth
    # No labels, no env override → AuthSpec.default_mode == "oauth"
    for cli in ("claude", "codex", "gemini", "opencode", "forge"):
        monkeypatch.delenv(f"LINEAR_EXECUTOR_{cli.upper()}_AUTH", raising=False)
        assert resolve_auth([], cli) == "oauth"


def test_resolve_auth_label_apikey_overrides_default():
    from app.cli_registry import resolve_auth
    assert resolve_auth(["auth:apikey"], "claude") == "apikey"
    assert resolve_auth([{"name": "auth:apikey"}], "opencode") == "apikey"


def test_resolve_auth_label_oauth_explicit():
    from app.cli_registry import resolve_auth
    assert resolve_auth(["auth:oauth"], "claude") == "oauth"


def test_resolve_auth_unknown_label_falls_through(monkeypatch):
    from app.cli_registry import resolve_auth
    monkeypatch.setenv("LINEAR_EXECUTOR_CLAUDE_AUTH", "apikey")
    # Garbage label → ignored, env wins
    assert resolve_auth(["auth:dwim"], "claude") == "apikey"


def test_resolve_auth_env_override_per_cli(monkeypatch):
    from app.cli_registry import resolve_auth
    monkeypatch.setenv("LINEAR_EXECUTOR_OPENCODE_AUTH", "apikey")
    monkeypatch.delenv("LINEAR_EXECUTOR_CLAUDE_AUTH", raising=False)
    assert resolve_auth([], "opencode") == "apikey"
    assert resolve_auth([], "claude") == "oauth"  # other CLI unaffected


def test_resolve_auth_label_beats_env(monkeypatch):
    from app.cli_registry import resolve_auth
    monkeypatch.setenv("LINEAR_EXECUTOR_CLAUDE_AUTH", "apikey")
    assert resolve_auth(["auth:oauth"], "claude") == "oauth"


def test_resolve_auth_invalid_env_falls_through(monkeypatch):
    from app.cli_registry import resolve_auth
    monkeypatch.setenv("LINEAR_EXECUTOR_CLAUDE_AUTH", "garbage")
    assert resolve_auth([], "claude") == "oauth"  # falls through to spec default


def test_build_argv_opencode_oauth_picks_go_plan_model():
    argv = build_argv("opencode", "hi", auth_mode="oauth")
    assert "--pure" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == DEFAULT_OPENCODE_MODEL


def test_build_argv_opencode_apikey_picks_provider_model():
    argv = build_argv("opencode", "hi", auth_mode="apikey")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "openrouter/anthropic/claude-haiku-4.5"


def test_build_argv_explicit_model_wins_over_auth_default():
    argv = build_argv("opencode", "hi", model="custom/model", auth_mode="oauth")
    assert argv[argv.index("--model") + 1] == "custom/model"


def test_build_argv_opencode_adds_reasoning_variant():
    argv = build_argv("opencode", "hi", reasoning="low", auth_mode="oauth")
    assert "--variant" in argv
    assert argv[argv.index("--variant") + 1] == "low"


def test_build_argv_default_reasoning_omits_variant():
    argv = build_argv("opencode", "hi", reasoning="default", auth_mode="oauth")
    assert "--variant" not in argv


def test_build_argv_non_opencode_ignores_reasoning(caplog):
    with caplog.at_level("WARNING"):
        argv = build_argv("claude", "hi", reasoning="high", auth_mode="oauth")
    assert "--variant" not in argv
    assert "reasoning:high ignored" in caplog.text


def test_build_argv_claude_no_auto_model_either_mode():
    # Claude has no default model in its AuthSpec — Max-Plan picks tier itself.
    argv_oauth = build_argv("claude", "hi", auth_mode="oauth")
    argv_apikey = build_argv("claude", "hi", auth_mode="apikey")
    assert "--model" not in argv_oauth
    assert "--model" not in argv_apikey


def test_build_argv_claude_print_mode_contract():
    argv = build_argv("claude", "hi", auth_mode="oauth")
    assert argv[1:3] == ["--print", "--dangerously-skip-permissions"]
    assert "--no-session-persistence" in argv


def test_build_argv_codex_exec_mode_contract():
    argv = build_argv("codex", "hi", auth_mode="oauth")
    assert argv[1:3] == ["exec", "--dangerously-bypass-approvals-and-sandbox"]
    assert argv[-1] == "hi"
