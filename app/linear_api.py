"""Thin wrapper around the Linear GraphQL API.

Only the calls the executor needs:
- ``fetch_issue_attachments`` — get attachment URLs for a ticket
- ``fetch_workflow_state_id`` — map a state name to its UUID (per team)
- ``post_comment`` — post a markdown comment
- ``set_issue_state`` — move a ticket to a different workflow state

Auth: Linear uses a plain API key in the ``Authorization`` header, NOT a
``Bearer`` prefix. The token is read from env var ``LINEAR_API_KEY``.

All functions use a shared ``httpx.Client`` passed in by the caller so the
webhook handler can share connections and so tests can inject a mock.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

import httpx


LINEAR_API_URL = "https://api.linear.app/graphql"


@dataclass(frozen=True)
class Attachment:
    id: str
    title: str
    url: str
    subtitle: str | None


@dataclass(frozen=True)
class IssueSummary:
    id: str
    identifier: str
    title: str
    url: str
    state_name: str
    state_type: str
    project_name: str | None
    labels: tuple[str, ...]
    created_at: str
    updated_at: str
    completed_at: str | None


# Prefix markers the orchestrator uses when it posts its own comments back to
# Linear. Any comment starting with one of these is a previous executor run
# (or a Claude-emitted progress update during a previous run) and should be
# skipped when we re-inject comments as follow-up context.
EXECUTOR_COMMENT_PREFIXES = (
    "\U0001f916 **Linear-Executor**",   # HEADER_RUN
    "✅ **Linear-Executor**",        # HEADER_REVIEW + lifecycle Done
    "\U0001f500 **Linear-Executor**",   # HEADER_MERGE
    "⚠ **Linear-Executor**",        # HEADER_CONFLICT
    "⚡ **Linear-Executor**",        # HEADER_PROXY
    "⏸ **Linear-Executor**",        # HEADER_CANCELLED
    "👀 **Linear-Executor**",        # HEADER_REVIEW_WATCH
    "⏳ **Linear-Executor**",        # lifecycle Queued
    "\U0001f3c3 **Linear-Executor**",   # 🏃 lifecycle Running
    "❌ **Linear-Executor**",        # lifecycle Failed
    "\U0001f501 **Linear-Executor**",   # 🔁 lifecycle Retrying
    "⚠ Linear-Executor",             # generic error comment
    "\U0001f504",                    # 🔄 progress markers from Claude itself
)


def _is_executor_body(body: str) -> bool:
    stripped = (body or "").lstrip()
    return any(stripped.startswith(p) for p in EXECUTOR_COMMENT_PREFIXES)


@dataclass(frozen=True)
class Comment:
    id: str
    body: str
    created_at: str
    author_name: str
    is_executor_comment: bool


def _client(api_key: str | None = None) -> httpx.Client:
    token = api_key or os.getenv("LINEAR_API_KEY", "")
    if not token:
        raise RuntimeError("LINEAR_API_KEY not set")
    return httpx.Client(
        base_url=LINEAR_API_URL,
        headers={"Authorization": token, "Content-Type": "application/json"},
        timeout=20.0,
    )


def _post_graphql(client: httpx.Client, query: str, variables: dict) -> dict:
    r = client.post("", json={"query": query, "variables": variables})
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Linear GraphQL errors: {body['errors']}")
    return body["data"]


def fetch_issue_project_id(issue_id: str, *, client: httpx.Client | None = None) -> str | None:
    """Fetch the Linear project ID a ticket belongs to.

    Linear's webhook payload does NOT include the project field by default
    (only ``team``/``teamId``), so detection of proxy tickets requires this
    extra GraphQL hop. Returns ``None`` if the issue has no project assigned
    or the lookup fails.
    """
    query = """
    query($id: String!) {
      issue(id: $id) {
        project { id }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, query, {"id": issue_id})
        issue = data.get("issue") or {}
        project = issue.get("project") or {}
        return project.get("id")
    except Exception:
        return None
    finally:
        if owns_client:
            client.close()


def fetch_project_issues(
    project_name: str,
    state_names: list[str],
    *,
    first: int = 100,
    client: httpx.Client | None = None,
) -> list[IssueSummary]:
    """Fetch issues for an executive-assistant sweep.

    This intentionally returns a compact, prose-safe issue snapshot instead of
    raw GraphQL payloads. Callers render evidence pointers, not full issue data.
    """
    query = """
    query($projectName: String!, $stateNames: [String!], $first: Int!, $after: String) {
      issues(
        first: $first,
        after: $after,
        filter: {
          project: { name: { eq: $projectName } }
          state: { name: { in: $stateNames } }
        }
      ) {
        nodes {
          id
          identifier
          title
          url
          createdAt
          updatedAt
          completedAt
          state { name type }
          project { name }
          labels { nodes { name } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    issues: list[IssueSummary] = []
    after: str | None = None
    try:
        while True:
            data = _post_graphql(
                client,
                query,
                {
                    "projectName": project_name,
                    "stateNames": state_names,
                    "first": first,
                    "after": after,
                },
            )
            page = data.get("issues") or {}
            for node in page.get("nodes") or []:
                state = node.get("state") or {}
                project = node.get("project") or {}
                labels = node.get("labels") or {}
                issues.append(
                    IssueSummary(
                        id=node.get("id", "") or "",
                        identifier=node.get("identifier", "") or "",
                        title=node.get("title", "") or "",
                        url=node.get("url", "") or "",
                        state_name=state.get("name", "") or "",
                        state_type=state.get("type", "") or "",
                        project_name=project.get("name"),
                        labels=tuple(
                            label.get("name", "") or ""
                            for label in (labels.get("nodes") or [])
                            if label.get("name")
                        ),
                        created_at=node.get("createdAt", "") or "",
                        updated_at=node.get("updatedAt", "") or "",
                        completed_at=node.get("completedAt"),
                    )
                )
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return issues
            after = page_info.get("endCursor")
            if not after:
                return issues
    finally:
        if owns_client:
            client.close()


def fetch_issue_attachments(issue_id: str, *, client: httpx.Client | None = None) -> list[Attachment]:
    query = """
    query($id: String!) {
      issue(id: $id) {
        attachments {
          nodes { id title url subtitle }
        }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, query, {"id": issue_id})
        nodes = (data.get("issue") or {}).get("attachments", {}).get("nodes", []) or []
        return [
            Attachment(id=n["id"], title=n.get("title", ""), url=n["url"], subtitle=n.get("subtitle"))
            for n in nodes
        ]
    finally:
        if owns_client:
            client.close()


def fetch_issue_comments(issue_id: str, *, client: httpx.Client | None = None) -> list[Comment]:
    """Fetch all comments on a ticket, oldest first.

    Each comment is tagged with ``is_executor_comment`` so callers can skip
    the executor's own run/review/merge posts when building follow-up context.
    """
    query = """
    query($id: String!) {
      issue(id: $id) {
        comments {
          nodes {
            id
            body
            createdAt
            user { id name }
          }
        }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, query, {"id": issue_id})
        nodes = (data.get("issue") or {}).get("comments", {}).get("nodes", []) or []
        comments = [
            Comment(
                id=n["id"],
                body=n.get("body", "") or "",
                created_at=n.get("createdAt", "") or "",
                author_name=((n.get("user") or {}).get("name") or ""),
                is_executor_comment=_is_executor_body(n.get("body", "") or ""),
            )
            for n in nodes
        ]
        # Linear returns newest-first by default; normalize to chronological.
        comments.sort(key=lambda c: c.created_at)
        return comments
    finally:
        if owns_client:
            client.close()


def fetch_workflow_state_id(team_id: str, state_name: str, *, client: httpx.Client | None = None) -> str | None:
    query = """
    query($teamId: ID!, $name: String!) {
      workflowStates(filter: { team: { id: { eq: $teamId } }, name: { eq: $name } }) {
        nodes { id name }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, query, {"teamId": team_id, "name": state_name})
        nodes = data.get("workflowStates", {}).get("nodes", []) or []
        return nodes[0]["id"] if nodes else None
    finally:
        if owns_client:
            client.close()


def post_comment(issue_id: str, body: str, *, client: httpx.Client | None = None) -> str:
    mutation = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
        comment { id }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, mutation, {"issueId": issue_id, "body": body})
        res = data.get("commentCreate") or {}
        if not res.get("success"):
            raise RuntimeError(f"Linear commentCreate failed: {res}")
        return (res.get("comment") or {}).get("id", "")
    finally:
        if owns_client:
            client.close()


def update_comment(comment_id: str, body: str, *, client: httpx.Client | None = None) -> None:
    """Edit an existing Linear comment in place (commentUpdate mutation)."""
    mutation = """
    mutation($id: String!, $body: String!) {
      commentUpdate(id: $id, input: { body: $body }) {
        success
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, mutation, {"id": comment_id, "body": body})
        res = data.get("commentUpdate") or {}
        if not res.get("success"):
            raise RuntimeError(f"Linear commentUpdate failed: {res}")
    finally:
        if owns_client:
            client.close()


def _request_file_upload(
    *,
    content_type: str,
    filename: str,
    size: int,
    client: httpx.Client | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Ask Linear for a signed upload URL.

    Returns ``(upload_url, asset_url, headers)`` — PUT the file bytes to
    ``upload_url`` with ``headers``, then reference ``asset_url`` from a
    Linear attachment.
    """
    mutation = """
    mutation($contentType: String!, $filename: String!, $size: Int!) {
      fileUpload(contentType: $contentType, filename: $filename, size: $size) {
        success
        uploadFile {
          uploadUrl
          assetUrl
          headers { key value }
        }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, mutation, {
            "contentType": content_type,
            "filename": filename,
            "size": size,
        })
        payload = (data.get("fileUpload") or {})
        if not payload.get("success"):
            raise RuntimeError(f"Linear fileUpload returned success=false: {payload}")
        upload = payload.get("uploadFile") or {}
        upload_url = upload.get("uploadUrl") or ""
        asset_url = upload.get("assetUrl") or ""
        headers_list = upload.get("headers") or []
        headers = {h["key"]: h["value"] for h in headers_list}
        if not upload_url or not asset_url:
            raise RuntimeError(f"Linear fileUpload missing url(s): {payload}")
        return upload_url, asset_url, headers
    finally:
        if owns_client:
            client.close()


def _put_file_to_storage(
    upload_url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout: float = 60.0,
) -> None:
    """Upload raw bytes to the signed URL Linear handed us."""
    with httpx.Client(timeout=timeout) as plain:
        r = plain.put(upload_url, content=body, headers=headers)
        r.raise_for_status()


def create_attachment(
    issue_id: str,
    *,
    url: str,
    title: str,
    subtitle: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Attach a URL (typically the assetUrl from an upload) to a Linear issue."""
    mutation = """
    mutation($issueId: String!, $title: String!, $url: String!, $subtitle: String) {
      attachmentCreate(input: { issueId: $issueId, title: $title, url: $url, subtitle: $subtitle }) {
        success
        attachment { id }
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, mutation, {
            "issueId": issue_id, "title": title, "url": url, "subtitle": subtitle,
        })
        res = data.get("attachmentCreate") or {}
        if not res.get("success"):
            raise RuntimeError(f"Linear attachmentCreate failed: {res}")
        return (res.get("attachment") or {}).get("id", "")
    finally:
        if owns_client:
            client.close()


def attach_local_file(
    issue_id: str,
    local_path: Path,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    content_type: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Upload a local file and attach the resulting URL to a Linear issue.

    Returns the new attachment's id. Raises on any step failure — the
    caller decides whether to swallow per file or abort the whole batch.
    """
    if content_type is None:
        guessed, _ = mimetypes.guess_type(str(local_path))
        content_type = guessed or "application/octet-stream"
    body = local_path.read_bytes()
    upload_url, asset_url, headers = _request_file_upload(
        content_type=content_type,
        filename=local_path.name,
        size=len(body),
        client=client,
    )
    # Linear's pre-signed GCS URL is signed with the content-type we declared
    # to fileUpload(). httpx won't auto-set it for raw bytes, so the GCS PUT
    # fails 400 unless we add it explicitly.
    headers = {**headers, "Content-Type": content_type}
    _put_file_to_storage(upload_url, body, headers)
    return create_attachment(
        issue_id,
        url=asset_url,
        title=title or local_path.name,
        subtitle=subtitle,
        client=client,
    )


def set_issue_state(issue_id: str, state_id: str, *, client: httpx.Client | None = None) -> bool:
    mutation = """
    mutation($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
      }
    }
    """
    owns_client = client is None
    client = client or _client()
    try:
        data = _post_graphql(client, mutation, {"id": issue_id, "stateId": state_id})
        return bool((data.get("issueUpdate") or {}).get("success"))
    finally:
        if owns_client:
            client.close()
