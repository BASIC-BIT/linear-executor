"""Unit tests for the cli_registry router (TES-646)."""
from __future__ import annotations

import pytest

from app.cli_registry import (
    CLI_LABEL_PREFIX,
    CLI_REGISTRY,
    DEFAULT_CLI,
    build_argv,
    resolve_cli,
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
        assert argv[0].split("/")[-1] == CLI_REGISTRY[name][0]
        # static prefix preserved
        assert argv[1:-1] == CLI_REGISTRY[name][1:]


def test_build_argv_unknown_cli_raises():
    with pytest.raises(ValueError, match="unknown cli"):
        build_argv("cursor", "hi")


def test_forge_in_registry():
    """TES-646: forge added in this iteration — guard against accidental removal."""
    assert "forge" in CLI_REGISTRY
    assert CLI_REGISTRY["forge"][0] == "forge"
