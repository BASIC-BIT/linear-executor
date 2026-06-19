"""Thin subprocess-based wrapper around git for the executor.

Phase 3b: Stage 1 creates a worktree per ticket so Claude works on an isolated
branch; Stage 2 merges that branch back to main, pushes, and cleans up.

All functions take explicit paths and never raise on git failure — they return
``GitResult`` so the caller can decide how to react (retry, comment, status
rollback, etc.).
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("linear-executor")

# Git author identity for executor commits. Override via LINEAR_EXECUTOR_GIT_EMAIL
# and LINEAR_EXECUTOR_GIT_NAME in .env so commits attribute to your own setup.
GIT_AUTHOR_EMAIL = os.environ.get("LINEAR_EXECUTOR_GIT_EMAIL", "executor@example.com").strip()
GIT_AUTHOR_NAME = os.environ.get("LINEAR_EXECUTOR_GIT_NAME", "Linear-Executor").strip()


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str
    stderr: str


def _run(cmd: list[str], cwd: Path, *, timeout: int = 60) -> GitResult:
    logger.info("git: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return GitResult(False, exc.stdout or "", f"TIMEOUT after {timeout}s")
    return GitResult(
        ok=(completed.returncode == 0),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    res = _run(["git", "rev-parse", "--git-dir"], cwd=path)
    return res.ok


def default_branch(repo_path: Path) -> str:
    """Return the repo's default branch name (main, master, …) or 'main' as last resort."""
    res = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_path)
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    return "main"


def create_worktree(repo_path: Path, worktree_path: Path, branch: str) -> GitResult:
    """Create a new worktree on a fresh branch. Idempotent-ish: if the branch or
    worktree already exists we surface the git error rather than overwrite."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    return _run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=repo_path,
    )


def prepare_ticket_worktree(repo_path: Path, worktree_path: Path, branch: str) -> GitResult:
    """Prepare the ticket worktree without destructive cleanup.

    Reruns may find either the intended worktree or the ticket branch already
    present. Reuse only the exact expected path on the exact expected branch;
    otherwise return a clear error so a human can repair the repo state.
    """
    if worktree_path.exists():
        return _validate_existing_ticket_worktree(repo_path, worktree_path, branch)

    existing = _worktree_path_for_branch(repo_path, branch)
    if existing is not None and existing.resolve() != worktree_path.resolve():
        return GitResult(
            False,
            "",
            (
                f"ticket branch {branch!r} is already checked out at {existing}, "
                f"not the expected ticket worktree {worktree_path}. "
                "Preserving isolation: move the Linear issue to Human Input Needed "
                "and inspect the existing worktree manually. The executor will not "
                "delete or move worktrees automatically."
            ),
        )

    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_path, branch):
        return _run(["git", "worktree", "add", str(worktree_path), branch], cwd=repo_path)
    return create_worktree(repo_path, worktree_path, branch)


def _validate_existing_ticket_worktree(repo_path: Path, worktree_path: Path, branch: str) -> GitResult:
    if not worktree_path.is_dir():
        return _invalid_worktree_path(worktree_path, "path exists but is not a directory")

    if not is_git_repo(worktree_path):
        return _invalid_worktree_path(worktree_path, "path is not a usable git worktree")

    repo_common = _git_common_dir(repo_path)
    worktree_common = _git_common_dir(worktree_path)
    if repo_common is None or worktree_common is None or repo_common != worktree_common:
        return _invalid_worktree_path(worktree_path, "path belongs to a different git repository")

    current = _current_branch(worktree_path)
    if current != branch:
        return _invalid_worktree_path(
            worktree_path,
            f"path is checked out on branch {current!r}, expected {branch!r}",
        )

    if not working_tree_clean(worktree_path):
        return GitResult(
            False,
            "",
            (
                f"ticket worktree {worktree_path} exists for {branch!r} but has "
                "uncommitted changes. Commit, stash, or move those changes manually, "
                "then rerun the executor. The executor will not delete or overwrite "
                "dirty worktrees automatically."
            ),
        )

    return GitResult(True, f"reusing existing worktree {worktree_path}", "")


def _invalid_worktree_path(worktree_path: Path, reason: str) -> GitResult:
    return GitResult(
        False,
        "",
        (
            f"stale or invalid ticket worktree path exists at {worktree_path}: {reason}. "
            "Move it aside, repair it, or remove it manually before rerunning. "
            "The executor will not delete worktrees automatically."
        ),
    )


def _branch_exists(repo_path: Path, branch: str) -> bool:
    return _run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path).ok


def _current_branch(repo_path: Path) -> str | None:
    res = _run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_path)
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    return None


def _git_common_dir(repo_path: Path) -> Path | None:
    res = _run(["git", "rev-parse", "--git-common-dir"], cwd=repo_path)
    if not res.ok or not res.stdout.strip():
        return None
    common = Path(res.stdout.strip())
    if not common.is_absolute():
        common = repo_path / common
    return common.resolve()


def _worktree_path_for_branch(repo_path: Path, branch: str) -> Path | None:
    res = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_path)
    if not res.ok:
        return None

    current_path: Path | None = None
    wanted = f"refs/heads/{branch}"
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
        elif line == f"branch {wanted}" and current_path is not None:
            return current_path
        elif not line:
            current_path = None
    return None


def commit_all(worktree_path: Path, message: str) -> GitResult:
    """Stage all changes and commit. Returns ok=True even if there's nothing to commit."""
    add = _run(["git", "add", "-A"], cwd=worktree_path)
    if not add.ok:
        return add
    status = _run(["git", "status", "--porcelain"], cwd=worktree_path)
    if not status.stdout.strip():
        return GitResult(True, "(nothing to commit)", "")
    return _run(
        ["git", "-c", f"user.email={GIT_AUTHOR_EMAIL}",
         "-c", f"user.name={GIT_AUTHOR_NAME}",
         "commit", "-m", message],
        cwd=worktree_path,
    )


def status(worktree_path: Path) -> str:
    return _run(["git", "status", "--short"], cwd=worktree_path).stdout


def diff(worktree_path: Path, base: str) -> str:
    """Diff the worktree's branch against ``base`` (e.g. main)."""
    return _run(["git", "diff", f"{base}...HEAD"], cwd=worktree_path).stdout


def shortlog(worktree_path: Path, base: str) -> str:
    return _run(["git", "log", "--oneline", f"{base}..HEAD"], cwd=worktree_path).stdout


def merge(repo_path: Path, branch: str, into: str) -> GitResult:
    """Switch the main repo to ``into`` and merge ``branch``. No fast-forward
    so the branch history is preserved in main's log."""
    co = _run(["git", "checkout", into], cwd=repo_path)
    if not co.ok:
        return co
    return _run(["git", "merge", "--no-ff", "-m", f"Merge {branch}", branch], cwd=repo_path)


def push(repo_path: Path, branch: str) -> GitResult:
    return _run(["git", "push", "origin", branch], cwd=repo_path, timeout=120)


def has_remote(repo_path: Path, name: str = "origin") -> bool:
    res = _run(["git", "remote", "get-url", name], cwd=repo_path)
    return res.ok


def remove_worktree(repo_path: Path, worktree_path: Path) -> GitResult:
    return _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_path)


def delete_branch(repo_path: Path, branch: str) -> GitResult:
    return _run(["git", "branch", "-D", branch], cwd=repo_path)


def working_tree_clean(repo_path: Path) -> bool:
    return not _run(["git", "status", "--porcelain"], cwd=repo_path).stdout.strip()
