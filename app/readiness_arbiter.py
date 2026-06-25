"""Local Draft PR readiness arbiter.

The arbiter is intentionally advisory: it reads local git state and optional
evidence files, then emits a structured recommendation for the controller or a
human to enforce later.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


Decision = Literal[
    "local_child_ready",
    "needs_local_review",
    "human_review_required",
    "paid_public_review_candidate",
    "human_input_needed",
]
Risk = Literal["low", "medium", "high"]
Complexity = Literal["low", "medium", "high"]

BAS_106_ATTACHMENT_FILTER_COMMIT = "60f89bd"

DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".cs", ".java", ".sh", ".ps1"}
CONFIG_FILENAMES = {
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}
RISK_PATH_PARTS = {
    "auth",
    "billing",
    "cancel",
    "deploy",
    "filter",
    "git_ops",
    "linear_api",
    "main",
    "orchestrator",
    "permissions",
    "queue",
    "runner",
    "security",
    "signature",
    "worker",
}
RISK_MODULE_PATHS = {
    "app/attachments.py",
    "app/cli_registry.py",
    "app/filter.py",
    "app/git_ops.py",
    "app/linear_api.py",
    "app/main.py",
    "app/orchestrator.py",
    "app/queue.py",
    "app/readiness_arbiter.py",
    "app/runner.py",
    "app/signature.py",
    "app/worker.py",
}
ARTIFACT_PATTERNS = (
    ".env",
    "jobs.db",
    "webhook.log",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
    "state/",
    "proxy-outputs/",
    ".claude/",
)
PASS_PATTERNS = ("tests passed", "pytest passed", "all tests passed", "exit code 0")
FAIL_PATTERNS = ("tests failed", "pytest failed", "exit code 1", "traceback", "failed checks")
MISSING_TEST_PATTERNS = ("tests not run", "not run", "missing tests", "skipped tests", "no tests")
PASS_REGEXES = (r"\b\d+\s+passed\b",)
FAIL_REGEXES = (r"\b\d+\s+failed\b", r"\b\d+\s+errors?\b")


@dataclass(frozen=True)
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReadinessDecision:
    ticket: str
    decision: Decision
    risk: Risk
    complexity: Complexity
    diff: DiffStats
    reasons: list[str]
    required_next_steps: list[str]
    review_recommendation: str
    paid_review: str
    markdown_summary: str

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["diff"] = asdict(self.diff)
        return data


@dataclass(frozen=True)
class ChangedFile:
    path: str
    insertions: int
    deletions: int


@dataclass(frozen=True)
class RegistryEvidence:
    local_review_ran: bool = False
    accepted_findings_unresolved: bool = False


@dataclass(frozen=True)
class EvidenceSignals:
    tests_passed: bool = False
    tests_failed: bool = False
    tests_missing: bool = False
    local_review_ran: bool = False
    accepted_findings_unresolved: bool = False
    suspicious_artifacts: list[str] = field(default_factory=list)


def _run_git(args: list[str], cwd: Path, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_ok(args: list[str], cwd: Path) -> bool:
    return _run_git(args, cwd).returncode == 0


def _git_text(args: list[str], cwd: Path) -> str:
    result = _run_git(args, cwd)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout


def _parse_numstat(output: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        insertions = 0 if parts[0] == "-" else int(parts[0])
        deletions = 0 if parts[1] == "-" else int(parts[1])
        files.append(ChangedFile(path=parts[2], insertions=insertions, deletions=deletions))
    return files


def _untracked_files(repo_path: Path) -> list[ChangedFile]:
    output = _git_text(["ls-files", "--others", "--exclude-standard"], repo_path)
    files: list[ChangedFile] = []
    for raw_path in output.splitlines():
        path = raw_path.strip()
        if not path:
            continue
        full_path = repo_path / path
        if full_path.is_file():
            try:
                insertions = len(full_path.read_bytes().splitlines())
            except OSError:
                insertions = 0
            files.append(ChangedFile(path=path, insertions=insertions, deletions=0))
    return files


def _diff_files(repo_path: Path, base_ref: str, candidate_ref: str) -> list[ChangedFile]:
    if candidate_ref == "HEAD":
        output = _git_text(["diff", "--numstat", base_ref], repo_path)
        tracked = _parse_numstat(output)
        tracked_paths = {file.path for file in tracked}
        untracked = [file for file in _untracked_files(repo_path) if file.path not in tracked_paths]
        return tracked + untracked

    output = _git_text(["diff", "--numstat", f"{base_ref}...{candidate_ref}"], repo_path)
    return _parse_numstat(output)


def _worktree_dirty(repo_path: Path) -> bool:
    output = _git_text(["status", "--porcelain", "--untracked-files=normal"], repo_path)
    return bool(output.strip())


def _category_for(path: str) -> str:
    normalized = path.replace("\\", "/")
    name = Path(normalized).name
    suffix = Path(normalized).suffix.lower()
    if normalized.startswith("tests/") or name.startswith("test_"):
        return "tests"
    if suffix in DOC_EXTENSIONS or normalized.startswith("docs/"):
        return "docs"
    if name in CONFIG_FILENAMES or normalized.startswith(".github/"):
        return "config"
    if suffix in CODE_EXTENSIONS:
        return "code"
    return "other"


def _diff_stats(files: list[ChangedFile]) -> DiffStats:
    categories = sorted({_category_for(file.path) for file in files})
    return DiffStats(
        files_changed=len(files),
        insertions=sum(file.insertions for file in files),
        deletions=sum(file.deletions for file in files),
        categories=categories,
    )


def _is_docs_only(stats: DiffStats) -> bool:
    return bool(stats.categories) and set(stats.categories) <= {"docs"}


def _is_runtime_sensitive(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    if normalized in RISK_MODULE_PATHS:
        return True
    parts = set(re.split(r"[/._-]+", normalized))
    if normalized.startswith("app/") and (parts & RISK_PATH_PARTS):
        return True
    return bool(parts & {"auth", "billing", "deploy", "merge", "permission", "permissions", "secret", "security"})


def _artifact_hits(text: str) -> list[str]:
    lowered = text.lower().replace("\\", "/")
    return sorted({pattern for pattern in ARTIFACT_PATTERNS if pattern.lower() in lowered})


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in patterns)


def _matches_evidence(text: str, patterns: tuple[str, ...], regexes: tuple[str, ...]) -> bool:
    return _contains_any(text, patterns) or any(re.search(pattern, text, re.IGNORECASE) for pattern in regexes)


def _read_optional_text(path: Path | None, inline_text: str | None) -> str:
    chunks: list[str] = []
    if path is not None:
        chunks.append(path.read_text(encoding="utf-8"))
    if inline_text:
        chunks.append(inline_text)
    return "\n".join(chunks)


def _walk_registry(value: Any) -> RegistryEvidence:
    local_review_ran = False
    accepted_findings_unresolved = False

    def visit(node: Any) -> None:
        nonlocal local_review_ran, accepted_findings_unresolved
        if isinstance(node, dict):
            lowered = {str(k).lower(): v for k, v in node.items()}
            if lowered.get("local_review_ran") is True:
                local_review_ran = True
            if str(lowered.get("local_review_status", "")).lower() in {"passed", "complete", "completed"}:
                local_review_ran = True
            if str(lowered.get("review_status", "")).lower() in {"passed", "complete", "completed"}:
                local_review_ran = True
            unresolved = lowered.get("accepted_findings_unresolved")
            if unresolved is True or (isinstance(unresolved, int) and unresolved > 0):
                accepted_findings_unresolved = True
            for key in ("unresolved_accepted_findings", "accepted_findings"):
                items = lowered.get(key)
                if isinstance(items, list) and items:
                    accepted_findings_unresolved = True
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return RegistryEvidence(local_review_ran, accepted_findings_unresolved)


def _registry_evidence(path: Path | None, ticket: str) -> RegistryEvidence:
    if path is None:
        return RegistryEvidence()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    ticket_key = ticket.lower()
    if isinstance(parsed, dict):
        for key in ("tickets", "issues", "branches"):
            records = parsed.get(key)
            if isinstance(records, dict):
                for record_key, record_value in records.items():
                    if str(record_key).lower() == ticket_key:
                        return _walk_registry(record_value)
        for record_key, record_value in parsed.items():
            if str(record_key).lower() == ticket_key:
                return _walk_registry(record_value)
    return RegistryEvidence()


def _evidence_signals(
    *,
    evidence_path: Path | None,
    evidence_text: str | None,
    registry_path: Path | None,
    ticket: str,
) -> EvidenceSignals:
    evidence = _read_optional_text(evidence_path, evidence_text)
    registry = _registry_evidence(registry_path, ticket)
    accepted_unresolved = registry.accepted_findings_unresolved or "accepted findings unresolved" in evidence.lower()
    local_review_ran = registry.local_review_ran or "local review passed" in evidence.lower()
    tests_failed = _matches_evidence(evidence, FAIL_PATTERNS, FAIL_REGEXES)
    tests_passed = _matches_evidence(evidence, PASS_PATTERNS, PASS_REGEXES) and not tests_failed
    return EvidenceSignals(
        tests_passed=tests_passed,
        tests_failed=tests_failed,
        tests_missing=_contains_any(evidence, MISSING_TEST_PATTERNS),
        local_review_ran=local_review_ran,
        accepted_findings_unresolved=accepted_unresolved,
        suspicious_artifacts=_artifact_hits(evidence),
    )


def _complexity(stats: DiffStats) -> Complexity:
    total = stats.insertions + stats.deletions
    if stats.files_changed >= 20 or total >= 800:
        return "high"
    if stats.files_changed >= 6 or total >= 200 or "code" in stats.categories:
        return "medium"
    return "low"


def _risk(files: list[ChangedFile], stats: DiffStats, evidence: EvidenceSignals) -> Risk:
    if evidence.tests_failed or evidence.accepted_findings_unresolved:
        return "high"
    if any(_is_runtime_sensitive(file.path) for file in files):
        return "high"
    if "code" in stats.categories or "config" in stats.categories:
        return "medium"
    return "low"


def _review_recommendation(decision: Decision, risk: Risk) -> str:
    if decision == "local_child_ready":
        return "none"
    if decision == "needs_local_review":
        return "local_general" if risk != "high" else "local_adversarial"
    if decision == "paid_public_review_candidate":
        return "human_then_paid_public"
    return "human"


def _paid_review(decision: Decision, complexity: Complexity, risk: Risk) -> str:
    _ = (complexity, risk)
    if decision == "paid_public_review_candidate":
        return "candidate_after_human_approval"
    return "not_recommended"


def _markdown_summary(result: ReadinessDecision) -> str:
    lines = [
        "**Findings**",
    ]
    if result.reasons:
        lines.extend(f"- {reason}" for reason in result.reasons)
    else:
        lines.append("- No blocking findings detected.")
    lines.extend(
        [
            "",
            f"**Decision:** `{result.decision}`",
            f"**Risk:** `{result.risk}`  **Complexity:** `{result.complexity}`",
            "",
            "**Required Next Steps**",
        ]
    )
    if result.required_next_steps:
        lines.extend(f"- {step}" for step in result.required_next_steps)
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "**Diff**",
            f"- Files changed: {result.diff.files_changed}",
            f"- Insertions/deletions: +{result.diff.insertions}/-{result.diff.deletions}",
            f"- Categories: {', '.join(result.diff.categories) if result.diff.categories else '(none)'}",
            "",
            f"**Review Recommendation:** `{result.review_recommendation}`",
            f"**Paid Review:** `{result.paid_review}`",
        ]
    )
    return "\n".join(lines)


def evaluate_readiness(
    *,
    ticket: str,
    base_ref: str,
    candidate_ref: str = "HEAD",
    repo_path: Path | None = None,
    evidence_path: Path | None = None,
    evidence_text: str | None = None,
    integration_registry_path: Path | None = None,
) -> ReadinessDecision:
    repo_path = repo_path or Path.cwd()
    reasons: list[str] = []
    required_next_steps: list[str] = []

    if not _git_ok(["rev-parse", "--git-dir"], repo_path):
        stats = DiffStats(categories=[])
        result = ReadinessDecision(
            ticket=ticket,
            decision="human_input_needed",
            risk="high",
            complexity="low",
            diff=stats,
            reasons=[f"{repo_path} is not a git repository."],
            required_next_steps=["Run the arbiter from a candidate git worktree or pass --repo-path."],
            review_recommendation="human",
            paid_review="not_recommended",
            markdown_summary="",
        )
        return _with_markdown(result)

    if not _git_ok(["merge-base", "--is-ancestor", base_ref, candidate_ref], repo_path):
        stats = DiffStats(categories=[])
        result = ReadinessDecision(
            ticket=ticket,
            decision="human_input_needed",
            risk="high",
            complexity="low",
            diff=stats,
            reasons=[f"Candidate ref `{candidate_ref}` is not stacked on base `{base_ref}`."],
            required_next_steps=["Rebase or merge the required base before readiness arbitration."],
            review_recommendation="human",
            paid_review="not_recommended",
            markdown_summary="",
        )
        return _with_markdown(result)

    files = _diff_files(repo_path, base_ref, candidate_ref)
    stats = _diff_stats(files)
    dirty_explicit_candidate = candidate_ref != "HEAD" and _worktree_dirty(repo_path)
    evidence = _evidence_signals(
        evidence_path=evidence_path,
        evidence_text=evidence_text,
        registry_path=integration_registry_path,
        ticket=ticket,
    )
    complexity = _complexity(stats)
    risk = _risk(files, stats, evidence)
    docs_only = _is_docs_only(stats)
    code_or_config = bool({"code", "config"} & set(stats.categories))
    changed_artifacts = sorted({hit for file in files for hit in _artifact_hits(file.path)})
    artifact_hits = sorted(set(changed_artifacts + evidence.suspicious_artifacts))
    has_bas_106 = _git_ok(["merge-base", "--is-ancestor", BAS_106_ATTACHMENT_FILTER_COMMIT, candidate_ref], repo_path)

    if not files:
        reasons.append("No candidate diff was found against the requested base.")
        required_next_steps.append("Confirm the base and candidate refs point at the intended branches.")

    if dirty_explicit_candidate:
        reasons.append("Explicit candidate ref was evaluated while the worktree had uncommitted or untracked changes.")
        required_next_steps.append("Commit, stash, or rerun with the default HEAD candidate so dirty worktree changes are included.")

    if "ticket/bas-" in base_ref.lower() or re.search(r"\bBAS-\d+\b", base_ref, re.IGNORECASE):
        reasons.append(f"Candidate is stacked on ticket base `{base_ref}`.")

    if artifact_hits:
        reasons.append(f"Suspicious local/runtime artifacts were detected: {', '.join(artifact_hits)}.")
        if changed_artifacts or not has_bas_106:
            required_next_steps.append("Remove local/runtime artifacts or prove BAS-106 attachment filtering is present before review.")
        else:
            required_next_steps.append("Human should confirm historical artifact evidence is not part of the candidate branch.")

    if evidence.tests_failed:
        reasons.append("Evidence reports failed checks or test failures.")
        required_next_steps.append("Fix failing checks and rerun verification before integration.")
    elif evidence.tests_missing and code_or_config:
        reasons.append("Code or config changed without passing test evidence.")
        required_next_steps.append("Run targeted tests or explain why tests are not applicable.")
    elif code_or_config and not evidence.tests_passed:
        reasons.append("Code or config changed but no passing test evidence was provided.")
        required_next_steps.append("Provide passing local test evidence before readiness is trusted.")

    if evidence.accepted_findings_unresolved:
        reasons.append("Accepted local review findings remain unresolved.")
        required_next_steps.append("Resolve accepted findings or mark them explicitly waived by a human.")

    sensitive_files = [file.path for file in files if _is_runtime_sensitive(file.path)]
    if sensitive_files:
        reasons.append(f"Runtime/control-plane sensitive files changed: {', '.join(sensitive_files[:5])}.")
        required_next_steps.append("Require BASIC/human inspection for control-plane, auth, runtime, or merge behavior changes.")

    if complexity == "high":
        reasons.append("Large diff exceeds local child-review thresholds.")
        required_next_steps.append("Batch into a coherent integration review and get human approval before any paid/public review.")

    if docs_only and complexity == "low" and not evidence.tests_failed and not artifact_hits:
        reasons.append("Small docs-only diff is suitable for local child integration.")

    if evidence.local_review_ran:
        reasons.append("Local review evidence indicates review already ran.")

    decision: Decision
    tests_not_trusted = code_or_config and not evidence.tests_passed
    if (
        dirty_explicit_candidate
        or evidence.tests_failed
        or evidence.accepted_findings_unresolved
        or (artifact_hits and (changed_artifacts or not has_bas_106))
        or not files
    ):
        decision = "human_input_needed"
    elif artifact_hits:
        decision = "human_review_required"
    elif tests_not_trusted:
        decision = "needs_local_review"
    elif complexity == "high" and risk in {"medium", "high"}:
        decision = "paid_public_review_candidate"
    elif risk == "high":
        decision = "human_review_required"
    elif code_or_config and not evidence.local_review_ran:
        decision = "needs_local_review"
    else:
        decision = "local_child_ready"

    if decision == "needs_local_review" and "Run local agentic review before integration." not in required_next_steps:
        required_next_steps.append("Run local agentic review before integration.")
    if decision == "human_review_required" and "BASIC/human should inspect before merge or public PR." not in required_next_steps:
        required_next_steps.append("BASIC/human should inspect before merge or public PR.")

    result = ReadinessDecision(
        ticket=ticket,
        decision=decision,
        risk=risk,
        complexity=complexity,
        diff=stats,
        reasons=_dedupe(reasons),
        required_next_steps=_dedupe(required_next_steps),
        review_recommendation=_review_recommendation(decision, risk),
        paid_review=_paid_review(decision, complexity, risk),
        markdown_summary="",
    )
    return _with_markdown(result)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _with_markdown(result: ReadinessDecision) -> ReadinessDecision:
    return ReadinessDecision(
        ticket=result.ticket,
        decision=result.decision,
        risk=result.risk,
        complexity=result.complexity,
        diff=result.diff,
        reasons=result.reasons,
        required_next_steps=result.required_next_steps,
        review_recommendation=result.review_recommendation,
        paid_review=result.paid_review,
        markdown_summary=_markdown_summary(result),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate local Draft PR readiness for a ticket branch.")
    parser.add_argument("--ticket", required=True, help="Ticket key, for example BAS-112")
    parser.add_argument("--base", required=True, help="Base ref or commit to diff from")
    parser.add_argument("--candidate", default="HEAD", help="Candidate ref to evaluate, defaults to HEAD")
    parser.add_argument("--repo-path", default=".", type=Path, help="Candidate git worktree path")
    parser.add_argument("--evidence", type=Path, help="Optional evidence summary/comment body path")
    parser.add_argument("--evidence-text", help="Optional inline evidence summary/comment body")
    parser.add_argument("--integration-registry", type=Path, help="Optional local integration registry JSON path")
    parser.add_argument("--json-output", type=Path, help="Optional file path for JSON output")
    parser.add_argument("--markdown-output", type=Path, help="Optional file path for Markdown output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = evaluate_readiness(
            ticket=args.ticket,
            base_ref=args.base,
            candidate_ref=args.candidate,
            repo_path=args.repo_path,
            evidence_path=args.evidence,
            evidence_text=args.evidence_text,
            integration_registry_path=args.integration_registry,
        )
    except Exception as exc:
        print(f"readiness arbiter failed: {exc}", file=sys.stderr)
        return 2

    json_text = json.dumps(result.to_json_dict(), indent=2, sort_keys=True)
    if args.json_output:
        args.json_output.write_text(json_text + "\n", encoding="utf-8")
    if args.markdown_output:
        args.markdown_output.write_text(result.markdown_summary + "\n", encoding="utf-8")
    print(json_text)
    print()
    print(result.markdown_summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
