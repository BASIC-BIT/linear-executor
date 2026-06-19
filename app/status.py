"""Read-only dashboard status contract for queue/operator views."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import queue as q
from app.cli_registry import resolve_auth, resolve_cli, resolve_model
from app.folders import resolve_folder


DASHBOARD_SCHEMA_VERSION = "dashboard_v1"
STALE_RUNNING_SECONDS = 60 * 60
RECENT_FAILURE_SECONDS = 24 * 60 * 60
_ERROR_MAX_CHARS = 240
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*(?:bearer\s+)?\S+|bearer\s+\S+"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _elapsed_seconds(started_at: str | None, now: datetime) -> int | None:
    started = _parse_db_time(started_at)
    if started is None:
        return None
    return max(0, int((now - started).total_seconds()))


def _age_seconds(created_at: str, now: datetime) -> int | None:
    created = _parse_db_time(created_at)
    if created is None:
        return None
    return max(0, int((now - created).total_seconds()))


def _redact_error(error: str | None) -> str | None:
    if not error:
        return None
    compact = " ".join(error.split())

    def replacement(match: re.Match[str]) -> str:
        key = match.group(1)
        return f"{key}=<redacted>" if key else "bearer <redacted>"

    redacted = _SECRET_RE.sub(replacement, compact)
    if len(redacted) > _ERROR_MAX_CHARS:
        return redacted[: _ERROR_MAX_CHARS - 3].rstrip() + "..."
    return redacted


def _data(job: q.Job) -> dict[str, Any]:
    try:
        payload = job.payload
    except Exception:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _project_name(data: dict[str, Any]) -> str | None:
    project = data.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        return project["name"]
    return None


def _labels(data: dict[str, Any]) -> Any:
    labels = data.get("labels")
    return labels if isinstance(labels, list) else []


def _lane(kind: str) -> str:
    return "express" if kind == "proxy" else "general"


def _folder_resolution(data: dict[str, Any]) -> dict[str, Any]:
    try:
        resolution = resolve_folder(data)
    except Exception as exc:
        return {
            "status": "error",
            "error_summary": _redact_error(str(exc)),
        }
    path = resolution.path
    parts = path.parts[-2:]
    return {
        "status": "resolved",
        "strategy": resolution.strategy,
        "folder_name": path.name,
        "path_tail": str(Path(*parts)) if parts else path.name,
        "exists": path.exists(),
    }


def _backend(data: dict[str, Any]) -> dict[str, Any]:
    labels = _labels(data)
    cli = resolve_cli(labels)
    return {
        "cli": cli,
        "auth_mode": resolve_auth(labels, cli),
        "model": resolve_model(labels),
    }


def _job_base(job: q.Job, now: datetime) -> dict[str, Any]:
    data = _data(job)
    return {
        "job_id": job.id,
        "issue_key": job.identifier,
        "issue_uuid": job.ticket_id,
        "title": data.get("title") if isinstance(data.get("title"), str) else None,
        "project": _project_name(data),
        "repo": _folder_resolution(data),
        "backend": _backend(data),
        "kind": job.kind,
        "lane": _lane(job.kind),
        "status": job.status,
        "created_at": _iso(_parse_db_time(job.created_at)),
        "started_at": _iso(_parse_db_time(job.started_at)),
        "retries": job.retries,
        "max_retries": job.max_retries,
        "error_summary": _redact_error(job.last_error),
    }


def _active_job(job: q.Job, now: datetime) -> dict[str, Any]:
    item = _job_base(job, now)
    item["elapsed_seconds"] = _elapsed_seconds(job.started_at, now)
    return item


def _pending_classification(job: q.Job, folder: dict[str, Any]) -> str:
    if folder.get("status") == "error":
        return "blocked"
    if job.last_error or job.retries:
        return "retry_after_failure"
    return "ready"


def _pending_job(job: q.Job, now: datetime) -> dict[str, Any]:
    item = _job_base(job, now)
    item["queue_age_seconds"] = _age_seconds(job.created_at, now)
    item["classification"] = _pending_classification(job, item["repo"])
    return item


def _counts(jobs: list[q.Job]) -> dict[str, int]:
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return counts


def _health(jobs: list[q.Job], now: datetime) -> dict[str, Any]:
    pending = [job for job in jobs if job.status == "pending"]
    running = [job for job in jobs if job.status == "running"]
    failed = [job for job in jobs if job.status == "failed"]
    stale_running = [
        job for job in running
        if (_elapsed_seconds(job.started_at, now) or 0) >= STALE_RUNNING_SECONDS
    ]
    recent_failures = [
        job for job in failed
        if (completed := _parse_db_time(job.completed_at)) is not None
        and (now - completed).total_seconds() <= RECENT_FAILURE_SECONDS
    ]
    queue_ages = [age for job in pending if (age := _age_seconds(job.created_at, now)) is not None]
    return {
        "executor": {"status": "ok"},
        "stale_running_jobs": {
            "threshold_seconds": STALE_RUNNING_SECONDS,
            "count": len(stale_running),
            "job_ids": [job.id for job in stale_running],
        },
        "recent_failures": {
            "window_seconds": RECENT_FAILURE_SECONDS,
            "count": len(recent_failures),
            "job_ids": [job.id for job in recent_failures],
        },
        "queue_age": {
            "oldest_pending_seconds": max(queue_ages) if queue_ages else None,
            "pending_count": len(pending),
        },
        "artifact_filter_warnings": None,
    }


def build_status(db_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the stable, read-only dashboard status JSON."""
    current = (now or _utc_now()).astimezone(UTC)
    jobs = q.list_jobs(db_path)
    running = [job for job in jobs if job.status == "running"]
    pending = [job for job in jobs if job.status == "pending"]
    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "generated_at": _iso(current),
        "status": "ok",
        "counts": _counts(jobs),
        "active_jobs": [_active_job(job, current) for job in running],
        "pending_queue": [_pending_job(job, current) for job in pending],
        "health": _health(jobs, current),
    }
