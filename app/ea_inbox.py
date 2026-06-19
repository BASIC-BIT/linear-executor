from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Match app.main's env loading so the manual CLI can use the same local Linear
# credentials as the webhook server without requiring the operator to export them.
SHARED_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if SHARED_ENV_PATH.exists():
    load_dotenv(dotenv_path=SHARED_ENV_PATH, override=False, encoding="utf-8-sig")
load_dotenv(dotenv_path=ENV_PATH, override=True, encoding="utf-8-sig")

from app import linear_api
from app import queue as q
from app import status as status_mod


DEFAULT_STATES = (
    "Human Design Review",
    "Human Input Needed",
    "Draft PR Ready",
    "AI Planning & Research",
    "AI Implementation",
    "Stop AI",
    "Done",
)
DEFAULT_PROJECT = "Linear Agent Control Plane"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
DEFAULT_CURSOR_PATH = STATE_DIR / "ea-inbox-cursor.json"
DEFAULT_LAST_REPORT_PATH = STATE_DIR / "ea-inbox-last.json"
STALE_JOB_SECONDS = 30 * 60

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
CATEGORY_ORDER = (
    "needs BASIC action",
    "blocked/failing",
    "agent should handle next",
    "done since last check",
    "FYI",
)


@dataclass(frozen=True)
class MemoItem:
    id: str
    created_at: str
    updated_at: str
    source: str
    project_key: str
    issue_id: str | None
    issue_url: str | None
    title: str
    kind: str
    category: str
    severity: str
    summary: str
    why_it_matters: str
    recommended_action: str
    needs_human: bool
    dedupe_key: str
    evidence: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "source": self.source,
            "projectKey": self.project_key,
            "issueId": self.issue_id,
            "issueUrl": self.issue_url,
            "title": self.title,
            "kind": self.kind,
            "category": self.category,
            "severity": self.severity,
            "summary": self.summary,
            "whyItMatters": self.why_it_matters,
            "recommendedAction": self.recommended_action,
            "needsHuman": self.needs_human,
            "dedupeKey": self.dedupe_key,
            "evidence": list(self.evidence),
        }


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


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _item_id(source: str, dedupe_key: str) -> str:
    safe = dedupe_key.replace(" ", "-").replace(":", "-")
    return f"ea-inbox:{source}:{safe}"


def _issue_evidence(issue: linear_api.IssueSummary) -> tuple[dict[str, str], ...]:
    evidence = {"label": issue.identifier or "Issue", "url": issue.url}
    return (evidence,) if issue.url else ({"label": issue.identifier or "Issue", "url": ""},)


def classify_issue(issue: linear_api.IssueSummary, *, since: datetime | None, now: datetime) -> MemoItem | None:
    updated = _parse_datetime(issue.updated_at) or now
    completed = _parse_datetime(issue.completed_at)
    issue_key = issue.identifier or issue.id
    project = issue.project_name or DEFAULT_PROJECT
    state = issue.state_name

    if state == "Human Input Needed":
        return _issue_item(
            issue,
            now=now,
            updated=updated,
            kind="human_gate",
            category="needs BASIC action",
            severity="high",
            summary=f"{issue_key} is waiting for human input.",
            why="Agent work is blocked until the requested input or decision is supplied.",
            action="Answer the blocker, move it to a runnable state, or close/defer it if it is stale.",
            needs_human=True,
            dedupe=f"{issue_key}:human-input",
        )
    if state == "Human Design Review":
        return _issue_item(
            issue,
            now=now,
            updated=updated,
            kind="design_review",
            category="needs BASIC action",
            severity="medium",
            summary=f"{issue_key} has a design/planning result ready for review.",
            why="This is a human gate; implementation should not continue until the plan is approved, revised, or closed.",
            action="Approve the plan, request changes in a comment, or close/defer if it is no longer useful.",
            needs_human=True,
            dedupe=f"{issue_key}:design-review",
        )
    if state == "Draft PR Ready":
        return _issue_item(
            issue,
            now=now,
            updated=updated,
            kind="review_ready",
            category="needs BASIC action",
            severity="medium",
            summary=f"{issue_key} is waiting in Draft PR Ready.",
            why="Work may be complete enough to review, recycle, merge, or close, but should not sit parked silently.",
            action="Review the evidence, recycle if needed, or promote/close it according to repo policy.",
            needs_human=True,
            dedupe=f"{issue_key}:draft-pr-ready",
        )
    if state in {"AI Planning & Research", "AI Implementation"}:
        return _issue_item(
            issue,
            now=now,
            updated=updated,
            kind="active_agent_lane",
            category="agent should handle next",
            severity="low",
            summary=f"{issue_key} is in an AI-active lane.",
            why="The EA should verify there is matching executor activity instead of requiring BASIC attention by default.",
            action="Let the agent continue unless the matching job is stale or failed.",
            needs_human=False,
            dedupe=f"{issue_key}:active-agent-lane",
        )
    if state == "Done" and completed is not None and (since is None or completed > since):
        return _issue_item(
            issue,
            now=now,
            updated=updated,
            kind="completed_issue",
            category="done since last check",
            severity="info",
            summary=f"{issue_key} was closed since the last acknowledged check-in.",
            why="This is useful progress context, but it does not need action unless the closure looks wrong.",
            action="No action unless you disagree with the closure.",
            needs_human=False,
            dedupe=f"{issue_key}:done",
        )
    return None


def _issue_item(
    issue: linear_api.IssueSummary,
    *,
    now: datetime,
    updated: datetime,
    kind: str,
    category: str,
    severity: str,
    summary: str,
    why: str,
    action: str,
    needs_human: bool,
    dedupe: str,
) -> MemoItem:
    issue_key = issue.identifier or issue.id
    return MemoItem(
        id=_item_id("linear", dedupe),
        created_at=_iso(now),
        updated_at=_iso(updated),
        source="linear",
        project_key=issue.project_name or DEFAULT_PROJECT,
        issue_id=issue_key,
        issue_url=issue.url,
        title=issue.title or issue_key,
        kind=kind,
        category=category,
        severity=severity,
        summary=summary,
        why_it_matters=why,
        recommended_action=action,
        needs_human=needs_human,
        dedupe_key=dedupe,
        evidence=_issue_evidence(issue),
    )


def classify_job(job: q.Job, *, since: datetime | None, now: datetime) -> MemoItem | None:
    issue_key = job.identifier or job.ticket_id or f"job-{job.id}"
    started = _parse_datetime(job.started_at)
    completed = _parse_datetime(job.completed_at)
    created = _parse_datetime(job.created_at) or now
    updated = completed or started or created
    evidence = ({"label": f"queue job {job.id}", "url": ""},)
    title = f"{issue_key} executor job {job.id} ({job.kind})"

    if job.status == "failed" and (since is None or updated > since):
        error = status_mod.summarize_error(job.last_error) or "No error summary recorded."
        return MemoItem(
            id=_item_id("executor", f"job:{job.id}:failed"),
            created_at=_iso(now),
            updated_at=_iso(updated),
            source="executor",
            project_key=DEFAULT_PROJECT,
            issue_id=issue_key,
            issue_url=None,
            title=title,
            kind="failed_job",
            category="blocked/failing",
            severity="high",
            summary=f"{issue_key} executor job failed: {error}",
            why_it_matters="Failed agent work can strand a Linear issue unless a human or recycler decides the next step.",
            recommended_action="Inspect the issue and either rerun after fixing the blocker, recycle, or close/defer.",
            needs_human=True,
            dedupe_key=f"job:{job.id}:failed",
            evidence=evidence,
        )

    if job.status == "running":
        elapsed = int((now - (started or created)).total_seconds())
        stale = elapsed >= STALE_JOB_SECONDS
        return MemoItem(
            id=_item_id("executor", f"job:{job.id}:running"),
            created_at=_iso(now),
            updated_at=_iso(updated),
            source="executor",
            project_key=DEFAULT_PROJECT,
            issue_id=issue_key,
            issue_url=None,
            title=title,
            kind="stale_job" if stale else "running_job",
            category="blocked/failing" if stale else "FYI",
            severity="high" if stale else "info",
            summary=f"{issue_key} executor job is {'stale' if stale else 'running'} ({elapsed}s elapsed).",
            why_it_matters="Long-running jobs may need intervention if they stop producing evidence.",
            recommended_action="Check worker output and move to Human Input Needed if it is stuck." if stale else "No action unless it exceeds the stale threshold.",
            needs_human=stale,
            dedupe_key=f"job:{job.id}:running",
            evidence=evidence,
        )

    if job.status == "pending":
        return MemoItem(
            id=_item_id("executor", f"job:{job.id}:pending"),
            created_at=_iso(now),
            updated_at=_iso(updated),
            source="executor",
            project_key=DEFAULT_PROJECT,
            issue_id=issue_key,
            issue_url=None,
            title=title,
            kind="pending_job",
            category="agent should handle next",
            severity="low",
            summary=f"{issue_key} executor job is queued.",
            why_it_matters="Queued work should be visible, but it usually does not need BASIC unless it ages out.",
            recommended_action="Let the worker pick it up; inspect only if it remains pending unexpectedly.",
            needs_human=False,
            dedupe_key=f"job:{job.id}:pending",
            evidence=evidence,
        )

    return None


def _completed_jobs_memo(jobs: list[q.Job], *, since: datetime | None, now: datetime) -> MemoItem | None:
    completed: list[tuple[q.Job, datetime]] = []
    for job in jobs:
        if job.status != "done":
            continue
        completed_at = _parse_datetime(job.completed_at)
        if completed_at is None:
            continue
        if since is not None and completed_at <= since:
            continue
        completed.append((job, completed_at))
    if not completed:
        return None

    completed.sort(key=lambda pair: pair[1], reverse=True)
    latest = completed[0][1]
    issue_keys: list[str] = []
    seen: set[str] = set()
    for job, _completed_at in completed:
        key = job.identifier or job.ticket_id or f"job-{job.id}"
        if key in seen:
            continue
        seen.add(key)
        issue_keys.append(key)
    visible = ", ".join(issue_keys[:8])
    if len(issue_keys) > 8:
        visible = f"{visible}, +{len(issue_keys) - 8} more"
    evidence = tuple({"label": f"queue job {job.id}", "url": ""} for job, _ in completed[:5])
    return MemoItem(
        id=_item_id("executor", "jobs:done-since-ack"),
        created_at=_iso(now),
        updated_at=_iso(latest),
        source="executor",
        project_key=DEFAULT_PROJECT,
        issue_id=None,
        issue_url=None,
        title="Executor jobs completed",
        kind="completed_jobs_summary",
        category="done since last check",
        severity="info",
        summary=f"{len(completed)} executor job(s) completed since the last check-in: {visible}.",
        why_it_matters="This gives progress context without turning the inbox into a raw job log.",
        recommended_action="No action unless a related Linear issue is now waiting in a human-review state.",
        needs_human=False,
        dedupe_key="jobs:done-since-ack",
        evidence=evidence,
    )


def dedupe_items(items: list[MemoItem]) -> list[MemoItem]:
    chosen: dict[str, MemoItem] = {}
    for item in items:
        existing = chosen.get(item.dedupe_key)
        if existing is None:
            chosen[item.dedupe_key] = item
            continue
        if SEVERITY_RANK[item.severity] > SEVERITY_RANK[existing.severity]:
            chosen[item.dedupe_key] = item
            continue
        item_updated = _parse_datetime(item.updated_at) or datetime.min.replace(tzinfo=UTC)
        existing_updated = _parse_datetime(existing.updated_at) or datetime.min.replace(tzinfo=UTC)
        if item_updated > existing_updated:
            chosen[item.dedupe_key] = item
    return sorted(
        chosen.values(),
        key=lambda item: (
            CATEGORY_ORDER.index(item.category) if item.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
            -SEVERITY_RANK[item.severity],
            item.updated_at,
        ),
    )


def build_report(
    *,
    issues: list[linear_api.IssueSummary],
    jobs: list[q.Job],
    since: datetime | None,
    now: datetime,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    items: list[MemoItem] = []
    for issue in issues:
        item = classify_issue(issue, since=since, now=now)
        if item is not None:
            items.append(item)
    for job in jobs:
        item = classify_job(job, since=since, now=now)
        if item is not None:
            items.append(item)
    completed_jobs = _completed_jobs_memo(jobs, since=since, now=now)
    if completed_jobs is not None:
        items.append(completed_jobs)
    deduped = dedupe_items(items)
    counts = {category: 0 for category in CATEGORY_ORDER}
    for item in deduped:
        counts[item.category] = counts.get(item.category, 0) + 1
    return {
        "schemaVersion": "ea_inbox_v1",
        "generatedAt": _iso(now),
        "since": _iso(since) if since else None,
        "counts": counts,
        "warnings": warnings or [],
        "items": [item.to_dict() for item in deduped],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Executive Assistant Inbox", ""]
    lines.append(f"Generated: `{report['generatedAt']}`")
    lines.append(f"Since: `{report['since'] or 'no acknowledgement cursor'}`")
    if report.get("warnings"):
        lines.append("")
        lines.append("## Warnings")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
    items = report.get("items") or []
    if not items:
        lines.append("")
        lines.append("No memo items need attention.")
        return "\n".join(lines)

    for category in CATEGORY_ORDER:
        category_items = [item for item in items if item["category"] == category]
        if not category_items:
            continue
        lines.append("")
        lines.append(f"## {category}")
        for item in category_items:
            title = item["title"]
            if item.get("issueUrl"):
                title = f"[{title}]({item['issueUrl']})"
            lines.append(f"- **{item['severity']}** {title}: {item['summary']}")
            lines.append(f"  Why it matters: {item['whyItMatters']}")
            lines.append(f"  Action: {item['recommendedAction']}")
            evidence = item.get("evidence") or []
            if evidence:
                labels = []
                for entry in evidence:
                    if entry.get("url"):
                        labels.append(f"[{entry['label']}]({entry['url']})")
                    else:
                        labels.append(entry.get("label", "evidence"))
                lines.append(f"  Evidence: {', '.join(labels)}")
    return "\n".join(lines)


def load_ack_cursor(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _parse_datetime(data.get("acknowledgedAt"))


def save_ack_cursor(path: Path, acknowledged_at: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schemaVersion": "ea_inbox_cursor_v1", "acknowledgedAt": _iso(acknowledged_at)}, indent=2)
        + "\n",
        encoding="utf-8",
    )


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_since(cursor_path: Path, since_hours: int) -> datetime:
    cursor = load_ack_cursor(cursor_path)
    if cursor is not None:
        return cursor
    return datetime.now(UTC) - timedelta(hours=since_hours)


def _fetch_issues(projects: list[str], states: list[str]) -> tuple[list[linear_api.IssueSummary], list[str]]:
    issues: list[linear_api.IssueSummary] = []
    warnings: list[str] = []
    for project in projects:
        try:
            issues.extend(linear_api.fetch_project_issues(project, states))
        except Exception as exc:
            warnings.append(f"Could not fetch Linear issues for {project!r}: {exc}")
    return issues, warnings


def _load_jobs(db_path: Path) -> tuple[list[q.Job], list[str]]:
    if not db_path.exists():
        return [], [f"Executor DB not found at {db_path}"]
    try:
        return q.list_jobs(db_path), []
    except Exception as exc:
        return [], [f"Could not read executor DB {db_path}: {exc}"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a memoized Linear EA inbox digest.")
    parser.add_argument("--project", action="append", default=None, help="Linear project name to sweep")
    parser.add_argument("--state", action="append", default=None, help="Linear workflow state to include")
    parser.add_argument("--db", type=Path, default=status_mod.default_db_path(), help="Path to executor jobs.db")
    parser.add_argument("--cursor", type=Path, default=DEFAULT_CURSOR_PATH, help="Ack cursor JSON path")
    parser.add_argument("--since-hours", type=int, default=24, help="Fallback lookback if no ack cursor exists")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--write-state", type=Path, default=DEFAULT_LAST_REPORT_PATH, help="Write latest report JSON here")
    parser.add_argument("--no-linear", action="store_true", help="Skip Linear API reads and report only executor state")
    parser.add_argument("--ack", action="store_true", help="Mark this report as acknowledged after generation")
    parser.add_argument("--post-linear-comment", action="store_true", help="Post Markdown digest to --inbox-issue-id")
    parser.add_argument("--inbox-issue-id", help="Linear issue UUID/identifier for the EA inbox comment")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = datetime.now(UTC)
    since = _default_since(args.cursor, args.since_hours)
    projects = args.project or [DEFAULT_PROJECT]
    states = args.state or list(DEFAULT_STATES)

    issues: list[linear_api.IssueSummary] = []
    warnings: list[str] = []
    if not args.no_linear:
        issues, warnings = _fetch_issues(projects, states)

    jobs, job_warnings = _load_jobs(args.db)
    warnings.extend(job_warnings)
    report = build_report(issues=issues, jobs=jobs, since=since, now=now, warnings=warnings)
    write_report(args.write_state, report)
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else render_markdown(report)
    print(output)

    if args.post_linear_comment:
        if not args.inbox_issue_id:
            print("--inbox-issue-id is required with --post-linear-comment", file=sys.stderr)
            return 2
        linear_api.post_comment(args.inbox_issue_id, render_markdown(report))

    if args.ack:
        save_ack_cursor(args.cursor, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
