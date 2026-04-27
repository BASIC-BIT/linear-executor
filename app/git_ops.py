"""Thin subprocess-based wrapper around git for the executor.

Phase 3b: Stage 1 creates a worktree per ticket so Claude works on an isolated
branch; Stage 2 merges that branch back to main, pushes, and cleans up.

All functions take explicit paths and never raise on git failure — they return
``GitResult`` so the caller can decide how to react (retry, comment, status
rollback, etc.).
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("linear-executor")


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


def commit_all(worktree_path: Path, message: str) -> GitResult:
    """Stage all changes and commit. Returns ok=True even if there's nothing to commit."""
    add = _run(["git", "add", "-A"], cwd=worktree_path)
    if not add.ok:
        return add
    status = _run(["git", "status", "--porcelain"], cwd=worktree_path)
    if not status.stdout.strip():
        return GitResult(True, "(nothing to commit)", "")
    return _run(
        ["git", "-c", "user.email=executor@ai-devhub-247.site",
         "-c", "user.name=Linear-Executor",
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
