import hashlib
import hmac

from app.signature import verify_signature


SECRET = "lin_wh_secret_test_value"


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true():
    body = b'{"action":"update","type":"Issue"}'
    sig = _sign(SECRET, body)
    assert verify_signature(sig, body, SECRET) is True


def test_invalid_signature_returns_false():
    body = b'{"action":"update","type":"Issue"}'
    assert verify_signature("deadbeef" * 8, body, SECRET) is False


def test_wrong_secret_returns_false():
    body = b'{"action":"update","type":"Issue"}'
    sig = _sign("other-secret", body)
    assert verify_signature(sig, body, SECRET) is False


def test_tampered_body_returns_false():
    body = b'{"action":"update","type":"Issue"}'
    sig = _sign(SECRET, body)
    tampered = b'{"action":"remove","type":"Issue"}'
    assert verify_signature(sig, tampered, SECRET) is False


def test_missing_signature_returns_false():
    body = b'{"action":"update"}'
    assert verify_signature("", body, SECRET) is False
    assert verify_signature(None, body, SECRET) is False


def test_malformed_hex_signature_returns_false():
    body = b'{"action":"update"}'
    assert verify_signature("not-hex-at-all-zzz", body, SECRET) is False


def test_uppercase_hex_signature_accepted():
    body = b'{"action":"update"}'
    sig = _sign(SECRET, body).upper()
    assert verify_signature(sig, body, SECRET) is True
