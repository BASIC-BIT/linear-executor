"""Download Linear issue attachments to a local folder.

Linear attachments can be:
- Linear-hosted (uploads.linear.app or linear.app) — need the API key in the
  Authorization header
- External (S3, Google Drive, customer-provided URLs) — no auth, and we must
  NOT leak our API key to third parties

Only the needed files are downloaded, file-name-sanitized to avoid directory
traversal. Returns the list of local paths for the caller (so Claude's prompt
can reference them).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.linear_api import Attachment


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _needs_linear_auth(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "linear.app" or host.endswith(".linear.app")


def _sanitize(name: str, fallback: str) -> str:
    name = (name or "").strip() or fallback
    safe = _SAFE_NAME_RE.sub("_", name).strip("._")
    return safe or fallback


def _target_path(target_dir: Path, attachment: Attachment, index: int) -> Path:
    """Pick a filesystem-safe local path for the downloaded file.

    Prefers the attachment title; if that has no extension, tries to pull one
    from the URL path. Falls back to ``attachment-<n>`` if nothing is usable.
    """
    base = _sanitize(attachment.title, fallback=f"attachment-{index}")
    has_ext = "." in base and not base.startswith(".")
    if not has_ext:
        url_name = os.path.basename(urlparse(attachment.url).path)
        url_ext = os.path.splitext(url_name)[1]
        if url_ext:
            base = f"{base}{url_ext}"
    return target_dir / base


def download_attachments(
    attachments: list[Attachment],
    target_dir: Path,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[Path]:
    if not attachments:
        return []
    target_dir.mkdir(parents=True, exist_ok=True)

    token = api_key or os.getenv("LINEAR_API_KEY", "")
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)

    saved: list[Path] = []
    try:
        for idx, att in enumerate(attachments, start=1):
            headers = {"Authorization": token} if (_needs_linear_auth(att.url) and token) else {}
            r = client.get(att.url, headers=headers)
            r.raise_for_status()
            out = _target_path(target_dir, att, idx)
            out.write_bytes(r.content)
            saved.append(out)
    finally:
        if owns_client:
            client.close()
    return saved
