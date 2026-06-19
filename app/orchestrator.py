"""Orchestrate everything that happens after a webhook trigger fires.

Two stages, each runs as a FastAPI BackgroundTask so the webhook can return 200
to Linear within the 5-second budget while real work happens off-thread.

**Stage 1 — orchestrate_start** (state ``* → AI Planning & Research`` or
``* → AI Implementation``):
1. Resolve the target folder (mapping / override / fallback).
2. If the folder is a git repo, create a worktree on a fresh ``ticket/<id>``
   branch and run Claude there. Otherwise run Claude in the folder directly.
3. Download attachments locally next to the work-cwd.
4. Run Claude headless with safety rules baked into the prompt.
5. If git: commit any pending changes, post diff + shortlog as comment.
   If non-git: post Claude's stdout as comment.
6. Set state to the workflow-specific next gate.

**Stage 2 — orchestrate_complete** (state ``* → Done``):
- If a worktree exists for this ticket: ``git merge --no-ff`` to main, push if
  origin is configured, remove worktree, delete branch, post merge summary.
- If no worktree (e.g. a non-code ticket like the Hello-World test): no-op
  confirmation comment.
- On merge conflict: post conflict info, set state back to ``Draft PR Ready``.

If anything fails we log + post an error comment but do not raise — the webhook
response is already on its way back to Linear.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import traceback
from dataclasses import dataclass
from pathlib import Path

from app import attachments as attachments_mod
from app import folders
from app import git_ops
from app import job_registry
from app import linear_api
from app import queue as q
from app import runner
from app.cli_registry import (
    resolve_auth,
    resolve_cli,
    resolve_context_mode,
    resolve_model,
    resolve_reasoning,
    resolve_timeout,
)
from app.folders import resolve_folder


logger = logging.getLogger("linear-executor")


class WorktreePreparationError(RuntimeError):
    """Deterministic worktree state problem that needs human repair."""


HEADER_RUN = "🤖 **Linear-Executor** — Coding Agent Run"
HEADER_REVIEW = "✅ **Linear-Executor** — Marked Done"
HEADER_MERGE = "🔀 **Linear-Executor** — Merged"
HEADER_CONFLICT = "⚠ **Linear-Executor** — Merge Conflict"
HEADER_PROXY = "⚡ **Linear-Executor** — Ad-hoc AI Proxy"
HEADER_CANCELLED = "⏸ **Linear-Executor** — Run Cancelled"
HEADER_REVIEW_WATCH = "👀 **Linear-Executor** — Review Watch"

MAX_OUTPUT_CHARS = 25_000
MAX_INHERITED_CONTEXT_CHARS = 12_000
MAX_INHERITED_COMMENT_CHARS = 6_000
PROMPTS_DIR_ENV = "LINEAR_CONTROLLER_PROMPTS_DIR"

START_STATE_FINAL_STATES = {
    "AI Planning & Research": "Human Design Review",
    "AI Implementation": "Draft PR Ready",
}

STATE_PROMPT_FILES = {
    "AI Planning & Research": "ai-planning-research.md",
    "AI Implementation": "ai-implementation.md",
}

GITHUB_PR_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s)>'\"]+)/(?P<repo>[^/\s)>'\"]+)/pull/(?P<number>\d+)"
)
LINEAR_REVIEW_PR_RE = re.compile(
    r"https?://linear\.review/(?P<owner>[^/\s)>'\"]+)/(?P<repo>[^/\s)>'\"]+)/pull/(?P<number>\d+)"
)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str

# Linear team UUID. Required for state-id lookups (Backlog / Todo / AI
# Implementation / Draft PR Ready / Done). Set LINEAR_TEAM_ID in .env to your
# team's UUID — find it at Linear Settings → Teams → <your team> in the URL,
# or via the GraphQL API.
TEAM_ID = os.environ.get("LINEAR_TEAM_ID", "").strip()
# Per-ticket folder base — same source-of-truth as folders.FALLBACK_BASE so
# a `folder:` override in description and the fallback path agree on layout.
TICKETS_BASE = Path(folders.FALLBACK_BASE).expanduser()
# Proxy-mode work dirs live next to the installed package by default so
# they're persistent (survives /tmp cleanup) but not tied to any particular
# user's home layout. Override via LINEAR_EXECUTOR_PROXY_BASE in .env if you
# want a different location. (TES-606)
PROXY_BASE = Path(
    os.environ.get(
        "LINEAR_EXECUTOR_PROXY_BASE",
        str(Path(__file__).resolve().parent.parent / "proxy-outputs"),
    )
).expanduser()

_state_id_cache: dict[str, str] = {}


def _state_id(name: str) -> str | None:
    if name not in _state_id_cache:
        try:
            sid = linear_api.fetch_workflow_state_id(TEAM_ID, name)
            if sid:
                _state_id_cache[name] = sid
        except Exception as exc:
            logger.error("could not resolve state id for %r: %s", name, exc)
            return None
    return _state_id_cache.get(name)


def _truncate(s: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n…[truncated, {len(s) - limit} more chars]"


def _extract_github_pr_url(text: str | None) -> str | None:
    if not text:
        return None
    match = GITHUB_PR_RE.search(text)
    if match:
        return match.group(0)
    match = LINEAR_REVIEW_PR_RE.search(text)
    if match:
        return (
            f"https://github.com/{match.group('owner')}/"
            f"{match.group('repo')}/pull/{match.group('number')}"
        )
    return None


def _find_github_pr_url(
    data: dict,
    attachments: list[linear_api.Attachment],
    comments: list[linear_api.Comment],
) -> str | None:
    candidates: list[str | None] = [data.get("description")]
    # Most recent comments usually contain the latest executor-created PR URL.
    candidates.extend(c.body for c in reversed(comments))
    for attachment in attachments:
        candidates.extend([attachment.url, attachment.title, attachment.subtitle])
    for text in candidates:
        url = _extract_github_pr_url(text)
        if url:
            return url
    return None


def _run_gh_pr_ready(pr_url: str) -> CommandResult:
    try:
        completed = subprocess.run(
            ["gh", "pr", "ready", pr_url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("GitHub CLI `gh` is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return CommandResult(124, stdout, stderr or "gh pr ready timed out")
    return CommandResult(completed.returncode, completed.stdout or "", completed.stderr or "")


def _gh_pr_ready_succeeded(result: CommandResult) -> bool:
    if result.exit_code == 0:
        return True
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return "already" in combined and ("ready" in combined or "not a draft" in combined)


def _format_command_result(result: CommandResult, limit: int = 4000) -> str:
    lines = [f"exit code: {result.exit_code}"]
    if result.stdout.strip():
        lines.append("stdout:")
        lines.append(_truncate(result.stdout.strip(), limit=limit))
    if result.stderr.strip():
        lines.append("stderr:")
        lines.append(_truncate(result.stderr.strip(), limit=limit))
    return "\n".join(lines)


def _move_issue_to_human_input(issue_id: str, identifier: str) -> None:
    state_id = _state_id("Human Input Needed")
    if state_id:
        linear_api.set_issue_state(issue_id, state_id)
        logger.info("review-watch — id=%s state→Human Input Needed", identifier)


def _workflow_state_name(data: dict) -> str | None:
    state = data.get("state") or {}
    name = state.get("name")
    return name if isinstance(name, str) and name else None


def _final_state_for_workflow_state(workflow_state: str | None) -> str:
    return START_STATE_FINAL_STATES.get(workflow_state or "", "Draft PR Ready")


def _load_state_prompt(workflow_state: str | None) -> str | None:
    if not workflow_state:
        return None
    filename = STATE_PROMPT_FILES.get(workflow_state)
    if not filename:
        return None
    prompts_dir = os.environ.get(PROMPTS_DIR_ENV, "").strip()
    if not prompts_dir:
        return None
    path = Path(prompts_dir).expanduser() / filename
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        logger.warning("prompt — state=%s prompt file missing: %s", workflow_state, path)
        return None
    except OSError as exc:
        logger.warning("prompt — state=%s could not read %s: %s", workflow_state, path, exc)
        return None
    return text or None


def _pick_timeout(labels, default: int, identifier: str) -> int:
    """Apply per-ticket ``timeout:<sec>`` Linear label override on top of
    a path-specific default, clamped to ``runner.TIMEOUT_HARD_CAP_SECONDS``.

    Logs the chosen value so the operator can correlate Linear-side label
    changes with subprocess behaviour without digging through the runner.
    """
    override = resolve_timeout(labels)
    chosen = override if override is not None else default
    if chosen > runner.TIMEOUT_HARD_CAP_SECONDS:
        logger.warning(
            "timeout — id=%s requested %ds exceeds hard cap %ds; clamping",
            identifier, chosen, runner.TIMEOUT_HARD_CAP_SECONDS,
        )
        chosen = runner.TIMEOUT_HARD_CAP_SECONDS
    if override is not None:
        logger.info("timeout — id=%s using label override %ds (default was %ds)",
                    identifier, chosen, default)
    return chosen


SAFETY_RULES = """
You run inside a git worktree on a dedicated branch — your work is isolated
from main. Hard rules for this session:

- DO NOT run `git push` (the executor will merge after human review).
- DO NOT run `vercel --prod`, `docker compose up`, `systemctl`, or anything
  that touches running production services.
- DO NOT modify files in `~/docker/`, `/etc/`, or `.env` files outside this
  worktree.
- DO NOT run `rm -rf` against directories you didn't create in this session.
- When unsure, leave a `TODO` comment in the code or stop and explain rather
  than guessing.
""".strip()


PROGRESS_INSTRUCTIONS = """
**Progress updates** — give the user real-time visibility, not just a final answer.

Before each step that takes more than ~10 seconds (web search, file
download, transcription, long LLM call, multi-step subprocess), post a
short comment to this Linear issue using the Linear MCP:

```
mcp__claude_ai_Linear__save_comment(
    issueId="{issue_id}",
    body="🔄 <one-line description of what you're about to do>"
)
```

Rules:
- Always start the body with the 🔄 emoji — the executor uses it to
  recognise progress comments and exclude them from follow-up context.
- Keep each progress line under ~200 characters. One sentence.
- At most one progress line per significant step (don't spam every minor
  tool call).
- The final result still goes back via your normal stdout — the executor
  posts that as the closing comment automatically.
""".strip()


def _format_followup_comments(comments: list[linear_api.Comment] | None) -> list[str]:
    """Render non-executor comments as an appendable 'Follow-up Instructions' block.

    Empty return → no section rendered in the prompt. Executor-authored
    comments are skipped so earlier runs don't echo back as context.
    """
    if not comments:
        return []
    relevant = [c for c in comments if not c.is_executor_comment and (c.body or "").strip()]
    if not relevant:
        return []
    lines = ["", "---", "Follow-up Instructions (comments added after ticket creation, chronological):", ""]
    for c in relevant:
        who = c.author_name or "unknown"
        stamp = (c.created_at or "").replace("T", " ").rstrip("Z")
        lines.append(f"— {who} · {stamp}")
        lines.append(c.body.rstrip())
        lines.append("")
    return lines


def _prompt_truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n\n...[truncated, {len(s) - limit} more chars]"


def _is_inheritable_executor_comment(comment: linear_api.Comment) -> bool:
    """True for previous executor result comments worth feeding to forked runs."""
    if not comment.is_executor_comment:
        return False
    body = (comment.body or "").lstrip()
    return (
        body.startswith("\U0001f916 **Linear-Executor**")
        or body.startswith(HEADER_PROXY)
        or body.startswith("⚠ Linear-Executor")
    )


def _format_inherited_context(
    comments: list[linear_api.Comment] | None,
    context_mode: str,
) -> list[str]:
    """Render selected previous executor output for ``context:fork``.

    ``context:fresh`` deliberately keeps the older behavior: the worker sees
    the ticket, attachments, and human follow-up comments, but not prior agent
    output. ``context:fork`` includes previous run/proxy output as background
    evidence with a role reset so the worker does not continue the old task by
    accident.
    """
    if context_mode != "fork" or not comments:
        return []

    relevant = [c for c in comments if _is_inheritable_executor_comment(c)]
    if not relevant:
        return []

    lines = [
        "",
        "---",
        "Inherited Context (context:fork)",
        "",
        "You are a fork of previous executor work on this Linear ticket. The inherited output below is background evidence only. Your current task is still the ticket description and any human follow-up instructions. If inherited context conflicts with current instructions, follow the current instructions.",
        "",
    ]
    used = 0
    for c in relevant:
        who = c.author_name or "Linear-Executor"
        stamp = (c.created_at or "").replace("T", " ").rstrip("Z")
        body = _prompt_truncate(c.body.rstrip(), MAX_INHERITED_COMMENT_CHARS)
        rendered = f"Previous executor output — {who} · {stamp}\n{body}\n"
        remaining = MAX_INHERITED_CONTEXT_CHARS - used
        if remaining <= 0:
            lines.append("...[additional inherited executor output omitted]")
            break
        if len(rendered) > remaining:
            rendered = _prompt_truncate(rendered, remaining)
            lines.append(rendered.rstrip())
            break
        lines.append(rendered.rstrip())
        lines.append("")
        used += len(rendered)
    return lines


def _supports_linear_progress(cli: str) -> bool:
    """Whether this backend can use the Claude-style Linear MCP tool name."""
    return cli == "claude"


def _build_prompt(
    data: dict,
    *,
    cwd: Path,
    attachments_dir: Path | None,
    in_git: bool,
    branch: str | None,
    comments: list[linear_api.Comment] | None = None,
    cli: str = "claude",
    context_mode: str = "fresh",
    workflow_state: str | None = None,
    state_prompt: str | None = None,
) -> str:
    title = data.get("title", "")
    description = data.get("description", "") or ""
    identifier = data.get("identifier", "?")
    issue_id = data.get("id", "")

    parts = [f"You are working on Linear ticket {identifier}: {title}", ""]
    if workflow_state:
        parts.append(f"Workflow state: {workflow_state}")
        parts.append("")
    if in_git:
        parts.append(f"Working directory (a git worktree): {cwd}")
        parts.append(f"Branch: {branch}")
        parts.append("")
        parts.append(SAFETY_RULES)
        parts.append("")
    else:
        parts.append(f"Working directory: {cwd}")
        parts.append("")
    if attachments_dir and attachments_dir.exists() and any(attachments_dir.iterdir()):
        parts.append(f"Attachments referenced by this ticket are in: {attachments_dir}")
        parts.append("(Read them as context but don't commit them.)")
        parts.append("")
    if issue_id and _supports_linear_progress(cli):
        parts.append(PROGRESS_INSTRUCTIONS.replace("{issue_id}", issue_id))
        parts.append("")
    if state_prompt:
        parts.append("---")
        parts.append(f"Controller prompt for `{workflow_state}`:")
        parts.append("")
        parts.append(state_prompt)
        parts.append("")
    parts.append("---")
    parts.append("Ticket description follows:")
    parts.append("")
    parts.append(description)
    parts.extend(_format_inherited_context(comments, context_mode))
    parts.extend(_format_followup_comments(comments))
    parts.append("")
    parts.append("Complete the work above. Your final reply will be posted as a "
                 "comment on the ticket; if you produced code changes, the "
                 "executor will diff and surface them automatically.")
    return "\n".join(parts)


def _compose_run_comment(
    identifier: str,
    folder_res,
    found_attachments: list[linear_api.Attachment],
    downloaded: list[Path],
    run_result: runner.RunResult,
    delivery_id: str | None,
    *,
    branch: str | None = None,
    diff_text: str | None = None,
    shortlog_text: str | None = None,
    upload_results: list[tuple[Path, str | None, str | None]] | None = None,
    context_mode: str = "fresh",
    workflow_state: str | None = None,
) -> str:
    lines: list[str] = [HEADER_RUN, ""]

    lines.append("**Folder**")
    lines.append(f"- strategy: `{folder_res.strategy}`")
    lines.append(f"- path: `{folder_res.path}`")
    if workflow_state:
        lines.append(f"- workflow state: `{workflow_state}`")
    lines.append(f"- context: `{context_mode}`")
    if branch:
        lines.append(f"- branch: `{branch}`")
    lines.append("")

    lines.append("**Attachments**")
    if not found_attachments:
        lines.append("- none")
    else:
        for att, local in zip(found_attachments, downloaded):
            lines.append(f"- `{att.title}` → `{local}`")
    lines.append("")

    lines.append(f"**Output** (backend: `{run_result.cli}`)")
    if run_result.timed_out:
        lines.append(f"⏱ timed out after {run_result.timeout_used}s")
    elif run_result.exit_code != 0:
        lines.append(f"⚠ exit code {run_result.exit_code}")
    if run_result.stdout.strip():
        lines.append("")
        lines.append(_truncate(run_result.stdout))
    else:
        lines.append("")
        lines.append("_(no stdout)_")
    if run_result.stderr.strip():
        lines.append("")
        lines.append("**stderr**")
        lines.append("```")
        lines.append(_truncate(run_result.stderr, limit=4000))
        lines.append("```")
    lines.append("")

    if shortlog_text is not None:
        lines.append("**Commits on branch**")
        if shortlog_text.strip():
            lines.append("```")
            lines.append(shortlog_text.strip())
            lines.append("```")
        else:
            lines.append("_(no commits — Claude made no changes)_")
        lines.append("")

    if diff_text is not None:
        lines.append("**Diff vs. main**")
        if diff_text.strip():
            lines.append("```diff")
            lines.append(_truncate(diff_text, limit=15_000))
            lines.append("```")
        else:
            lines.append("_(no diff)_")
        lines.append("")

    if upload_results:
        uploaded = [r for r in upload_results if r[1] is not None]
        failed = [r for r in upload_results if r[1] is None]
        lines.append("**Files attached to ticket**")
        for path, att_id, _err in uploaded:
            lines.append(f"- `{path.name}` — uploaded ({att_id})")
        for path, _att, err in failed:
            lines.append(f"- `{path.name}` — upload failed: {err}")
        lines.append("")

    lines.append("---")
    lines.append(f"_ticket: {identifier} • delivery: {delivery_id or '?'}_")
    return "\n".join(lines)


def _ticket_dir(identifier: str) -> Path:
    return TICKETS_BASE / identifier


def _db_path() -> Path:
    """Resolve the queue DB path the same way main.py does."""
    explicit = os.getenv("LINEAR_EXECUTOR_DB")
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parent.parent / "state" / "jobs.db"


def _was_cancelled_during_run(issue_id: str | None, run_result) -> bool:
    """True if cancel.cancel_ticket flipped the queue status while we ran.

    Used to short-circuit post-run side-effects in orchestrate_start /
    orchestrate_proxy after a Cancel webhook killed the subprocess. Two
    signals together: queue has a cancelled job for this ticket, AND the
    subprocess exited with the SIGTERM signature (-15 / 143). Either one
    alone could be a normal failure, both together is unambiguous cancel.
    """
    if issue_id is None:
        return False
    try:
        if not q.has_cancelled_job(_db_path(), issue_id):
            return False
    except Exception:
        return False
    # Subprocess killed by cancel handler exits 143 (128+15 SIGTERM) or
    # -15 if Python translates the signal. Some non-cancel paths can also
    # produce these but combined with cancelled-in-DB it's safe to skip.
    return run_result.exit_code in (-15, 143, -9, 137)


def _backend_failure_reason(run_result: runner.RunResult) -> str | None:
    """Return a concise failure reason for backend runs that should not advance.

    Some CLIs can print a permission failure and still exit 0, so we also look
    for OpenCode's non-interactive auto-reject wording.
    """
    combined = "\n".join([run_result.stdout or "", run_result.stderr or ""])
    if run_result.timed_out:
        return f"{run_result.cli} timed out after {run_result.timeout_used}s"
    if run_result.exit_code != 0:
        return f"{run_result.cli} exited with code {run_result.exit_code}"
    lowered = combined.lower()
    if "permission requested:" in lowered and "auto-rejecting" in lowered:
        return f"{run_result.cli} hit an auto-rejected permission request"
    if "the user rejected permission" in lowered:
        return f"{run_result.cli} reported a rejected permission request"
    return None


def _format_backend_failure(reason: str, run_result: runner.RunResult) -> str:
    details = "\n".join(
        part.strip()
        for part in (run_result.stderr, run_result.stdout)
        if part and part.strip()
    )
    if not details:
        return reason
    return f"{reason}\n\n{_truncate(details, limit=1500)}"


def _cleanup_empty_ticket_dir(identifier: str) -> None:
    """Remove TICKETS_BASE/<id>/ if it contains no actual files (recursively).

    Conservative: empty sub-dirs are fine, but a single file anywhere makes us
    leave the whole tree alone — Bastian or Claude may have put work there.
    """
    td = _ticket_dir(identifier)
    if not td.exists():
        return
    has_files = any(p.is_file() for p in td.rglob("*"))
    if has_files:
        return
    try:
        shutil.rmtree(td)
        logger.info("cleanup — id=%s removed empty ticket dir %s", identifier, td)
    except OSError as exc:
        logger.warning("cleanup — id=%s could not remove %s: %s", identifier, td, exc)


def orchestrate_start(
    payload: dict,
    delivery_id: str | None = None,
    *,
    final_state: str | None = None,
) -> None:
    """Stage 1 — handle a fresh transition into an AI-active start state.

    Args:
        final_state: Optional override for the Linear workflow state to move
            the ticket to after the run. When omitted, the current workflow
            state chooses the next gate.
    """
    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "?")
    workflow_state = _workflow_state_name(data)
    target_state = final_state or _final_state_for_workflow_state(workflow_state)

    try:
        folder_res = resolve_folder(data)
        is_git = git_ops.is_git_repo(folder_res.path)

        branch: str | None = None
        if is_git:
            branch = f"ticket/{identifier}"
            ticket_dir = _ticket_dir(identifier)
            worktree_path = ticket_dir / "worktree"
            attachments_dir = ticket_dir / "attachments"
            ticket_dir.mkdir(parents=True, exist_ok=True)

            wt_res = git_ops.prepare_ticket_worktree(folder_res.path, worktree_path, branch)
            if not wt_res.ok:
                raise WorktreePreparationError(
                    f"could not prepare worktree: {wt_res.stderr.strip() or wt_res.stdout.strip()}"
                )
            cwd = worktree_path
            logger.info("stage1 — id=%s git worktree at %s on branch %s", identifier, cwd, branch)
        else:
            cwd = folder_res.path
            attachments_dir = folder_res.path / "linear" / identifier / "attachments"
            logger.info("stage1 — id=%s non-git folder %s", identifier, cwd)

        found = linear_api.fetch_issue_attachments(issue_id) if issue_id else []
        downloaded: list[Path] = []
        if found:
            downloaded = attachments_mod.download_attachments(found, attachments_dir)
            logger.info("stage1 — id=%s downloaded %d attachment(s)", identifier, len(downloaded))

        comments = linear_api.fetch_issue_comments(issue_id) if issue_id else []
        followup_count = sum(1 for c in comments if not c.is_executor_comment and (c.body or "").strip())
        if followup_count:
            logger.info("stage1 — id=%s attaching %d follow-up comment(s) to prompt", identifier, followup_count)

        cli = resolve_cli(data.get("labels"))
        model = resolve_model(data.get("labels"))
        reasoning = resolve_reasoning(data.get("labels"))
        auth_mode = resolve_auth(data.get("labels"), cli)
        context_mode = resolve_context_mode(data.get("labels"))
        state_prompt = _load_state_prompt(workflow_state)
        prompt = _build_prompt(
            data, cwd=cwd, attachments_dir=attachments_dir,
            in_git=is_git, branch=branch, comments=comments, cli=cli,
            context_mode=context_mode, workflow_state=workflow_state,
            state_prompt=state_prompt,
        )
        timeout = _pick_timeout(data.get("labels"), runner.DEFAULT_TIMEOUT_STAGE1, identifier)
        logger.info(
            "stage1 — id=%s backend=%s auth=%s context=%s model=%s reasoning=%s timeout=%ds",
            identifier, cli, auth_mode, context_mode, model or "(default)",
            reasoning or "(default)", timeout,
        )
        files_before = _snapshot_files(cwd)
        try:
            run_kwargs = {
                "timeout": timeout,
                "on_start": lambda p: job_registry.register(identifier, p),
                "model": model,
                "auth_mode": auth_mode,
            }
            if reasoning is not None:
                run_kwargs["reasoning"] = reasoning
            run_result = runner.run_cli(
                cli, prompt, cwd,
                **run_kwargs,
            )
        finally:
            job_registry.unregister(identifier)

        # If Cancel webhook fired during the run, skip the post-run side-effects
        # (no run-comment, no state→Draft PR Ready). The cancel handler already
        # posted its own comment and set the queue job to cancelled.
        if _was_cancelled_during_run(issue_id, run_result):
            logger.info("stage1 — id=%s skipping post-run actions (cancelled)", identifier)
            return

        failure_reason = _backend_failure_reason(run_result)
        if failure_reason is not None:
            raise RuntimeError(_format_backend_failure(failure_reason, run_result))

        diff_text: str | None = None
        shortlog_text: str | None = None
        if is_git:
            git_ops.commit_all(cwd, f"Linear-Executor: {identifier} — auto-commit pending changes")
            base = git_ops.default_branch(folder_res.path)
            diff_text = git_ops.diff(cwd, base=base)
            shortlog_text = git_ops.shortlog(cwd, base=base)

        # Auto-attach new/changed files in cwd as Linear attachments (TES-615).
        # In a git worktree this captures both tracked + untracked changes;
        # in non-git folders it captures whatever Claude wrote into cwd.
        files_after = _snapshot_files(cwd)
        new_files = _files_added_or_changed(files_before, files_after)
        upload_results: list[tuple[Path, str | None, str | None]] = []
        if new_files and issue_id:
            upload_results = _attach_files_to_issue(issue_id, identifier, new_files)
            logger.info(
                "stage1 — id=%s auto-attached %d/%d files to ticket",
                identifier, sum(1 for r in upload_results if r[1] is not None), len(new_files),
            )

        body = _compose_run_comment(
            identifier, folder_res, found, downloaded, run_result, delivery_id,
            branch=branch, diff_text=diff_text, shortlog_text=shortlog_text,
            upload_results=upload_results, context_mode=context_mode,
            workflow_state=workflow_state,
        )

        if issue_id:
            linear_api.post_comment(issue_id, body)
            target_id = _state_id(target_state)
            if target_id:
                linear_api.set_issue_state(issue_id, target_id)
                logger.info("stage1 — id=%s state→%s", identifier, target_state)
            else:
                logger.warning(
                    "stage1 — id=%s could not move state, %r id missing",
                    identifier, target_state,
                )
        else:
            logger.warning("stage1 — id=%s no issue_id, skipped comment+state", identifier)

    except Exception as exc:
        logger.error("stage1 FAILED for %s: %s\n%s", identifier, exc, traceback.format_exc())
        if issue_id:
            try:
                linear_api.post_comment(issue_id, f"⚠ Linear-Executor failed:\n```\n{exc}\n```")
            except Exception:
                pass
        raise  # let the worker catch it for retry bookkeeping


def _proxy_dir(identifier: str) -> Path:
    return PROXY_BASE / identifier


# Files Claude shouldn't have its work uploaded: runtime state, caches,
# dependency trees, local secrets, and anything ignored by the repo.
_ATTACH_SKIP_NAMES = {".DS_Store", "Thumbs.db"}
_ATTACH_SKIP_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "ENV",
    "__pycache__",
    "build",
    "dist",
    "env",
    "logs",
    "node_modules",
    "proxy-outputs",
    "state",
    "venv",
}
_ATTACH_SKIP_SUFFIXES = {".db", ".log", ".sqlite", ".sqlite3"}
_ATTACH_MAX_BYTES = 25 * 1024 * 1024  # Linear's per-file limit


def _is_local_artifact(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if path.name in _ATTACH_SKIP_NAMES:
        return True
    if any(part in _ATTACH_SKIP_DIR_NAMES for part in rel.parts):
        return True
    if path.name.startswith(".env"):
        return True
    return path.suffix.lower() in _ATTACH_SKIP_SUFFIXES


def _git_ignored_files(root: Path, files: list[Path]) -> set[Path]:
    if not files:
        return set()
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if top.returncode != 0:
        return set()
    if Path(top.stdout.strip()).resolve() != root.resolve():
        return set()

    rels = [p.relative_to(root).as_posix() for p in files]
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=str(root),
            input="\n".join(rels) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode not in {0, 1}:
        return set()
    ignored_rels = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    return {root / rel for rel in ignored_rels}


def _snapshot_files(root: Path) -> dict[Path, float]:
    """Map every file under ``root`` to its mtime. Used to detect files
    written/modified by a run."""
    if not root.exists():
        return {}
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_local_artifact(p, root):
            continue
        candidates.append(p)

    ignored = _git_ignored_files(root, candidates)
    snap: dict[Path, float] = {}
    for p in candidates:
        if p in ignored:
            continue
        try:
            snap[p] = p.stat().st_mtime
        except OSError:
            pass
    return snap


def _files_added_or_changed(before: dict[Path, float], after: dict[Path, float]) -> list[Path]:
    """Return the paths whose mtime changed or that didn't exist before."""
    changed: list[Path] = []
    for path, mtime in after.items():
        prior = before.get(path)
        if prior is None or mtime > prior:
            changed.append(path)
    changed.sort()
    return changed


def _attach_files_to_issue(
    issue_id: str, identifier: str, files: list[Path],
) -> list[tuple[Path, str | None, str | None]]:
    """Best-effort upload of each file to Linear. Returns ``(path, attachment_id_or_None, error_or_None)``
    so the caller can both report success and surface failures in the comment.
    """
    results: list[tuple[Path, str | None, str | None]] = []
    for path in files:
        try:
            size = path.stat().st_size
        except OSError as exc:
            results.append((path, None, f"stat failed: {exc}"))
            continue
        if size > _ATTACH_MAX_BYTES:
            results.append((path, None, f"skipped: {size} bytes exceeds Linear's 25 MB limit"))
            continue
        try:
            att_id = linear_api.attach_local_file(issue_id, path, title=path.name)
            logger.info("proxy — id=%s attached %s as %s", identifier, path, att_id)
            results.append((path, att_id, None))
        except Exception as exc:
            logger.warning("proxy — id=%s could not attach %s: %s", identifier, path, exc)
            results.append((path, None, str(exc)))
    return results


def orchestrate_proxy(payload: dict, delivery_id: str | None = None) -> None:
    """Phase 4 — lightweight ad-hoc Q&A flow.

    No folder mapping, no git worktree, no In-Review step. Run Claude in a
    temp dir, post the answer, set state directly to Done.
    """
    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "?")

    try:
        cwd = _proxy_dir(identifier)
        cwd.mkdir(parents=True, exist_ok=True)
        logger.info("proxy — id=%s cwd=%s", identifier, cwd)

        found = linear_api.fetch_issue_attachments(issue_id) if issue_id else []
        attachments_dir = cwd / "attachments"
        downloaded: list[Path] = []
        if found:
            downloaded = attachments_mod.download_attachments(found, attachments_dir)
            logger.info("proxy — id=%s downloaded %d attachment(s)", identifier, len(downloaded))

        title = data.get("title", "")
        description = data.get("description", "") or ""
        attachment_hint = (
            f"\nAttachments referenced by this ticket are in: {attachments_dir}\n"
            if downloaded else ""
        )
        comments = linear_api.fetch_issue_comments(issue_id) if issue_id else []
        followup_block = "\n".join(_format_followup_comments(comments))
        if followup_block:
            followup_count = sum(1 for c in comments if not c.is_executor_comment and (c.body or "").strip())
            logger.info("proxy — id=%s attaching %d follow-up comment(s) to prompt", identifier, followup_count)

        cli = resolve_cli(data.get("labels"))
        context_mode = resolve_context_mode(data.get("labels"))
        progress_block = (
            "\n" + PROGRESS_INSTRUCTIONS.replace("{issue_id}", issue_id) + "\n"
            if issue_id and _supports_linear_progress(cli) else ""
        )
        inherited_block = "\n".join(_format_inherited_context(comments, context_mode))
        prompt = (
            f"Linear ticket {identifier}: {title}\n\n"
            f"{description}\n"
            f"{attachment_hint}"
            f"{progress_block}"
            f"{inherited_block}\n"
            f"{followup_block}\n"
            f"Please answer or perform the task above. Your response will be "
            f"posted as a Linear comment on this ticket."
        )

        model = resolve_model(data.get("labels"))
        auth_mode = resolve_auth(data.get("labels"), cli)
        timeout = _pick_timeout(data.get("labels"), runner.DEFAULT_TIMEOUT_PROXY, identifier)
        logger.info(
            "proxy — id=%s backend=%s auth=%s context=%s model=%s timeout=%ds",
            identifier, cli, auth_mode, context_mode, model or "(default)", timeout,
        )
        files_before = _snapshot_files(cwd)
        try:
            run_result = runner.run_cli(
                cli, prompt, cwd,
                timeout=timeout,
                on_start=lambda p: job_registry.register(identifier, p),
                model=model,
                auth_mode=auth_mode,
            )
        finally:
            job_registry.unregister(identifier)

        if _was_cancelled_during_run(issue_id, run_result):
            logger.info("proxy — id=%s skipping post-run actions (cancelled)", identifier)
            return

        files_after = _snapshot_files(cwd)
        new_files = _files_added_or_changed(files_before, files_after)
        attach_results: list[tuple[Path, str | None, str | None]] = []
        if new_files and issue_id:
            attach_results = _attach_files_to_issue(issue_id, identifier, new_files)

        lines = [
            HEADER_PROXY,
            "",
            f"_backend: `{run_result.cli}` • context: `{context_mode}`_",
            "",
        ]
        if run_result.timed_out:
            lines.append(f"⏱ timed out after {run_result.timeout_used}s")
        elif run_result.exit_code != 0:
            lines.append(f"⚠ exit code {run_result.exit_code}")
        lines.append("")
        lines.append(_truncate(run_result.stdout) if run_result.stdout.strip() else "_(no output)_")
        if run_result.stderr.strip():
            lines.append("")
            lines.append("**stderr**")
            lines.append("```")
            lines.append(_truncate(run_result.stderr, limit=4000))
            lines.append("```")
        if attach_results:
            uploaded = [r for r in attach_results if r[1] is not None]
            failed = [r for r in attach_results if r[1] is None]
            lines.append("")
            lines.append("**Files generated**")
            for path, att_id, _err in uploaded:
                lines.append(f"- `{path.name}` — uploaded ({att_id})")
            for path, _att, err in failed:
                lines.append(f"- `{path.name}` — upload failed: {err}")
            lines.append("")
            lines.append(f"Local copies: `{cwd}`")
        lines.append("")
        lines.append("---")
        lines.append(f"_ticket: {identifier} • delivery: {delivery_id or '?'}_")
        body = "\n".join(lines)

        if issue_id:
            linear_api.post_comment(issue_id, body)
            done_id = _state_id("Done")
            if done_id:
                linear_api.set_issue_state(issue_id, done_id)
                logger.info("proxy — id=%s state->Done", identifier)

    except Exception as exc:
        logger.error("proxy FAILED for %s: %s\n%s", identifier, exc, traceback.format_exc())
        if issue_id:
            try:
                linear_api.post_comment(issue_id, f"⚠ Linear-Executor proxy failed:\n```\n{exc}\n```")
            except Exception:
                pass
        raise


def orchestrate_review_watch(payload: dict, delivery_id: str | None = None) -> None:
    """Publish a linked draft PR and leave the ticket in AI Review Watch."""
    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "?")

    if not issue_id:
        logger.warning("review-watch — id=%s no issue_id, skipped", identifier)
        return

    try:
        attachments = linear_api.fetch_issue_attachments(issue_id)
        comments = linear_api.fetch_issue_comments(issue_id)
        pr_url = _find_github_pr_url(data, attachments, comments)
        if not pr_url:
            body = (
                f"{HEADER_REVIEW_WATCH}\n\n"
                "No GitHub pull request URL was found in the Linear issue "
                "description, attachments, or comments. Add the PR URL or a "
                "`Linear: <issue-key>` PR link, then move the ticket back to "
                "`AI Review Watch` to retry.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            linear_api.post_comment(issue_id, body)
            _move_issue_to_human_input(issue_id, identifier)
            return

        result = _run_gh_pr_ready(pr_url)
        if _gh_pr_ready_succeeded(result):
            details = _format_command_result(result) if result.stdout.strip() or result.stderr.strip() else "exit code: 0"
            body = (
                f"{HEADER_REVIEW_WATCH}\n\n"
                f"Marked PR ready for review: {pr_url}\n\n"
                "Command:\n"
                "```\n"
                f"gh pr ready {pr_url}\n"
                "```\n\n"
                "Result:\n"
                "```\n"
                f"{details}\n"
                "```\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            linear_api.post_comment(issue_id, body)
            logger.info("review-watch — id=%s marked PR ready: %s", identifier, pr_url)
            return

        body = (
            f"{HEADER_REVIEW_WATCH}\n\n"
            f"Could not mark PR ready for review: {pr_url}\n\n"
            "Command result:\n"
            "```\n"
            f"{_format_command_result(result)}\n"
            "```\n\n"
            "Status moved to `Human Input Needed`; fix the PR/link or run "
            "the GitHub action manually, then move back to `AI Review Watch`.\n\n"
            f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
        )
        linear_api.post_comment(issue_id, body)
        _move_issue_to_human_input(issue_id, identifier)
        logger.warning("review-watch — id=%s gh pr ready failed for %s", identifier, pr_url)

    except Exception as exc:
        logger.error("review-watch FAILED for %s: %s\n%s", identifier, exc, traceback.format_exc())
        try:
            linear_api.post_comment(
                issue_id,
                f"{HEADER_REVIEW_WATCH}\n\nFailed while preparing review watch:\n```\n{exc}\n```",
            )
            _move_issue_to_human_input(issue_id, identifier)
        except Exception:
            pass


def orchestrate_complete(payload: dict, delivery_id: str | None = None) -> None:
    """Stage 2 — BASIC moved ticket to Done. Merge worktree if one exists, else no-op."""
    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "?")

    try:
        ticket_dir = _ticket_dir(identifier)
        worktree_path = ticket_dir / "worktree"

        if not worktree_path.exists():
            body = (
                f"{HEADER_REVIEW}\n\nNo worktree to merge for this ticket "
                f"(non-code or already cleaned up). Stage 2 noop.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            if issue_id:
                linear_api.post_comment(issue_id, body)
            logger.info("stage2 — id=%s no worktree, posted noop comment", identifier)
            _cleanup_empty_ticket_dir(identifier)
            return

        # Find the original repo from the worktree
        repo_res = git_ops._run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=worktree_path,
        )
        if not repo_res.ok:
            raise RuntimeError(f"could not find main repo for worktree: {repo_res.stderr}")
        # --git-common-dir points at <repo>/.git, parent of that is the repo
        repo_path = Path(repo_res.stdout.strip()).parent

        branch = f"ticket/{identifier}"
        base = git_ops.default_branch(repo_path)

        # If main repo's working tree is dirty, refuse to checkout — don't lose BASIC's WIP
        if not git_ops.working_tree_clean(repo_path):
            body = (
                f"{HEADER_CONFLICT}\n\nMain repo `{repo_path}` has uncommitted changes "
                f"on `{base}`. Refusing to merge to avoid losing your work. Commit or "
                f"stash there, then move ticket back to Done to retry.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            if issue_id:
                linear_api.post_comment(issue_id, body)
                rev = _state_id("Draft PR Ready")
                if rev:
                    linear_api.set_issue_state(issue_id, rev)
            logger.warning("stage2 — id=%s main repo dirty, aborted merge", identifier)
            return

        merge_res = git_ops.merge(repo_path, branch, into=base)
        if not merge_res.ok:
            body = (
                f"{HEADER_CONFLICT}\n\nMerge of `{branch}` → `{base}` failed.\n\n"
                f"```\n{(merge_res.stderr or merge_res.stdout).strip()}\n```\n\n"
                f"Status rolled back to **Draft PR Ready** — resolve the conflict in `{repo_path}` "
                f"and move to Done again to retry.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            if issue_id:
                linear_api.post_comment(issue_id, body)
                rev = _state_id("Draft PR Ready")
                if rev:
                    linear_api.set_issue_state(issue_id, rev)
            logger.warning("stage2 — id=%s merge conflict", identifier)
            return

        push_msg = ""
        if git_ops.has_remote(repo_path):
            push_res = git_ops.push(repo_path, base)
            push_msg = (
                f"\nPushed to origin/{base}: ✅" if push_res.ok
                else f"\n⚠ Push failed:\n```\n{(push_res.stderr or push_res.stdout).strip()}\n```"
            )
        else:
            push_msg = "\n_(no `origin` remote configured — skipped push)_"

        # Cleanup
        git_ops.remove_worktree(repo_path, worktree_path)
        git_ops.delete_branch(repo_path, branch)

        body = (
            f"{HEADER_MERGE}\n\n"
            f"Merged `{branch}` → `{base}` in `{repo_path}`.{push_msg}\n\n"
            f"Worktree removed, branch deleted.\n\n"
            f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
        )
        if issue_id:
            linear_api.post_comment(issue_id, body)
        _cleanup_empty_ticket_dir(identifier)
        logger.info("stage2 — id=%s merged + cleaned up", identifier)

    except Exception as exc:
        logger.error("stage2 FAILED for %s: %s\n%s", identifier, exc, traceback.format_exc())
        if issue_id:
            try:
                linear_api.post_comment(issue_id, f"⚠ Stage 2 failed:\n```\n{exc}\n```")
            except Exception:
                pass
        raise


# Backwards-compat alias for tests/imports written against Phase 2 skeleton.
orchestrate = orchestrate_start
