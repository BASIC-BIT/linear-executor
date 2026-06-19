import json
import subprocess
from pathlib import Path

from app.readiness_arbiter import evaluate_readiness, main


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _commit(repo, "init")
    return repo


def _branch(repo: Path, name: str, base: str = "main") -> None:
    _git(repo, "checkout", "-b", name, base)


def test_docs_only_small_diff_is_local_child_ready(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-docs")
    (repo / "docs").mkdir()
    (repo / "docs" / "workflow.md").write_text("# Workflow\n", encoding="utf-8")
    _commit(repo, "docs")

    result = evaluate_readiness(ticket="BAS-109", base_ref="main", repo_path=repo)

    assert result.decision == "local_child_ready"
    assert result.risk == "low"
    assert result.diff.categories == ["docs"]
    assert "Small docs-only diff" in "\n".join(result.reasons)


def test_default_head_candidate_includes_untracked_worktree_files(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-untracked")
    (repo / "docs").mkdir()
    (repo / "docs" / "untracked.md").write_text("# Untracked\n", encoding="utf-8")

    result = evaluate_readiness(ticket="BAS-109", base_ref="main", repo_path=repo)

    assert result.decision == "local_child_ready"
    assert result.diff.files_changed == 1
    assert result.diff.categories == ["docs"]


def test_runtime_code_without_local_review_needs_local_review(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-runtime")
    (repo / "app").mkdir()
    (repo / "app" / "registry.py").write_text("REGISTRY = {}\n", encoding="utf-8")
    _commit(repo, "runtime")

    result = evaluate_readiness(
        ticket="BAS-110",
        base_ref="main",
        repo_path=repo,
        evidence_text="pytest passed",
    )

    assert result.decision == "needs_local_review"
    assert result.risk == "medium"
    assert result.review_recommendation == "local_general"


def test_stacked_branch_reports_ticket_base(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-106")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_filter.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _commit(repo, "base ticket")
    _branch(repo, "ticket/BAS-112", "ticket/BAS-106")
    (repo / "docs").mkdir()
    (repo / "docs" / "arbiter.md").write_text("# Arbiter\n", encoding="utf-8")
    _commit(repo, "arbiter docs")

    result = evaluate_readiness(ticket="BAS-112", base_ref="ticket/BAS-106", repo_path=repo)

    assert result.decision == "local_child_ready"
    assert any("ticket/BAS-106" in reason for reason in result.reasons)


def test_missing_tests_for_code_change_requires_local_review(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-missing-tests")
    (repo / "app").mkdir()
    (repo / "app" / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _commit(repo, "code")

    result = evaluate_readiness(
        ticket="BAS-110",
        base_ref="main",
        repo_path=repo,
        evidence_text="tests not run",
    )

    assert result.decision == "needs_local_review"
    assert any("without passing test evidence" in reason for reason in result.reasons)
    assert any("Run targeted tests" in step for step in result.required_next_steps)


def test_failed_tests_block_with_human_input_needed(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-failed")
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("# x\n", encoding="utf-8")
    _commit(repo, "docs")

    result = evaluate_readiness(
        ticket="BAS-108",
        base_ref="main",
        repo_path=repo,
        evidence_text="pytest failed: 1 failed",
    )

    assert result.decision == "human_input_needed"
    assert result.risk == "high"
    assert any("failed" in reason.lower() for reason in result.reasons)


def test_artifact_leak_blocks_with_human_input_needed(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-artifact")
    (repo / "jobs.db").write_bytes(b"sqlite")
    _commit(repo, "leak artifact")

    result = evaluate_readiness(ticket="BAS-112", base_ref="main", repo_path=repo)

    assert result.decision == "human_input_needed"
    assert any("jobs.db" in reason for reason in result.reasons)


def test_large_runtime_diff_is_paid_public_review_candidate(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-large")
    (repo / "app").mkdir()
    large = "\n".join(f"VALUE_{i} = {i}" for i in range(900)) + "\n"
    (repo / "app" / "generated_runtime.py").write_text(large, encoding="utf-8")
    _commit(repo, "large runtime")

    result = evaluate_readiness(
        ticket="BAS-108",
        base_ref="main",
        repo_path=repo,
        evidence_text="tests passed; local review passed",
    )

    assert result.decision == "paid_public_review_candidate"
    assert result.complexity == "high"
    assert result.paid_review == "candidate_after_human_approval"


def test_sensitive_control_plane_file_requires_human_review(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-sensitive")
    (repo / "app").mkdir()
    (repo / "app" / "orchestrator.py").write_text("def merge_gate():\n    return True\n", encoding="utf-8")
    _commit(repo, "orchestrator")

    result = evaluate_readiness(
        ticket="BAS-108",
        base_ref="main",
        repo_path=repo,
        evidence_text="tests passed; local review passed",
    )

    assert result.decision == "human_review_required"
    assert result.review_recommendation == "human"


def test_integration_registry_can_record_completed_local_review(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-reviewed")
    (repo / "app").mkdir()
    (repo / "app" / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    _commit(repo, "code")
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"tickets": {"BAS-reviewed": {"local_review_status": "passed"}}}),
        encoding="utf-8",
    )

    result = evaluate_readiness(
        ticket="BAS-110",
        base_ref="main",
        repo_path=repo,
        evidence_text="tests passed",
        integration_registry_path=registry,
    )

    assert result.decision == "local_child_ready"
    assert any("Local review evidence" in reason for reason in result.reasons)


def test_cli_writes_json_and_markdown_outputs(tmp_path):
    repo = _make_repo(tmp_path)
    _branch(repo, "ticket/BAS-docs")
    (repo / "README.md").write_text("hello\nupdated\n", encoding="utf-8")
    _commit(repo, "docs")
    json_out = tmp_path / "readiness.json"
    md_out = tmp_path / "readiness.md"

    exit_code = main(
        [
            "--ticket",
            "BAS-109",
            "--base",
            "main",
            "--repo-path",
            str(repo),
            "--json-output",
            str(json_out),
            "--markdown-output",
            str(md_out),
        ]
    )

    assert exit_code == 0
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert data["decision"] == "local_child_ready"
    assert "**Findings**" in md_out.read_text(encoding="utf-8")
