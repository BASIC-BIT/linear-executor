"""Resolve the cwd that Claude Code should run in for a given Linear ticket.

Priority order:
1. Explicit override in the ticket description: ``folder: <path>``
2. Local product-map selection via ``product:<id>`` or ``repo:<id>`` labels.
3. Linear-project-to-folder mapping (PROJECT_FOLDERS, empty by default — see
    below)
4. Fallback: ``<LINEAR_EXECUTOR_TICKETS_BASE>/<identifier>/`` — a fresh folder
   per ticket. Default base is ``~/.linear-executor/tickets/``; override via
   the ``LINEAR_EXECUTOR_TICKETS_BASE`` env var.

Returns both the resolved path (expanded to an absolute path) and the strategy
that produced it, so the webhook handler can log / comment why this folder was
chosen.

PROJECT_FOLDERS is empty by default — every ticket gets its own folder under
the fallback base. To pre-map specific Linear projects to existing repos on
disk (so a ticket in project "ACME-Backend" runs in ``~/code/acme-backend/``),
edit this dict in your fork or set entries at runtime. The ``folder:`` override
in the ticket description always takes precedence.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


# Map Linear project names to local folders. Empty by default — populate in
# your fork / deployment if you want auto-routing of project-X tickets to
# repo-Y. The ``folder: <path>`` override in a ticket description always wins.
PROJECT_FOLDERS: dict[str, str] = {}


# Base directory for per-ticket folders when no override and no mapping match.
# Override via LINEAR_EXECUTOR_TICKETS_BASE in .env. Trailing slash is added if
# missing so ``f"{FALLBACK_BASE}{identifier}/"`` always produces a clean path.
_FALLBACK_BASE_RAW = os.environ.get(
    "LINEAR_EXECUTOR_TICKETS_BASE",
    "~/.linear-executor/tickets/",
).strip()
FALLBACK_BASE = _FALLBACK_BASE_RAW if _FALLBACK_BASE_RAW.endswith("/") else _FALLBACK_BASE_RAW + "/"


_OVERRIDE_RE = re.compile(r"^\s*folder\s*:\s*(?P<path>\S+)\s*$", re.MULTILINE)
_PRODUCT_RE = re.compile(r"^\s*(?:product|repo)\s*:\s*(?P<id>[A-Za-z0-9_.-]+)\s*$", re.MULTILINE)
PRODUCT_MAP_ENV = "LINEAR_CONTROLLER_PRODUCT_MAP"
PRODUCT_LABEL_PREFIXES = ("product:", "repo:")


@dataclass(frozen=True)
class FolderResolution:
    path: Path
    strategy: str  # "override" | "mapped" | "fallback"
    detail: str    # free-form string used in logs / Linear comments


def _expand(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _parse_override(description: str | None) -> str | None:
    if not description:
        return None
    match = _OVERRIDE_RE.search(description)
    return match.group("path") if match else None


def _label_names(labels) -> list[str]:
    if not labels:
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _parse_product_id(data: dict) -> tuple[str, str] | None:
    label_matches = sorted(
        name for name in _label_names(data.get("labels"))
        if any(name.startswith(prefix) for prefix in PRODUCT_LABEL_PREFIXES)
    )
    if label_matches:
        chosen = label_matches[0]
        for prefix in PRODUCT_LABEL_PREFIXES:
            if chosen.startswith(prefix):
                return chosen[len(prefix):].strip(), f"label {chosen!r}"

    description = data.get("description")
    if description:
        match = _PRODUCT_RE.search(description)
        if match:
            return match.group("id"), "ticket description product/repo override"
    return None


def _load_product_map() -> tuple[Path, dict]:
    raw_path = os.environ.get(PRODUCT_MAP_ENV, "").strip()
    if not raw_path:
        raise ValueError(
            f"product/repo label was set, but {PRODUCT_MAP_ENV} is not configured"
        )
    path = Path(raw_path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"product map not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"product map is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"product map root must be an object: {path}")
    return path, data


def _resolve_product_root(product_id: str) -> tuple[str, Path, Path]:
    path, data = _load_product_map()
    products = data.get("products")
    if not isinstance(products, dict):
        raise ValueError(f"product map must contain a products object: {path}")
    entry = products.get(product_id)
    if entry is None:
        known = ", ".join(sorted(str(k) for k in products.keys())) or "none"
        raise ValueError(f"unknown product/repo {product_id!r} in {path}; known: {known}")
    if isinstance(entry, str):
        target = entry
    elif isinstance(entry, dict):
        target = entry.get("targetRoot") or entry.get("contextRoot") or entry.get("root")
    else:
        target = None
    if not isinstance(target, str) or not target.strip():
        raise ValueError(
            f"product/repo {product_id!r} in {path} must define targetRoot"
        )
    return product_id, _expand(target), path


def resolve_folder(data: dict) -> FolderResolution:
    """Decide which folder Claude Code should run in for this issue payload.

    ``data`` is ``payload["data"]`` from a Linear webhook: contains ``identifier``,
    ``description``, and ``project.name`` (when the ticket belongs to a project).
    """
    identifier = data.get("identifier", "unknown")

    override = _parse_override(data.get("description"))
    if override:
        return FolderResolution(
            path=_expand(override),
            strategy="override",
            detail=f"ticket description override: folder: {override}",
        )

    product = _parse_product_id(data)
    if product:
        product_id, source = product
        product_id, target, map_path = _resolve_product_root(product_id)
        return FolderResolution(
            path=target,
            strategy="product-map",
            detail=f"mapped from {source} via {map_path}: {product_id} -> {target}",
        )

    project = data.get("project") or {}
    project_name = project.get("name")
    if project_name and project_name in PROJECT_FOLDERS:
        mapped = PROJECT_FOLDERS[project_name]
        return FolderResolution(
            path=_expand(mapped),
            strategy="mapped",
            detail=f"mapped from Linear project '{project_name}' → {mapped}",
        )

    fallback = f"{FALLBACK_BASE}{identifier}/"
    return FolderResolution(
        path=_expand(fallback),
        strategy="fallback",
        detail=(
            f"no mapping for project '{project_name}' and no folder: override — "
            f"using per-ticket folder {fallback}"
        ),
    )
