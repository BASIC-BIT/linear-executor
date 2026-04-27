from pathlib import Path

from app.folders import FALLBACK_BASE, PROJECT_FOLDERS, resolve_folder


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def test_override_in_description_wins():
    data = {
        "identifier": "TES-100",
        "description": "Normal text\n\nfolder: ~/tmp/special-folder\n\nmore text",
        "project": {"name": "Linear-Executor"},  # would otherwise map
    }
    r = resolve_folder(data)
    assert r.strategy == "override"
    assert r.path == _expand("~/tmp/special-folder")


def test_linear_project_maps_to_known_folder():
    data = {
        "identifier": "TES-200",
        "description": "No override here",
        "project": {"name": "Radio MCP"},
    }
    r = resolve_folder(data)
    assert r.strategy == "mapped"
    assert r.path == _expand("~/cc-dev/radio-mcp/")


def test_infra_project_maps_to_cc_dev_root():
    data = {
        "identifier": "TES-300",
        "description": "",
        "project": {"name": "Infra & AI Harnessing"},
    }
    r = resolve_folder(data)
    assert r.strategy == "mapped"
    assert r.path == _expand("~/cc-dev/")


def test_fallback_for_unknown_project():
    data = {
        "identifier": "TES-400",
        "description": "no override",
        "project": {"name": "Some Brand New Project"},
    }
    r = resolve_folder(data)
    assert r.strategy == "fallback"
    assert r.path == _expand(f"{FALLBACK_BASE}TES-400/")


def test_fallback_when_no_project():
    data = {"identifier": "TES-500", "description": ""}
    r = resolve_folder(data)
    assert r.strategy == "fallback"
    assert r.path == _expand(f"{FALLBACK_BASE}TES-500/")


def test_override_ignores_leading_whitespace_and_extra_blank_lines():
    data = {
        "identifier": "TES-600",
        "description": "Some\n\n\n  folder: /absolute/path  \n\n",
        "project": {"name": "HypeType"},
    }
    r = resolve_folder(data)
    assert r.strategy == "override"
    assert r.path == _expand("/absolute/path")


def test_all_known_mapping_entries_resolve():
    for name in PROJECT_FOLDERS:
        data = {"identifier": "TES-999", "project": {"name": name}}
        r = resolve_folder(data)
        assert r.strategy == "mapped"
        assert r.path == _expand(PROJECT_FOLDERS[name])
