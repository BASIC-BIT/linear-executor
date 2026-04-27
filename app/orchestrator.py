"""Orchestrate everything that happens after a webhook trigger fires.

Two stages, each runs as a FastAPI BackgroundTask so the webhook can return 200
to Linear within the 5-second budget while real work happens off-thread.

**Stage 1 — orchestrate_start** (state ``* → AI Implementation``):
1. Resolve the target folder (mapping / override / fallback).
2. If the folder is a git repo, create a worktree on a fresh ``ticket/<id>``
   branch and run Claude there. Otherwise run Claude in the folder directly.
3. Download attachments locally next to the work-cwd.
4. Run Claude headless with safety rules baked into the prompt.
5. If git: commit any pending changes, post diff + shortlog as comment.
   If non-git: post Claude's stdout as comment.
6. Set state to ``In Review``.

**Stage 2 — orchestrate_complete** (state ``In Review → Done``):
- If a worktree exists for this ticket: ``git merge --no-ff`` to main, push if
  origin is configured, remove worktree, delete branch, post merge summary.
- If no worktree (e.g. a non-code ticket like the Hello-World test): no-op
  confirmation comment.
- On merge conflict: post conflict info, set state back to ``In Review``.

If anything fails we log + post an error comment but do not raise — the webhook
response is already on its way back to Linear.
"""
from __future__ import annotations

import logging
import shutil
import traceback
from pathlib import Path

from app import attachments as attachments_mod
from app import git_ops
from app import job_registry
from app import linear_api
from app import queue as q
from app import runner
from app.cli_registry import resolve_cli, resolve_model
from app.folders import resolve_folder


logger = logging.getLogger("linear-executor")


HEADER_RUN = "🤖 **Linear-Executor** — Coding Agent Run"
HEADER_REVIEW = "✅ **Linear-Executor** — Marked Done"
HEADER_MERGE = "🔀 **Linear-Executor** — Merged"
HEADER_CONFLICT = "⚠ **Linear-Executor** — Merge Conflict"
HEADER_PROXY = "⚡ **Linear-Executor** — Ad-hoc Proxy"
HEADER_CANCELLED = "⏸ **Linear-Executor** — Run Cancelled"

MAX_OUTPUT_CHARS = 25_000

TEAM_ID = "0f5513d0-3343-4d69-ad20-fc51da44fb90"  # Test_Dev_123
TICKETS_BASE = Path.home() / "cc-dev" / "tickets"
# Persistent — survives /tmp cleanup so follow-up tickets can still reach
# files written by an earlier proxy run. (TES-606)
PROXY_BASE = Path.home() / "cc-dev" / "linear-executor" / "proxy-outputs"

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


def _build_prompt(
    data: dict,
    *,
    cwd: Path,
    attachments_dir: Path | None,
    in_git: bool,
    branch: str | None,
    comments: list[linear_api.Comment] | None = None,
) -> str:
    title = data.get("title", "")
    description = data.get("description", "") or ""
    identifier = data.get("identifier", "?")
    issue_id = data.get("id", "")

    parts = [f"You are working on Linear ticket {identifier}: {title}", ""]
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
    if issue_id:
        parts.append(PROGRESS_INSTRUCTIONS.replace("{issue_id}", issue_id))
        parts.append("")
    parts.append("---")
    parts.append("Ticket description follows:")
    parts.append("")
    parts.append(description)
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
) -> str:
    lines: list[str] = [HEADER_RUN, ""]

    lines.append("**Folder**")
    lines.append(f"- strategy: `{folder_res.strategy}`")
    lines.append(f"- path: `{folder_res.path}`")
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
        lines.append(f"⏱ timed out after {runner.DEFAULT_TIMEOUT_SECONDS}s")
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
    import os
    explicit = os.getenv("LINEAR_EXECUTOR_DB")
    if explicit:
        return Path(explicit)
    return Path.home() / "cc-dev" / "linear-executor" / "state" / "jobs.db"


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


def _cleanup_empty_ticket_dir(identifier: str) -> None:
    """Remove ~/cc-dev/tickets/<id>/ if it contains no actual files (recursively).

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
    final_state: str = "In Review",
) -> None:
    """Stage 1 — handle a fresh ``→ AI Implementation`` transition. Never raises.

    Args:
        final_state: Linear workflow state to move the ticket to after the
            run. Defaults to ``"In Review"`` (Stage1 standard) so the user
            can review and merge. ``"Done"`` is used by batch-mode runs
            (TES-612) where the user has explicitly opted into unattended
            marathon processing.
    """
    data = payload.get("data") or {}
    issue_id = data.get("id", "")
    identifier = data.get("identifier", "?")

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

            wt_res = git_ops.create_worktree(folder_res.path, worktree_path, branch)
            if not wt_res.ok:
                raise RuntimeError(f"could not create worktree: {wt_res.stderr.strip() or wt_res.stdout.strip()}")
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

        prompt = _build_prompt(
            data, cwd=cwd, attachments_dir=attachments_dir,
            in_git=is_git, branch=branch, comments=comments,
        )
        cli = resolve_cli(data.get("labels"))
        model = resolve_model(data.get("labels"))
        logger.info(
            "stage1 — id=%s backend=%s model=%s",
            identifier, cli, model or "(default)",
        )
        files_before = _snapshot_files(cwd)
        try:
            run_result = runner.run_cli(
                cli, prompt, cwd,
                on_start=lambda p: job_registry.register(identifier, p),
                model=model,
            )
        finally:
            job_registry.unregister(identifier)

        # If Cancel webhook fired during the run, skip the post-run side-effects
        # (no run-comment, no state→In Review). The cancel handler already
        # posted its own comment and set the queue job to cancelled.
        if _was_cancelled_during_run(issue_id, run_result):
            logger.info("stage1 — id=%s skipping post-run actions (cancelled)", identifier)
            return

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
            upload_results=upload_results,
        )

        if issue_id:
            linear_api.post_comment(issue_id, body)
            target_id = _state_id(final_state)
            if target_id:
                linear_api.set_issue_state(issue_id, target_id)
                logger.info("stage1 — id=%s state→%s", identifier, final_state)
            else:
                logger.warning(
                    "stage1 — id=%s could not move state, %r id missing",
                    identifier, final_state,
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


# Files Claude shouldn't have its work uploaded — internal cache/scratch dirs,
# editor lock files, anything that's not actually output.
_ATTACH_SKIP_NAMES = {".DS_Store", "Thumbs.db"}
_ATTACH_SKIP_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
_ATTACH_MAX_BYTES = 25 * 1024 * 1024  # Linear's per-file limit


def _snapshot_files(root: Path) -> dict[Path, float]:
    """Map every file under ``root`` to its mtime. Used to detect files
    written/modified by a run."""
    if not root.exists():
        return {}
    snap: dict[Path, float] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _ATTACH_SKIP_DIR_NAMES for part in p.relative_to(root).parts):
            continue
        if p.name in _ATTACH_SKIP_NAMES:
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

        progress_block = (
            "\n" + PROGRESS_INSTRUCTIONS.replace("{issue_id}", issue_id) + "\n"
            if issue_id else ""
        )

        prompt = (
            f"Linear ticket {identifier}: {title}\n\n"
            f"{description}\n"
            f"{attachment_hint}"
            f"{progress_block}"
            f"{followup_block}\n"
            f"Please answer or perform the task above. Your response will be "
            f"posted as a Linear comment on this ticket."
        )

        cli = resolve_cli(data.get("labels"))
        model = resolve_model(data.get("labels"))
        logger.info(
            "proxy — id=%s backend=%s model=%s",
            identifier, cli, model or "(default)",
        )
        files_before = _snapshot_files(cwd)
        try:
            run_result = runner.run_cli(
                cli, prompt, cwd,
                on_start=lambda p: job_registry.register(identifier, p),
                model=model,
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

        lines = [HEADER_PROXY, "", f"_backend: `{run_result.cli}`_", ""]
        if run_result.timed_out:
            lines.append(f"⏱ timed out after {runner.DEFAULT_TIMEOUT_SECONDS}s")
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
                logger.info("proxy — id=%s state→Done", identifier)

    except Exception as exc:
        logger.error("proxy FAILED for %s: %s\n%s", identifier, exc, traceback.format_exc())
        if issue_id:
            try:
                linear_api.post_comment(issue_id, f"⚠ Linear-Executor proxy failed:\n```\n{exc}\n```")
            except Exception:
                pass
        raise


def orchestrate_complete(payload: dict, delivery_id: str | None = None) -> None:
    """Stage 2 — Bastian moved ticket to Done. Merge worktree if one exists, else no-op."""
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

        # If main repo's working tree is dirty, refuse to checkout — don't lose Bastian's WIP
        if not git_ops.working_tree_clean(repo_path):
            body = (
                f"{HEADER_CONFLICT}\n\nMain repo `{repo_path}` has uncommitted changes "
                f"on `{base}`. Refusing to merge to avoid losing your work. Commit or "
                f"stash there, then move ticket back to Done to retry.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            if issue_id:
                linear_api.post_comment(issue_id, body)
                rev = _state_id("In Review")
                if rev:
                    linear_api.set_issue_state(issue_id, rev)
            logger.warning("stage2 — id=%s main repo dirty, aborted merge", identifier)
            return

        merge_res = git_ops.merge(repo_path, branch, into=base)
        if not merge_res.ok:
            body = (
                f"{HEADER_CONFLICT}\n\nMerge of `{branch}` → `{base}` failed.\n\n"
                f"```\n{(merge_res.stderr or merge_res.stdout).strip()}\n```\n\n"
                f"Status rolled back to **In Review** — resolve the conflict in `{repo_path}` "
                f"and move to Done again to retry.\n\n"
                f"---\n_ticket: {identifier} • delivery: {delivery_id or '?'}_"
            )
            if issue_id:
                linear_api.post_comment(issue_id, body)
                rev = _state_id("In Review")
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
