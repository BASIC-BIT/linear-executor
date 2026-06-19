from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import queue as q
from app.cli_registry import resolve_auth, resolve_cli, resolve_model
from app.folders import resolve_folder


STATUSES = ("pending", "running", "done", "failed", "cancelled")
DEFAULT_HEALTH_URL = "http://127.0.0.1:8123/health"
PROCESS_KEYWORDS = ("opencode", "codex", "claude")
DASHBOARD_SCHEMA_VERSION = "dashboard_v1"
STALE_RUNNING_SECONDS = 60 * 60
RECENT_FAILURE_SECONDS = 24 * 60 * 60
_ERROR_MAX_CHARS = 240
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*(?:bearer\s+)?\S+|bearer\s+\S+"
)


def default_db_path() -> Path:
    override = os.getenv("LINEAR_EXECUTOR_DB")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "state" / "jobs.db"


def _parse_datetime(value: str | None) -> datetime | None:
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


def format_elapsed(started_at: str | None, *, now: datetime | None = None) -> str | None:
    started = _parse_datetime(started_at)
    if started is None:
        return None
    current = now or datetime.now(UTC)
    seconds = max(0, int((current - started).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def summarize_error(error: str | None, *, limit: int = 160) -> str | None:
    redacted = _redact_error(error)
    if not redacted:
        return None
    if len(redacted) <= limit:
        return redacted
    return redacted[: limit - 1] + "..."


def _job_to_dict(row: sqlite3.Row, *, now: datetime | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticket_id": row["ticket_id"],
        "issue": row["identifier"],
        "kind": row["kind"],
        "status": row["status"],
        "retries": row["retries"],
        "max_retries": row["max_retries"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "elapsed": format_elapsed(row["started_at"], now=now),
        "last_error": summarize_error(row["last_error"]),
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _issue_where(issue: str | None) -> tuple[str, list[str]]:
    if not issue:
        return "", []
    return " AND (identifier = ? OR ticket_id = ?)", [issue, issue]


def read_queue_status(
    db_path: Path,
    *,
    issue: str | None = None,
    running_only: bool = False,
    recent: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "database": str(db_path),
        "database_exists": db_path.exists(),
        "queue": {status: 0 for status in STATUSES},
        "running": [],
        "pending": [],
        "recent_failures": [],
    }
    if not db_path.exists():
        result["database_error"] = "database file does not exist"
        return result

    try:
        with _connect_readonly(db_path) as conn:
            where, params = _issue_where(issue)
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS count FROM jobs WHERE 1=1{where} GROUP BY status",
                params,
            ).fetchall()
            counts = Counter({row["status"]: row["count"] for row in rows})
            result["queue"] = {status: int(counts.get(status, 0)) for status in STATUSES}

            running_rows = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'running'{where}
                ORDER BY started_at ASC, id ASC
                """,
                params,
            ).fetchall()
            result["running"] = [_job_to_dict(row, now=now) for row in running_rows]

            if not running_only:
                pending_rows = conn.execute(
                    f"""
                    SELECT * FROM jobs
                    WHERE status = 'pending'{where}
                    ORDER BY created_at ASC, id ASC
                    """,
                    params,
                ).fetchall()
                result["pending"] = [_job_to_dict(row, now=now) for row in pending_rows]

            failure_rows = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'failed'{where}
                ORDER BY completed_at DESC, id DESC
                LIMIT ?
                """,
                [*params, max(0, recent)],
            ).fetchall()
            result["recent_failures"] = [_job_to_dict(row, now=now) for row in failure_rows]
    except sqlite3.Error as exc:
        result["database_error"] = str(exc)
    return result


def check_health(health_url: str, *, timeout: float = 1.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as response:
            body = response.read(1024).decode("utf-8", errors="replace")
            parsed: Any
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = body
            return {"url": health_url, "ok": 200 <= response.status < 300, "status_code": response.status, "body": parsed}
    except (OSError, urllib.error.URLError) as exc:
        return {"url": health_url, "ok": False, "error": str(exc)}


def list_backend_processes() -> list[dict[str, Any]]:
    if os.name == "nt":
        return _list_windows_processes()
    return _list_posix_processes()


def _matches_backend(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in PROCESS_KEYWORDS)


def _list_windows_processes() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    processes: list[dict[str, Any]] = []
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) < 5:
            continue
        image, pid, session_name, _session_number, memory = row[:5]
        if not _matches_backend(image):
            continue
        processes.append(
            {
                "pid": pid,
                "name": image,
                "session": session_name,
                "memory": memory,
            }
        )
    return processes


def _list_posix_processes() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    processes: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not _matches_backend(line):
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        processes.append({"pid": parts[0], "name": parts[1], "command": parts[2] if len(parts) > 2 else ""})
    return processes


def build_status(
    db_path: Path,
    *,
    issue: str | None = None,
    running_only: bool = False,
    recent: int = 5,
    health_url: str | None = DEFAULT_HEALTH_URL,
    include_processes: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    data = read_queue_status(db_path, issue=issue, running_only=running_only, recent=recent, now=now)
    data["filters"] = {"issue": issue, "running_only": running_only, "recent": recent}
    data["health"] = check_health(health_url) if health_url else None
    data["backend_processes"] = list_backend_processes() if include_processes else []
    return data


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    values = [[str(value or "") for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    header_line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    separator = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in values]
    return "\n".join([header_line, separator, *body])


def _job_rows(
    jobs: list[dict[str, Any]],
    *,
    timestamp_field: str,
    include_error: bool = False,
) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for job in jobs:
        row: list[Any] = [
            job["id"],
            job["issue"],
            job["kind"],
            job.get(timestamp_field) or "",
            job["elapsed"] or "",
        ]
        if include_error:
            row.append(job["last_error"] or "")
        rows.append(row)
    return rows


def format_human(data: dict[str, Any]) -> str:
    lines = [f"Database: {data['database']}"]
    if data.get("database_error"):
        lines.append(f"Database error: {data['database_error']}")

    queue = data["queue"]
    lines.append(
        "Queue: "
        + ", ".join(f"{status}={queue.get(status, 0)}" for status in STATUSES)
    )

    health = data.get("health")
    if health:
        if health.get("ok"):
            lines.append(f"Health: ok ({health['url']})")
        else:
            lines.append(f"Health: unavailable ({health['url']}) {health.get('error', '')}".rstrip())

    processes = data.get("backend_processes") or []
    if processes:
        lines.append(f"Backend processes: {len(processes)}")
        lines.append(_table(["pid", "name", "detail"], [[p.get("pid"), p.get("name"), p.get("session") or p.get("command") or ""] for p in processes]))
    else:
        lines.append("Backend processes: none detected")

    lines.append("")
    lines.append("Running jobs")
    running = data.get("running") or []
    lines.append(
        _table(["id", "issue", "kind", "started_at", "elapsed"], _job_rows(running, timestamp_field="started_at"))
        if running
        else "(none)"
    )

    if not data.get("filters", {}).get("running_only"):
        lines.append("")
        lines.append("Pending jobs")
        pending = data.get("pending") or []
        lines.append(
            _table(["id", "issue", "kind", "created_at", "elapsed"], _job_rows(pending, timestamp_field="created_at"))
            if pending
            else "(none)"
        )

    lines.append("")
    lines.append("Recent failures")
    failures = data.get("recent_failures") or []
    lines.append(
        _table(
            ["id", "issue", "kind", "completed_at", "elapsed", "last_error"],
            _job_rows(failures, timestamp_field="completed_at", include_error=True),
        )
        if failures
        else "(none)"
    )
    return "\n".join(lines)


def _elapsed_seconds(started_at: str | None, now: datetime) -> int | None:
    started = _parse_datetime(started_at)
    if started is None:
        return None
    return max(0, int((now - started).total_seconds()))


def _age_seconds(created_at: str, now: datetime) -> int | None:
    created = _parse_datetime(created_at)
    if created is None:
        return None
    return max(0, int((now - created).total_seconds()))


def _job_payload_data(job: q.Job) -> dict[str, Any]:
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
    data = _job_payload_data(job)
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
        "created_at": _iso(_parse_datetime(job.created_at)),
        "started_at": _iso(_parse_datetime(job.started_at)),
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
    counts = {status: 0 for status in STATUSES}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return counts


def _dashboard_health(jobs: list[q.Job], now: datetime) -> dict[str, Any]:
    pending = [job for job in jobs if job.status == "pending"]
    running = [job for job in jobs if job.status == "running"]
    failed = [job for job in jobs if job.status == "failed"]
    stale_running = [
        job for job in running
        if (_elapsed_seconds(job.started_at, now) or 0) >= STALE_RUNNING_SECONDS
    ]
    recent_failures = [
        job for job in failed
        if (completed := _parse_datetime(job.completed_at)) is not None
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


def build_dashboard_status(db_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
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
        "health": _dashboard_health(jobs, current),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show linear-executor queue and worker status.")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="Path to jobs.db")
    parser.add_argument("--json", action="store_true", help="Emit stable JSON for automation")
    parser.add_argument("--issue", help="Filter by Linear issue key or ticket UUID")
    parser.add_argument("--running", action="store_true", help="Only list running jobs, plus queue summary and recent failures")
    parser.add_argument("--recent", type=int, default=5, help="Number of recent failures to include")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="Local health endpoint to probe")
    parser.add_argument("--no-health", action="store_true", help="Skip local health probe")
    parser.add_argument("--no-processes", action="store_true", help="Skip backend process discovery")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data = build_status(
        args.db,
        issue=args.issue,
        running_only=args.running,
        recent=args.recent,
        health_url=None if args.no_health else args.health_url,
        include_processes=not args.no_processes,
    )
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(format_human(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
