import httpx

from app.attachments import _needs_linear_auth, download_attachments
from app.linear_api import Attachment


def _make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_needs_linear_auth_detects_linear_hosts():
    assert _needs_linear_auth("https://uploads.linear.app/abc/file.pdf") is True
    assert _needs_linear_auth("https://linear.app/asset/x") is True


def test_needs_linear_auth_rejects_external_hosts():
    assert _needs_linear_auth("https://s3.amazonaws.com/bucket/file.pdf") is False
    assert _needs_linear_auth("https://drive.google.com/file/d/123") is False
    assert _needs_linear_auth("") is False


def test_download_writes_files_with_correct_names(tmp_path):
    received_auths: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received_auths[str(request.url)] = request.headers.get("authorization")
        return httpx.Response(200, content=f"body-of-{request.url.path}".encode())

    atts = [
        Attachment(id="1", title="Spec.pdf", url="https://uploads.linear.app/a/Spec.pdf", subtitle=None),
        Attachment(id="2", title="diagram", url="https://uploads.linear.app/b/diagram.png", subtitle=None),
    ]
    with _make_client(handler) as client:
        saved = download_attachments(atts, tmp_path, api_key="sekret", client=client)

    assert len(saved) == 2
    assert saved[0].name == "Spec.pdf"
    assert saved[0].read_bytes() == b"body-of-/a/Spec.pdf"
    # Extension inferred from URL because "diagram" has none
    assert saved[1].name == "diagram.png"
    # Auth header was sent for linear-hosted URLs
    assert all(v == "sekret" for v in received_auths.values())


def test_download_does_not_send_auth_to_external_host(tmp_path):
    seen_auth: list[str | None] = []

    def handler(request):
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, content=b"external bytes")

    atts = [Attachment(id="x", title="ext.pdf", url="https://cdn.example.com/ext.pdf", subtitle=None)]
    with _make_client(handler) as client:
        download_attachments(atts, tmp_path, api_key="sekret", client=client)

    assert seen_auth == [None]


def test_sanitizes_unsafe_filename(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"x")

    atts = [
        Attachment(id="1", title="../../../etc/passwd", url="https://cdn.example.com/x.bin", subtitle=None)
    ]
    with _make_client(handler) as client:
        saved = download_attachments(atts, tmp_path, api_key="", client=client)

    assert saved[0].parent == tmp_path  # no directory traversal escape
    assert "/" not in saved[0].name and ".." not in saved[0].name


def test_empty_attachment_list_returns_empty(tmp_path):
    assert download_attachments([], tmp_path) == []
