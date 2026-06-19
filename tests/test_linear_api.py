import httpx
import pytest

from app.linear_api import (
    Attachment,
    Comment,
    attach_local_file,
    create_attachment,
    fetch_issue_attachments,
    fetch_issue_comments,
    fetch_project_issues,
    fetch_workflow_state_id,
    post_comment,
    set_issue_state,
)


def _mock_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(
        base_url="https://api.linear.app/graphql",
        headers={"Authorization": "test-key"},
        transport=transport,
    )


def test_fetch_attachments_returns_parsed_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "attachments": {
                            "nodes": [
                                {"id": "a1", "title": "Spec.pdf", "url": "https://cdn/spec.pdf", "subtitle": "12 pages"},
                                {"id": "a2", "title": "Screenshot", "url": "https://cdn/screen.png", "subtitle": None},
                            ]
                        }
                    }
                }
            },
        )

    with _mock_client(handler) as client:
        result = fetch_issue_attachments("issue-uuid", client=client)
    assert result == [
        Attachment(id="a1", title="Spec.pdf", url="https://cdn/spec.pdf", subtitle="12 pages"),
        Attachment(id="a2", title="Screenshot", url="https://cdn/screen.png", subtitle=None),
    ]


def test_fetch_attachments_returns_empty_when_no_attachments():
    def handler(request):
        return httpx.Response(200, json={"data": {"issue": {"attachments": {"nodes": []}}}})

    with _mock_client(handler) as client:
        assert fetch_issue_attachments("x", client=client) == []


def test_fetch_workflow_state_id_found():
    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"workflowStates": {"nodes": [{"id": "state-uuid", "name": "Done"}]}}},
        )

    with _mock_client(handler) as client:
        assert fetch_workflow_state_id("team-uuid", "Done", client=client) == "state-uuid"


def test_fetch_workflow_state_id_none_when_missing():
    def handler(request):
        return httpx.Response(200, json={"data": {"workflowStates": {"nodes": []}}})

    with _mock_client(handler) as client:
        assert fetch_workflow_state_id("team-uuid", "Nonexistent", client=client) is None


def test_post_comment_returns_comment_id_on_success():
    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"commentCreate": {"success": True, "comment": {"id": "c-123"}}}},
        )

    with _mock_client(handler) as client:
        assert post_comment("issue-uuid", "hello", client=client) == "c-123"


def test_post_comment_raises_on_failure():
    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"commentCreate": {"success": False, "comment": None}}},
        )

    with _mock_client(handler) as client:
        with pytest.raises(RuntimeError):
            post_comment("issue-uuid", "hello", client=client)


def test_set_issue_state_returns_bool():
    def handler(request):
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})

    with _mock_client(handler) as client:
        assert set_issue_state("issue-uuid", "state-uuid", client=client) is True


def test_fetch_comments_returns_chronological_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "comments": {
                            "nodes": [
                                {
                                    "id": "c1",
                                    "body": "First note from Bastian",
                                    "createdAt": "2026-04-20T10:00:00Z",
                                    "user": {"id": "u1", "name": "Bastian"},
                                },
                                {
                                    "id": "c2",
                                    "body": "\U0001f916 **Linear-Executor** — Claude Code Run\nOutput here",
                                    "createdAt": "2026-04-20T10:05:00Z",
                                    "user": {"id": "u1", "name": "Bastian"},
                                },
                                {
                                    "id": "c3",
                                    "body": "Please also handle edge case X",
                                    "createdAt": "2026-04-20T10:10:00Z",
                                    "user": {"id": "u1", "name": "Bastian"},
                                },
                                {
                                    "id": "c4",
                                    "body": "👀 **Linear-Executor** — Review Watch\nMarked PR ready",
                                    "createdAt": "2026-04-20T10:15:00Z",
                                    "user": {"id": "u1", "name": "Bastian"},
                                },
                            ]
                        }
                    }
                }
            },
        )

    with _mock_client(handler) as client:
        result = fetch_issue_comments("issue-uuid", client=client)
    assert len(result) == 4
    assert [c.id for c in result] == ["c1", "c2", "c3", "c4"]
    assert result[0].body == "First note from Bastian"
    assert result[1].is_executor_comment is True
    assert result[2].is_executor_comment is False
    assert result[3].is_executor_comment is True


def test_fetch_comments_returns_empty_when_none():
    def handler(request):
        return httpx.Response(200, json={"data": {"issue": {"comments": {"nodes": []}}}})

    with _mock_client(handler) as client:
        assert fetch_issue_comments("x", client=client) == []


def test_create_attachment_returns_id_on_success():
    def handler(request):
        return httpx.Response(
            200,
            json={"data": {"attachmentCreate": {"success": True, "attachment": {"id": "att-9"}}}},
        )

    with _mock_client(handler) as client:
        att_id = create_attachment(
            "issue-uuid", url="https://uploads.linear.app/x.md", title="x.md", client=client,
        )
    assert att_id == "att-9"


def test_attach_local_file_uploads_and_creates_attachment(tmp_path, monkeypatch):
    """Two sequential GraphQL hops + one PUT to storage. We mock Linear's
    GraphQL transport AND the storage PUT, then verify the orchestration."""
    f = tmp_path / "research.md"
    f.write_bytes(b"# my notes\n")

    graphql_calls = []
    storage_puts = []

    def graphql_handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        graphql_calls.append(body)
        if "fileUpload" in body:
            return httpx.Response(200, json={
                "data": {"fileUpload": {
                    "success": True,
                    "uploadFile": {
                        "uploadUrl": "https://storage.example/upload?sig=abc",
                        "assetUrl": "https://uploads.linear.app/research.md",
                        "headers": [{"key": "x-amz-meta", "value": "ok"}],
                    },
                }}
            })
        if "attachmentCreate" in body:
            return httpx.Response(200, json={
                "data": {"attachmentCreate": {"success": True, "attachment": {"id": "att-42"}}}
            })
        return httpx.Response(500, text="unexpected mutation")

    def fake_put(upload_url, body, headers, *, timeout=60.0):
        storage_puts.append((upload_url, body, headers))

    monkeypatch.setattr("app.linear_api._put_file_to_storage", fake_put)

    with _mock_client(graphql_handler) as gql_client:
        att_id = attach_local_file("issue-uuid", f, client=gql_client)

    assert att_id == "att-42"
    assert len(storage_puts) == 1
    assert storage_puts[0][0] == "https://storage.example/upload?sig=abc"
    assert storage_puts[0][1] == b"# my notes\n"
    # Linear-given headers + Content-Type (forced explicitly to satisfy the
    # signed pre-signed URL — see attach_local_file)
    assert storage_puts[0][2] == {"x-amz-meta": "ok", "Content-Type": "text/markdown"}
    assert any("fileUpload" in c for c in graphql_calls)
    assert any("attachmentCreate" in c for c in graphql_calls)


def test_graphql_errors_raise():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "forbidden"}]})

    with _mock_client(handler) as client:
        with pytest.raises(RuntimeError, match="forbidden"):
            fetch_issue_attachments("x", client=client)


def test_fetch_project_issues_returns_compact_issue_summaries():
    seen = []

    def handler(request):
        body = request.read().decode()
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "issue-uuid",
                                "identifier": "BAS-117",
                                "title": "Implement Linear EA inbox memo sweep",
                                "url": "https://linear.app/basicbit/issue/BAS-117/x",
                                "createdAt": "2026-06-19T07:15:12Z",
                                "updatedAt": "2026-06-19T18:10:38Z",
                                "completedAt": None,
                                "state": {"name": "Todo", "type": "unstarted"},
                                "project": {"name": "Linear Agent Control Plane"},
                                "labels": {"nodes": [{"name": "infrastructure"}]},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    with _mock_client(handler) as client:
        issues = fetch_project_issues(
            "Linear Agent Control Plane",
            ["Todo", "Human Design Review"],
            client=client,
        )

    assert len(issues) == 1
    assert issues[0].identifier == "BAS-117"
    assert issues[0].state_name == "Todo"
    assert issues[0].project_name == "Linear Agent Control Plane"
    assert issues[0].labels == ("infrastructure",)
    assert "Linear Agent Control Plane" in seen[0]
