from pathlib import Path
import json

import pytest

from app import folders
from app.folders import FALLBACK_BASE, PROJECT_FOLDERS, resolve_folder


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def test_override_in_description_wins():
    data = {
        "identifier": "TES-100",
        "description": "Normal text\n\nfolder: ~/tmp/special-folder\n\nmore text",
        "project": {"name": "Some-Project"},  # would otherwise map if present
    }
    r = resolve_folder(data)
    assert r.strategy == "override"
    assert r.path == _expand("~/tmp/special-folder")


def _write_product_map(tmp_path, products: dict) -> Path:
    path = tmp_path / "product-map.local.json"
    path.write_text(json.dumps({"version": 1, "products": products}), encoding="utf-8")
    return path


def test_product_label_maps_to_target_root(monkeypatch, tmp_path):
    target = tmp_path / "linear-executor-wt" / "windows-compat"
    product_map = _write_product_map(
        tmp_path,
        {"linear-executor": {"targetRoot": str(target)}},
    )
    monkeypatch.setenv("LINEAR_CONTROLLER_PRODUCT_MAP", str(product_map))

    r = resolve_folder({
        "identifier": "TES-PROD",
        "description": "No folder override",
        "labels": [{"name": "product:linear-executor"}],
    })

    assert r.strategy == "product-map"
    assert r.path == target.resolve()
    assert "linear-executor" in r.detail


def test_repo_label_maps_to_string_product_entry(monkeypatch, tmp_path):
    target = tmp_path / "basic-infra-wt" / "cloudflare"
    product_map = _write_product_map(tmp_path, {"basic-infra": str(target)})
    monkeypatch.setenv("LINEAR_CONTROLLER_PRODUCT_MAP", str(product_map))

    r = resolve_folder({
        "identifier": "TES-REPO",
        "description": "No folder override",
        "labels": [{"name": "repo:basic-infra"}],
    })

    assert r.strategy == "product-map"
    assert r.path == target.resolve()


def test_folder_override_beats_product_label(monkeypatch, tmp_path):
    product_map = _write_product_map(
        tmp_path,
        {"linear-executor": {"targetRoot": str(tmp_path / "mapped")}},
    )
    monkeypatch.setenv("LINEAR_CONTROLLER_PRODUCT_MAP", str(product_map))

    r = resolve_folder({
        "identifier": "TES-OVERRIDE-PROD",
        "description": f"folder: {tmp_path / 'override'}",
        "labels": [{"name": "product:linear-executor"}],
    })

    assert r.strategy == "override"
    assert r.path == (tmp_path / "override").resolve()


def test_product_label_without_map_fails_clearly(monkeypatch):
    monkeypatch.delenv("LINEAR_CONTROLLER_PRODUCT_MAP", raising=False)

    with pytest.raises(ValueError, match="LINEAR_CONTROLLER_PRODUCT_MAP"):
        resolve_folder({
            "identifier": "TES-NO-MAP",
            "description": "No folder override",
            "labels": [{"name": "product:linear-executor"}],
        })


def test_linear_project_maps_to_known_folder(monkeypatch):
    """Pre-populated mapping wins over fallback when present."""
    monkeypatch.setitem(PROJECT_FOLDERS, "ACME-Backend", "~/code/acme-backend/")
    data = {
        "identifier": "TES-200",
        "description": "No override here",
        "project": {"name": "ACME-Backend"},
    }
    r = resolve_folder(data)
    assert r.strategy == "mapped"
    assert r.path == _expand("~/code/acme-backend/")


def test_mapping_works_for_root_folder(monkeypatch):
    monkeypatch.setitem(PROJECT_FOLDERS, "Workspace-Root", "~/code/")
    data = {
        "identifier": "TES-300",
        "description": "",
        "project": {"name": "Workspace-Root"},
    }
    r = resolve_folder(data)
    assert r.strategy == "mapped"
    assert r.path == _expand("~/code/")


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


def test_override_ignores_leading_whitespace_and_extra_blank_lines(monkeypatch):
    """Override beats project mapping even if mapping is populated."""
    monkeypatch.setitem(PROJECT_FOLDERS, "HypeType", "~/code/hypetype/")
    data = {
        "identifier": "TES-600",
        "description": "Some\n\n\n  folder: /absolute/path  \n\n",
        "project": {"name": "HypeType"},
    }
    r = resolve_folder(data)
    assert r.strategy == "override"
    assert r.path == _expand("/absolute/path")


def test_default_project_folders_is_empty():
    """Out-of-the-box installs should ship without any auto-mapping so every
    new install routes everything to the per-ticket fallback."""
    assert folders.PROJECT_FOLDERS == {}


def test_fallback_base_does_not_assume_cc_dev():
    """Out-of-the-box default should not embed any user-specific path
    (`cc-dev/`, `Bastian`, etc.). The current value will reflect the test-env
    setting if present — the regression we guard against is the default
    growing back into a personal workspace path."""
    assert "cc-dev" not in folders.FALLBACK_BASE
    assert "claudeclaw" not in folders.FALLBACK_BASE.lower()
