"""Resolve the cwd that Claude Code should run in for a given Linear ticket.

Priority order:
1. Explicit override in the ticket description: ``folder: <path>``
2. Linear-project-to-folder mapping (PROJECT_FOLDERS, empty by default — see
   below)
3. Fallback: ``<LINEAR_EXECUTOR_TICKETS_BASE>/<identifier>/`` — a fresh folder
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
