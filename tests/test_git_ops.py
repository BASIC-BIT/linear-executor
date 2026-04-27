"""Tests for git_ops use real temporary git repos. Faster than mocking git
behavior, and gives us actual confidence that our subprocess invocations work."""
from pathlib import Path

import pytest

from app import git_ops


@pytest.fixture
def repo(tmp_path):
    """Create a tiny initialized git repo with one initial commit on 'main'."""
    r = tmp_path / "origin"
    r.mkdir()
    git_ops._run(["git", "init", "-b", "main"], cwd=r)
    git_ops._run(["git", "config", "user.email", "test@example.com"], cwd=r)
    git_ops._run(["git", "config", "user.name", "Test"], cwd=r)
    (r / "README.md").write_text("hello\n")
    git_ops._run(["git", "add", "-A"], cwd=r)
    git_ops._run(["git", "commit", "-m", "init"], cwd=r)
    return r


def test_is_git_repo_true_for_real_repo(repo):
    assert git_ops.is_git_repo(repo) is True


def test_is_git_repo_false_for_plain_dir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_ops.is_git_repo(plain) is False


def test_is_git_repo_false_for_missing_path(tmp_path):
    assert git_ops.is_git_repo(tmp_path / "missing") is False


def test_default_branch_returns_current(repo):
    assert git_ops.default_branch(repo) == "main"


def test_create_worktree_makes_branch_and_dir(repo, tmp_path):
    wt = tmp_path / "wt"
    res = git_ops.create_worktree(repo, wt, "ticket/TES-1")
    assert res.ok, res.stderr
    assert wt.exists()
    assert (wt / "README.md").exists()
    # Branch is checked out in worktree
    assert git_ops.default_branch(wt) == "ticket/TES-1"


def test_commit_all_creates_commit_when_changes(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-2")
    (wt / "feature.txt").write_text("new file\n")
    res = git_ops.commit_all(wt, "Add feature")
    assert res.ok, res.stderr
    log = git_ops.shortlog(wt, base="main")
    assert "Add feature" in log


def test_commit_all_noop_when_clean(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-3")
    res = git_ops.commit_all(wt, "Noop")
    assert res.ok
    assert "nothing to commit" in res.stdout


def test_diff_shows_branch_changes_against_main(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-4")
    (wt / "x.txt").write_text("hello world\n")
    git_ops.commit_all(wt, "Add x.txt")
    d = git_ops.diff(wt, base="main")
    assert "x.txt" in d
    assert "hello world" in d


def test_merge_brings_branch_into_main(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-5")
    (wt / "merged.txt").write_text("merged\n")
    git_ops.commit_all(wt, "Add merged.txt")

    res = git_ops.merge(repo, "ticket/TES-5", into="main")
    assert res.ok, res.stderr
    assert (repo / "merged.txt").exists()


def test_merge_returns_failure_on_conflict(repo, tmp_path):
    # Both main and branch touch README.md → conflict
    (repo / "README.md").write_text("from main\n")
    git_ops.commit_all(repo, "main update")

    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-conflict")
    # Branch changes the file differently, branched from older commit (the new branch points to current main HEAD though;
    # to force conflict, change in branch then go back and change in main)
    (wt / "README.md").write_text("from branch\n")
    git_ops.commit_all(wt, "branch update")

    # Now move main forward divergently
    (repo / "README.md").write_text("from main again\n")
    git_ops.commit_all(repo, "main update 2")

    res = git_ops.merge(repo, "ticket/TES-conflict", into="main")
    assert res.ok is False
    assert "conflict" in (res.stdout + res.stderr).lower()


def test_remove_worktree_cleans_up(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-6")
    assert wt.exists()
    res = git_ops.remove_worktree(repo, wt)
    assert res.ok
    assert not wt.exists()


def test_delete_branch_removes_it(repo, tmp_path):
    wt = tmp_path / "wt"
    git_ops.create_worktree(repo, wt, "ticket/TES-7")
    git_ops.remove_worktree(repo, wt)  # have to remove worktree before deleting branch
    res = git_ops.delete_branch(repo, "ticket/TES-7")
    assert res.ok


def test_has_remote_false_for_repo_without_origin(repo):
    assert git_ops.has_remote(repo) is False


def test_working_tree_clean(repo):
    assert git_ops.working_tree_clean(repo) is True
    (repo / "dirty.txt").write_text("dirty\n")
    assert git_ops.working_tree_clean(repo) is False
