import hashlib
import hmac


def verify_signature(header_signature: str | None, raw_body: bytes, secret: str) -> bool:
    """Verify Linear webhook HMAC-SHA256 signature.

    Compares hex-encoded signature from ``Linear-Signature`` header against a fresh
    HMAC-SHA256 of the raw body using ``secret``. Uses constant-time comparison.
    """
    if not header_signature or not isinstance(header_signature, str):
        return False

    try:
        provided = bytes.fromhex(header_signature)
    except ValueError:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()

    if len(provided) != len(expected):
        return False

    return hmac.compare_digest(provided, expected)
