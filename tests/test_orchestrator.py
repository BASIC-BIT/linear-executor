from pathlib import Path

import pytest

from app import orchestrator
from app.linear_api import Attachment, Comment


def _payload(identifier="TES-700", issue_id="issue-uuid", project_name="Radio MCP",
             description="", title="A test ticket", state_name="AI Implementation"):
    return {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": title,
            "description": description,
            "project": {"name": project_name},
            "state": {"name": state_name},
        },
        "updatedFrom": {"stateId": "prev"},
    }


@pytest.fixture
def patched_orchestrator(monkeypatch):
    """Replace every external dependency of orchestrator with a controllable stub."""
    state = {
        "fetched_for": [],
        "downloaded_into": [],
        "comments": [],
        "state_changes": [],
    }

    def fake_fetch_attachments(issue_id, client=None):
        state["fetched_for"].append(issue_id)
        return state.get("_attachments_to_return", [])

    def fake_fetch_comments(issue_id, client=None):
        return state.get("_comments_to_return", [])

    def fake_download(att_list, target_dir, api_key=None, client=None):
        state["downloaded_into"].append(target_dir)
        return [target_dir / a.title for a in att_list]

    def fake_post(issue_id, body, client=None):
        state["comments"].append({"issue_id": issue_id, "body": body})
        return f"comment-id-{len(state['comments'])}"

    def fake_state_id(team_id, name, client=None):
        return f"state-id-for-{name}"

    def fake_set_state(issue_id, state_id, client=None):
        state["state_changes"].append({"issue_id": issue_id, "state_id": state_id})
        return True

    monkeypatch.setattr(orchestrator.linear_api, "fetch_issue_attachments", fake_fetch_attachments)
    monkeypatch.setattr(orchestrator.linear_api, "fetch_issue_comments", fake_fetch_comments)
    monkeypatch.setattr(orchestrator.attachments_mod, "download_attachments", fake_download)
    monkeypatch.setattr(orchestrator.linear_api, "post_comment", fake_post)
    monkeypatch.setattr(orchestrator.linear_api, "fetch_workflow_state_id", fake_state_id)
    monkeypatch.setattr(orchestrator.linear_api, "set_issue_state", fake_set_state)
    # Bust the state-id cache so each test starts clean
    monkeypatch.setattr(orchestrator, "_state_id_cache", {})

    return state


def test_stage1_runs_claude_posts_comment_sets_in_review(patched_orchestrator):
    orchestrator.orchestrate_start(_payload(), delivery_id="d-1")

    assert patched_orchestrator["fetched_for"] == ["issue-uuid"]
    assert len(patched_orchestrator["comments"]) == 1
    body = patched_orchestrator["comments"][0]["body"]
    assert "Coding Agent Run" in body
    assert "(mocked claude output)" in body  # from conftest run_cli stub
    assert "TES-700" in body
    assert "d-1" in body
    # Status moved to Draft PR Ready
    assert patched_orchestrator["state_changes"] == [
        {"issue_id": "issue-uuid", "state_id": "state-id-for-Draft PR Ready"}
    ]


def test_planning_state_loads_prompt_and_moves_to_human_design_review(monkeypatch, patched_orchestrator, tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "ai-planning-research.md").write_text("planning sentinel\nDo not edit product code.", encoding="utf-8")
    monkeypatch.setenv("LINEAR_CONTROLLER_PROMPTS_DIR", str(prompts_dir))

    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False, cli=cli)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(
        _payload(identifier="TES-PLAN", state_name="AI Planning & Research"),
        delivery_id="d-plan",
    )

    assert "Workflow state: AI Planning & Research" in captured["prompt"]
    assert "Controller prompt for `AI Planning & Research`" in captured["prompt"]
    assert "planning sentinel" in captured["prompt"]
    assert patched_orchestrator["state_changes"] == [
        {"issue_id": "issue-uuid", "state_id": "state-id-for-Human Design Review"}
    ]
    body = patched_orchestrator["comments"][0]["body"]
    assert "workflow state: `AI Planning & Research`" in body


def test_stage1_with_attachments_downloads_into_per_ticket_folder(patched_orchestrator, tmp_path):
    patched_orchestrator["_attachments_to_return"] = [
        Attachment(id="a1", title="Spec.pdf", url="https://uploads.linear.app/a/Spec.pdf", subtitle=None),
        Attachment(id="a2", title="notes.md", url="https://cdn.example.com/notes.md", subtitle=None),
    ]
    p = _payload(identifier="TES-701", description=f"folder: {tmp_path}")

    orchestrator.orchestrate_start(p, delivery_id="d-2")

    assert patched_orchestrator["downloaded_into"][0] == tmp_path / "linear" / "TES-701" / "attachments"
    body = patched_orchestrator["comments"][0]["body"]
    assert "Spec.pdf" in body
    assert "notes.md" in body


def test_stage1_posts_error_comment_and_reraises(monkeypatch, patched_orchestrator):
    def exploding_fetch(issue_id, client=None):
        raise RuntimeError("boom — graphql down")
    monkeypatch.setattr(orchestrator.linear_api, "fetch_issue_attachments", exploding_fetch)

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator.orchestrate_start(_payload(identifier="TES-702"), delivery_id="d-3")

    # Best-effort error comment still gets posted before the re-raise, so the
    # Linear ticket reflects the failure even though the worker will also
    # record it in the queue.
    assert len(patched_orchestrator["comments"]) == 1
    assert "failed" in patched_orchestrator["comments"][0]["body"].lower()


def test_stage1_backend_nonzero_does_not_advance_state(monkeypatch, patched_orchestrator):
    from app import runner as runner_mod

    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        return runner_mod.RunResult(2, "", "backend exploded", False, cli=cli)

    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    with pytest.raises(RuntimeError, match="exited with code 2"):
        orchestrator.orchestrate_start(_payload(identifier="TES-NONZERO"), delivery_id="d-nonzero")

    assert patched_orchestrator["state_changes"] == []
    body = patched_orchestrator["comments"][0]["body"]
    assert "backend exploded" in body


def test_stage1_opencode_permission_auto_reject_does_not_advance_state(monkeypatch, patched_orchestrator):
    from app import runner as runner_mod

    stderr = "permission requested: external_directory (D:\\*); auto-rejecting\nThe user rejected permission"

    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        return runner_mod.RunResult(0, "", stderr, False, cli="opencode")

    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    with pytest.raises(RuntimeError, match="auto-rejected permission"):
        orchestrator.orchestrate_start(_payload(identifier="TES-PERM"), delivery_id="d-perm")

    assert patched_orchestrator["state_changes"] == []
    body = patched_orchestrator["comments"][0]["body"]
    assert "external_directory" in body


def test_stage1_prompt_carries_progress_instructions_with_issue_id(monkeypatch, patched_orchestrator):
    """Claude needs to know it should post 🔄 progress lines and which Linear issue to post against. (TES-608)"""
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-PROG", issue_id="iss-prog"), delivery_id="d-prog")

    prompt = captured["prompt"]
    assert "Progress updates" in prompt
    assert "🔄" in prompt
    assert "iss-prog" in prompt  # the issue UUID Claude needs for save_comment
    assert "save_comment" in prompt


def test_stage1_non_claude_prompt_omits_claude_linear_mcp_progress(monkeypatch, patched_orchestrator):
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["cli"] = cli
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False, cli=cli)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    payload = _payload(identifier="TES-OPENCODE", issue_id="iss-opencode")
    payload["data"]["labels"] = [{"name": "cli:opencode"}]
    orchestrator.orchestrate_start(payload, delivery_id="d-opencode")

    assert captured["cli"] == "opencode"
    assert "Progress updates" not in captured["prompt"]
    assert "mcp__claude_ai_Linear__save_comment" not in captured["prompt"]


def test_stage1_passes_reasoning_label_to_runner(monkeypatch, patched_orchestrator):
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, reasoning=None, auth_mode="oauth"):
        captured["reasoning"] = reasoning
        return runner_mod.RunResult(0, "ok", "", False, cli=cli)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    payload = _payload(identifier="TES-REASON", issue_id="iss-reason")
    payload["data"]["labels"] = [{"name": "cli:opencode"}, {"name": "reasoning:low"}]
    orchestrator.orchestrate_start(payload, delivery_id="d-reason")

    assert captured["reasoning"] == "low"


def test_proxy_prompt_carries_progress_instructions(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", lambda *a, **kw: "att-x")
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        cwd.mkdir(parents=True, exist_ok=True)
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PXP", issue_id="iss-pxp", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-prog")

    prompt = captured["prompt"]
    assert "Progress updates" in prompt
    assert "iss-pxp" in prompt


def test_proxy_non_claude_prompt_omits_claude_linear_mcp_progress(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["cli"] = cli
        captured["prompt"] = prompt
        cwd.mkdir(parents=True, exist_ok=True)
        return runner_mod.RunResult(0, "ok", "", False, cli=cli)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PX-CODEX", issue_id="iss-codex", project_name="⚡ Ad-hoc AI Proxy")
    p["data"]["labels"] = [{"name": "cli:codex"}]
    orchestrator.orchestrate_proxy(p, delivery_id="px-codex")

    assert captured["cli"] == "codex"
    assert "Progress updates" not in captured["prompt"]
    assert "mcp__claude_ai_Linear__save_comment" not in captured["prompt"]


def test_progress_comments_excluded_from_followup_context(monkeypatch, patched_orchestrator):
    """🔄 comments Claude posted during a previous run must not flow back as follow-up context. (TES-608)"""
    from app.linear_api import Comment as C
    patched_orchestrator["_comments_to_return"] = [
        C(id="c1", body="🔄 Schritt 1: web search starting", created_at="2026-04-25T10:00:00Z",
          author_name="Bastian", is_executor_comment=True),
        C(id="c2", body="Bitte aufpassen dass keine paywalled sources reinkommen",
          created_at="2026-04-25T10:01:00Z", author_name="Bastian", is_executor_comment=False),
    ]
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-FILT"), delivery_id="d-filt")

    prompt = captured["prompt"]
    # Real follow-up still in
    assert "paywalled sources" in prompt
    # 🔄 progress comment filtered out
    assert "Schritt 1: web search starting" not in prompt


def test_stage1_appends_nonexecutor_comments_as_follow_up_context(monkeypatch, patched_orchestrator):
    """Follow-up comments added after ticket creation must reach Claude as context."""
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c1",
            body="Please also handle edge case X",
            created_at="2026-04-20T10:00:00Z",
            author_name="Bastian",
            is_executor_comment=False,
        ),
        Comment(
            id="c2",
            body="\U0001f916 **Linear-Executor** — Claude Code Run\nOutput from last run",
            created_at="2026-04-20T10:05:00Z",
            author_name="Bastian",
            is_executor_comment=True,
        ),
        Comment(
            id="c3",
            body="And retry with timeout 30s",
            created_at="2026-04-20T10:10:00Z",
            author_name="Bastian",
            is_executor_comment=False,
        ),
    ]
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-710"), delivery_id="d-comments")

    prompt = captured["prompt"]
    assert "Follow-up Instructions" in prompt
    assert "Please also handle edge case X" in prompt
    assert "And retry with timeout 30s" in prompt
    # Own executor comments must be filtered out
    assert "Output from last run" not in prompt
    assert "Inherited Context (context:fork)" not in prompt


def test_stage1_context_fork_inherits_previous_executor_output(monkeypatch, patched_orchestrator):
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c1",
            body="\U0001f916 **Linear-Executor** — Coding Agent Run\n\nprevious executor sentinel",
            created_at="2026-06-17T16:00:00Z",
            author_name="Linear-Executor",
            is_executor_comment=True,
        ),
        Comment(
            id="c2",
            body="human follow-up sentinel",
            created_at="2026-06-17T16:05:00Z",
            author_name="Bastian",
            is_executor_comment=False,
        ),
    ]
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    payload = _payload(identifier="TES-FORK")
    payload["data"]["labels"] = [{"name": "context:fork"}]
    orchestrator.orchestrate_start(payload, delivery_id="d-fork")

    prompt = captured["prompt"]
    assert "Inherited Context (context:fork)" in prompt
    assert "You are a fork of previous executor work" in prompt
    assert "previous executor sentinel" in prompt
    assert "human follow-up sentinel" in prompt


def test_stage1_without_follow_up_comments_omits_section(monkeypatch, patched_orchestrator):
    """No non-executor comments → no 'Follow-up Instructions' section in the prompt."""
    patched_orchestrator["_comments_to_return"] = []
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-711"), delivery_id="d-no-comments")

    assert "Follow-up Instructions" not in captured["prompt"]


def test_proxy_appends_nonexecutor_comments_as_follow_up_context(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c1",
            body="Bitte auch auf Deutsch zusammenfassen",
            created_at="2026-04-20T10:00:00Z",
            author_name="Bastian",
            is_executor_comment=False,
        ),
    ]
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PROXY-COMMENTS", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-comments")

    assert "Follow-up Instructions" in captured["prompt"]
    assert "auf Deutsch zusammenfassen" in captured["prompt"]


def test_proxy_context_fork_inherits_previous_executor_output(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c1",
            body="\U0001f916 **Linear-Executor** — Coding Agent Run\n\nproxy inherited sentinel",
            created_at="2026-06-17T16:10:00Z",
            author_name="Linear-Executor",
            is_executor_comment=True,
        ),
    ]
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["prompt"] = prompt
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PROXY-FORK", project_name="⚡ Ad-hoc AI Proxy")
    p["data"]["labels"] = [{"name": "context:fork"}]
    orchestrator.orchestrate_proxy(p, delivery_id="px-fork")

    assert "Inherited Context (context:fork)" in captured["prompt"]
    assert "proxy inherited sentinel" in captured["prompt"]


def test_stage1_without_issue_id_skips_comment_and_state(patched_orchestrator):
    p = _payload()
    p["data"]["id"] = ""
    orchestrator.orchestrate_start(p)

    assert patched_orchestrator["comments"] == []
    assert patched_orchestrator["state_changes"] == []


def test_stage2_posts_confirmation_comment_no_state_change(patched_orchestrator):
    orchestrator.orchestrate_complete(_payload(state_name="Done"), delivery_id="d-4")

    assert len(patched_orchestrator["comments"]) == 1
    assert "Marked Done" in patched_orchestrator["comments"][0]["body"]
    # Stage 2 (no-op merge phase) does not change state itself
    assert patched_orchestrator["state_changes"] == []


def test_review_watch_marks_linked_github_pr_ready(monkeypatch, patched_orchestrator):
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c-pr",
            body="PR is https://github.com/BASIC-BIT/linear-executor/pull/123",
            created_at="2026-06-17T10:00:00Z",
            author_name="Linear-Executor",
            is_executor_comment=True,
        )
    ]
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_run_gh_pr_ready",
        lambda pr_url: calls.append(pr_url) or orchestrator.CommandResult(0, "", ""),
    )

    orchestrator.orchestrate_review_watch(_payload(state_name="AI Review Watch"), delivery_id="d-rw")

    assert calls == ["https://github.com/BASIC-BIT/linear-executor/pull/123"]
    body = patched_orchestrator["comments"][0]["body"]
    assert "Marked PR ready for review" in body
    assert "gh pr ready https://github.com/BASIC-BIT/linear-executor/pull/123" in body
    assert patched_orchestrator["state_changes"] == []


def test_review_watch_converts_linear_review_url(monkeypatch, patched_orchestrator):
    patched_orchestrator["_comments_to_return"] = [
        Comment(
            id="c-pr",
            body="Review at https://linear.review/BASIC-BIT/linear-executor/pull/456",
            created_at="2026-06-17T10:00:00Z",
            author_name="Linear-Executor",
            is_executor_comment=True,
        )
    ]
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "_run_gh_pr_ready",
        lambda pr_url: calls.append(pr_url) or orchestrator.CommandResult(0, "", ""),
    )

    orchestrator.orchestrate_review_watch(_payload(state_name="AI Review Watch"), delivery_id="d-rw")

    assert calls == ["https://github.com/BASIC-BIT/linear-executor/pull/456"]


def test_review_watch_missing_pr_url_moves_to_human_input(patched_orchestrator):
    orchestrator.orchestrate_review_watch(_payload(state_name="AI Review Watch"), delivery_id="d-rw")

    body = patched_orchestrator["comments"][0]["body"]
    assert "No GitHub pull request URL was found" in body
    assert patched_orchestrator["state_changes"] == [
        {"issue_id": "issue-uuid", "state_id": "state-id-for-Human Input Needed"}
    ]


def test_review_watch_failed_gh_ready_moves_to_human_input(monkeypatch, patched_orchestrator):
    patched_orchestrator["_attachments_to_return"] = [
        Attachment(
            id="att-pr",
            title="PR",
            url="https://github.com/BASIC-BIT/linear-executor/pull/789",
            subtitle=None,
        )
    ]
    monkeypatch.setattr(
        orchestrator,
        "_run_gh_pr_ready",
        lambda pr_url: orchestrator.CommandResult(1, "", "GraphQL: Not Found"),
    )

    orchestrator.orchestrate_review_watch(_payload(state_name="AI Review Watch"), delivery_id="d-rw")

    body = patched_orchestrator["comments"][0]["body"]
    assert "Could not mark PR ready" in body
    assert "GraphQL: Not Found" in body
    assert patched_orchestrator["state_changes"] == [
        {"issue_id": "issue-uuid", "state_id": "state-id-for-Human Input Needed"}
    ]


# --- Phase 3b: git-aware paths --------------------------------------------------


def _make_real_repo(tmp_path):
    """Build a tiny initialized git repo for end-to-end orchestrator tests."""
    from app import git_ops as g
    r = tmp_path / "origin"
    r.mkdir()
    g._run(["git", "init", "-b", "main"], cwd=r)
    g._run(["git", "config", "user.email", "test@example.com"], cwd=r)
    g._run(["git", "config", "user.name", "Test"], cwd=r)
    (r / "README.md").write_text("hello\n")
    g._run(["git", "add", "-A"], cwd=r)
    g._run(["git", "commit", "-m", "init"], cwd=r)
    return r


def test_stage1_creates_worktree_when_folder_is_git_repo(monkeypatch, patched_orchestrator, tmp_path):
    repo = _make_real_repo(tmp_path)
    # Re-enable real is_git_repo so orchestrator detects the temp repo
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    p = _payload(identifier="TES-901", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-git")

    worktree = tmp_path / "tickets" / "TES-901" / "worktree"
    assert worktree.exists()
    assert (worktree / "README.md").exists()
    body = patched_orchestrator["comments"][0]["body"]
    assert "branch: `ticket/TES-901`" in body


def test_stage1_includes_diff_in_comment_when_claude_changes_files(monkeypatch, patched_orchestrator, tmp_path):
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    # Mock claude to write a file in the worktree before returning
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "claude_made_this.txt").write_text("hello from claude\n")
        return runner_mod.RunResult(0, "wrote claude_made_this.txt", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-902", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-diff")

    body = patched_orchestrator["comments"][0]["body"]
    assert "claude_made_this.txt" in body
    assert "Diff vs. main" in body
    assert "Commits on branch" in body


def test_stage2_merges_worktree_into_main_and_cleans_up(monkeypatch, patched_orchestrator, tmp_path):
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "feature.txt").write_text("feature content\n")
        return runner_mod.RunResult(0, "added feature", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    # Run stage 1 to create worktree + branch + commit
    p = _payload(identifier="TES-903", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-merge-1")
    assert (tmp_path / "tickets" / "TES-903" / "worktree" / "feature.txt").exists()

    # Now stage 2: merge
    orchestrator.orchestrate_complete(p, delivery_id="d-merge-2")

    # Feature landed on main
    assert (repo / "feature.txt").exists()
    # Worktree gone
    assert not (tmp_path / "tickets" / "TES-903" / "worktree").exists()
    # Last comment is the merge confirmation
    last = patched_orchestrator["comments"][-1]["body"]
    assert "Merged" in last
    assert "no `origin` remote" in last  # tmp repo has no remote


def test_stage2_handles_merge_conflict_and_rolls_back_status(monkeypatch, patched_orchestrator, tmp_path):
    from app import git_ops as g
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    # Stage 1 — branch edits README
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "README.md").write_text("from branch\n")
        return runner_mod.RunResult(0, "edited readme", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-904", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-c1")

    # Diverge main: edit README differently and commit
    (repo / "README.md").write_text("from main side\n")
    g._run(["git", "add", "-A"], cwd=repo)
    g._run(["git", "commit", "-m", "main edit"], cwd=repo)

    # Stage 2 — should hit a conflict
    orchestrator.orchestrate_complete(p, delivery_id="d-c2")

    last = patched_orchestrator["comments"][-1]["body"]
    assert "Conflict" in last or "conflict" in last
    # State rolled back to Draft PR Ready
    assert any(s["state_id"] == "state-id-for-Draft PR Ready" for s in patched_orchestrator["state_changes"][1:])


def test_stage2_removes_empty_ticket_dir_after_merge(monkeypatch, patched_orchestrator, tmp_path):
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "x.txt").write_text("x\n")
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-906", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-rm-1")
    orchestrator.orchestrate_complete(p, delivery_id="d-rm-2")

    # ticket_dir is gone after merge + cleanup
    assert not (tmp_path / "tickets" / "TES-906").exists()


def test_stage2_keeps_ticket_dir_if_files_remain(monkeypatch, patched_orchestrator, tmp_path):
    """If anything besides the worktree (e.g. attachments, user files) is in
    the ticket dir, leave it alone."""
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "y.txt").write_text("y\n")
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-907", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-keep-1")

    # Drop a sticky note in the ticket dir before merge
    (tmp_path / "tickets" / "TES-907" / "notes.txt").write_text("don't delete me\n")

    orchestrator.orchestrate_complete(p, delivery_id="d-keep-2")

    # Worktree gone but ticket_dir survives because of the sticky note
    assert not (tmp_path / "tickets" / "TES-907" / "worktree").exists()
    assert (tmp_path / "tickets" / "TES-907" / "notes.txt").exists()


def test_stage2_noop_path_also_cleans_empty_ticket_dir(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")
    # Create an empty ticket dir manually (simulating the non-git fallback case)
    td = tmp_path / "tickets" / "TES-908"
    td.mkdir(parents=True)

    p = _payload(identifier="TES-908", state_name="Done")
    orchestrator.orchestrate_complete(p, delivery_id="d-noop")

    assert not td.exists()


def test_proxy_uploads_new_files_as_linear_attachments(monkeypatch, patched_orchestrator, tmp_path):
    """Files Claude writes during a proxy run get auto-attached to the ticket
    and surface in the final comment."""
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")

    attached: list[tuple[str, Path, str | None]] = []

    def fake_attach(issue_id, local_path, *, title=None, subtitle=None, content_type=None, client=None):
        attached.append((issue_id, local_path, title))
        return f"att-{local_path.name}"

    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", fake_attach)

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "report.md").write_text("# notes\n")
        (cwd / "data.json").write_text('{"x": 1}\n')
        return runner_mod.RunResult(0, "wrote files", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-FILES", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-files")

    # Both files attached to the ticket
    issue_ids = [a[0] for a in attached]
    names = sorted(a[1].name for a in attached)
    assert issue_ids == ["issue-uuid", "issue-uuid"]
    assert names == ["data.json", "report.md"]

    body = patched_orchestrator["comments"][0]["body"]
    assert "Files generated" in body
    assert "report.md" in body
    assert "data.json" in body


def test_proxy_omits_files_section_when_nothing_written(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", lambda *a, **kw: "att-x")

    p = _payload(identifier="TES-NOFILES", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-nofiles")

    body = patched_orchestrator["comments"][0]["body"]
    assert "Files generated" not in body


def test_proxy_reports_upload_failure_in_comment(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")

    def boom_attach(*a, **kw):
        raise RuntimeError("Linear is down")
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", boom_attach)

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "out.md").write_text("hi\n")
        return runner_mod.RunResult(0, "wrote out.md", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-FAIL-UPLOAD", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-fail")

    body = patched_orchestrator["comments"][0]["body"]
    assert "out.md" in body
    assert "upload failed" in body
    assert "Linear is down" in body


def test_proxy_runs_in_tmp_and_sets_state_to_done(monkeypatch, patched_orchestrator, tmp_path):
    """Phase 4 — orchestrate_proxy uses /tmp, posts comment, sets Done directly."""
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")

    p = _payload(
        identifier="TES-PROXY-1",
        title="Was ist der Sinn des Lebens?",
        description="Eine kurze Antwort genügt.",
        project_name="⚡ Ad-hoc AI Proxy",
    )

    orchestrator.orchestrate_proxy(p, delivery_id="px-1")

    # cwd was created in tmp
    assert (tmp_path / "proxy-base" / "TES-PROXY-1").exists()
    # Comment posted
    assert len(patched_orchestrator["comments"]) == 1
    body = patched_orchestrator["comments"][0]["body"]
    assert "Ad-hoc AI Proxy" in body
    assert "(mocked claude output)" in body
    # State moved straight to Done
    assert patched_orchestrator["state_changes"] == [
        {"issue_id": "issue-uuid", "state_id": "state-id-for-Done"}
    ]


def test_proxy_includes_attachments_in_prompt(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    patched_orchestrator["_attachments_to_return"] = [
        Attachment(id="a1", title="data.csv", url="https://uploads.linear.app/data.csv", subtitle=None),
    ]

    captured_prompt = {"value": ""}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured_prompt["value"] = prompt
        cwd.mkdir(parents=True, exist_ok=True)
        return runner_mod.RunResult(0, "analyzed", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PROXY-2", title="Analysiere data.csv", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="px-2")

    # Attachments dir mentioned in prompt so Claude can find them
    assert "TES-PROXY-2/attachments" in captured_prompt["value"].replace("\\", "/")


def test_proxy_posts_error_and_reraises(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    from app import runner as runner_mod
    def boom(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        raise RuntimeError("subprocess died")
    monkeypatch.setattr("app.orchestrator.runner.run_cli", boom)

    p = _payload(identifier="TES-PROXY-3", project_name="⚡ Ad-hoc AI Proxy")
    with pytest.raises(RuntimeError, match="subprocess died"):
        orchestrator.orchestrate_proxy(p, delivery_id="px-3")

    assert len(patched_orchestrator["comments"]) == 1
    assert "proxy failed" in patched_orchestrator["comments"][0]["body"].lower()


def test_stage2_aborts_when_main_repo_is_dirty(monkeypatch, patched_orchestrator, tmp_path):
    repo = _make_real_repo(tmp_path)
    monkeypatch.setattr("app.orchestrator.git_ops.is_git_repo", lambda p: True)
    monkeypatch.setattr("app.orchestrator.TICKETS_BASE", tmp_path / "tickets")

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        (cwd / "x.txt").write_text("x\n")
        return runner_mod.RunResult(0, "ok", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-905", description=f"folder: {repo}")
    orchestrator.orchestrate_start(p, delivery_id="d-d1")

    # Make main repo dirty
    (repo / "untracked.txt").write_text("dirty\n")

    orchestrator.orchestrate_complete(p, delivery_id="d-d2")

    last = patched_orchestrator["comments"][-1]["body"]
    assert "uncommitted changes" in last
    # Worktree still exists since we aborted
    assert (tmp_path / "tickets" / "TES-905" / "worktree").exists()


def test_stage1_uploads_new_files_as_linear_attachments(monkeypatch, patched_orchestrator, tmp_path):
    """Files Claude writes into the cwd during a non-git stage1 run get
    auto-attached to the ticket and surface in the run-comment (TES-615)."""
    cwd = tmp_path / "stage1-folder"
    cwd.mkdir()
    monkeypatch.setattr(orchestrator, "resolve_folder",
                        lambda data: type("F", (), {"strategy": "fallback", "path": cwd})())

    attached: list[tuple[str, Path, str | None]] = []
    def fake_attach(issue_id, local_path, *, title=None, subtitle=None, content_type=None, client=None):
        attached.append((issue_id, local_path, title))
        return f"att-{local_path.name}"
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", fake_attach)

    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "report.md").write_text("# generated\n")
        (cwd / "data.csv").write_text("a,b,c\n1,2,3\n")
        return runner_mod.RunResult(0, "wrote files", "", False)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-ATT1"), delivery_id="d-att1")

    names = sorted(a[1].name for a in attached)
    assert names == ["data.csv", "report.md"]

    body = patched_orchestrator["comments"][0]["body"]
    assert "Files attached to ticket" in body
    assert "report.md" in body
    assert "data.csv" in body


def test_stage1_omits_files_section_when_nothing_written(monkeypatch, patched_orchestrator, tmp_path):
    """If Claude doesn't touch any file in cwd, no upload section is rendered."""
    cwd = tmp_path / "stage1-empty"
    cwd.mkdir()
    monkeypatch.setattr(orchestrator, "resolve_folder",
                        lambda data: type("F", (), {"strategy": "fallback", "path": cwd})())
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", lambda *a, **kw: "att-x")

    orchestrator.orchestrate_start(_payload(identifier="TES-ATT2"), delivery_id="d-att2")

    body = patched_orchestrator["comments"][0]["body"]
    assert "Files attached to ticket" not in body


# --- timeout resolution: env defaults + label override ---------------------

def test_stage1_uses_stage1_default_timeout_when_no_label(monkeypatch, patched_orchestrator):
    """No timeout:* label → run_cli called with runner.DEFAULT_TIMEOUT_STAGE1."""
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    orchestrator.orchestrate_start(_payload(identifier="TES-S1-DEF"), delivery_id="d")
    assert captured["timeout"] == runner_mod.DEFAULT_TIMEOUT_STAGE1


def test_proxy_uses_proxy_default_timeout_when_no_label(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", lambda *a, **kw: None)
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        cwd.mkdir(parents=True, exist_ok=True)
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PX-DEF", project_name="⚡ Ad-hoc AI Proxy")
    orchestrator.orchestrate_proxy(p, delivery_id="d")
    assert captured["timeout"] == runner_mod.DEFAULT_TIMEOUT_PROXY


def test_stage1_label_override_beats_stage1_default(monkeypatch, patched_orchestrator):
    """timeout:1800 label on a Stage 1 ticket → run_cli gets 1800s."""
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-S1-LBL")
    p["data"]["labels"] = [{"name": "timeout:1800"}]
    orchestrator.orchestrate_start(p, delivery_id="d")
    assert captured["timeout"] == 1800


def test_proxy_label_override_beats_proxy_default(monkeypatch, patched_orchestrator, tmp_path):
    monkeypatch.setattr("app.orchestrator.PROXY_BASE", tmp_path / "proxy-base")
    monkeypatch.setattr(orchestrator.linear_api, "attach_local_file", lambda *a, **kw: None)
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        cwd.mkdir(parents=True, exist_ok=True)
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-PX-LBL", project_name="⚡ Ad-hoc AI Proxy")
    p["data"]["labels"] = [{"name": "timeout:60"}]
    orchestrator.orchestrate_proxy(p, delivery_id="d")
    assert captured["timeout"] == 60


def test_label_override_clamped_to_hard_cap(monkeypatch, patched_orchestrator, caplog):
    """timeout:99999 → clamped to TIMEOUT_HARD_CAP_SECONDS, warning logged."""
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-CLAMP")
    p["data"]["labels"] = [{"name": "timeout:99999"}]
    with caplog.at_level("WARNING"):
        orchestrator.orchestrate_start(p, delivery_id="d")
    assert captured["timeout"] == runner_mod.TIMEOUT_HARD_CAP_SECONDS
    assert "exceeds hard cap" in caplog.text


def test_invalid_timeout_label_falls_back_to_default(monkeypatch, patched_orchestrator):
    """timeout:forever (unparseable) → resolve_timeout returns None, default applies."""
    captured = {}
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        captured["timeout"] = timeout
        return runner_mod.RunResult(0, "ok", "", False, cli=cli, timeout_used=timeout)
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-BAD-LBL")
    p["data"]["labels"] = [{"name": "timeout:forever"}]
    orchestrator.orchestrate_start(p, delivery_id="d")
    assert captured["timeout"] == runner_mod.DEFAULT_TIMEOUT_STAGE1


def test_timeout_message_uses_actual_value_not_module_constant(monkeypatch, patched_orchestrator):
    """The Linear failure comment must show the timeout the run actually got
    (not runner.DEFAULT_TIMEOUT_SECONDS, which would lie when a label override
    bumped or shortened the run)."""
    from app import runner as runner_mod
    def fake_run(cli, prompt, cwd, *, timeout=runner_mod.DEFAULT_TIMEOUT_SECONDS, on_start=None, model=None, auth_mode="oauth"):
        return runner_mod.RunResult(
            exit_code=-1, stdout="", stderr=f"TIMEOUT after {timeout}s",
            timed_out=True, cli=cli, timeout_used=timeout,
        )
    monkeypatch.setattr("app.orchestrator.runner.run_cli", fake_run)

    p = _payload(identifier="TES-MSG-TO")
    p["data"]["labels"] = [{"name": "timeout:42"}]
    with pytest.raises(RuntimeError, match="timed out after 42s"):
        orchestrator.orchestrate_start(p, delivery_id="d")
    bodies = [c["body"] for c in patched_orchestrator["comments"]]
    assert any("timed out after 42s" in b for b in bodies), bodies
    assert patched_orchestrator["state_changes"] == []
