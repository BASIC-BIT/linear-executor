"""Resolve the cwd that Claude Code should run in for a given Linear ticket.

Priority order:
1. Explicit override in the ticket description: ``folder: <path>``
2. Linear-project-to-folder mapping (kept in sync with ~/.claude/rules/linear-projects.md)
3. Fallback: ``~/cc-dev/tickets/<identifier>/`` — a fresh folder per ticket.

Returns both the resolved path (expanded to an absolute path) and the strategy
that produced it, so the webhook handler can log / comment why this folder was
chosen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Synced manually from ~/.claude/rules/linear-projects.md (2026-04-18).
# Update both when a new Linear project appears.
PROJECT_FOLDERS: dict[str, str] = {
    "Infra & AI Harnessing": "~/cc-dev/",
    "Linear-Executor": "~/cc-dev/linear-executor/",
    "Data Dashboard": "~/cc-dev/data-dashboard/",
    "HypeType": "~/cc-dev/archetype-assessment/",
    "The Etruscan Project": "~/cc-dev/the_etruscan_project/",
    "Radio MCP": "~/cc-dev/radio-mcp/",
    "ClaudeClaw": "~/cc-claudeclaw/",
}


FALLBACK_BASE = "~/cc-dev/tickets/"


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
