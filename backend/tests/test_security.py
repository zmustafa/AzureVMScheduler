from datetime import datetime, timezone

import pytest

from app.connections import decrypt_value, encrypt_value
from app.oidc import read_state, validate_return_url


def test_encrypted_oidc_state_round_trip() -> None:
    state = encrypt_value('{"iat":%d,"return_url":"/settings","verifier":"v","nonce":"n"}' % int(datetime.now(timezone.utc).timestamp()))
    assert read_state(state)["return_url"] == "/settings"


def test_rejects_protocol_relative_return_url() -> None:
    with pytest.raises(Exception):
        validate_return_url("//evil.example/path")


def test_secret_encryption_is_not_plaintext() -> None:
    encrypted = encrypt_value("highly-secret")
    assert "highly-secret" not in encrypted
    assert decrypt_value(encrypted) == "highly-secret"

def test_a_passphrase_fernet_key_is_accepted_and_stable() -> None:
    """Deployment templates cannot produce 32 url-safe base64 bytes, so any string must work."""
    from cryptography.fernet import Fernet

    from app import connections
    from app.config import get_settings

    settings = get_settings()
    original = settings.fernet_key
    try:
        settings.fernet_key = "a plain deployment passphrase"
        token = connections._fernet().encrypt(b"secret")
        # A second load of the same passphrase must derive the identical key, or every restart
        # would invalidate the stored Azure credentials.
        assert connections._fernet().decrypt(token) == b"secret"

        # A real Fernet key is still used verbatim rather than derived from.
        raw = Fernet.generate_key().decode()
        settings.fernet_key = raw
        assert connections._fernet().decrypt(Fernet(raw.encode()).encrypt(b"x")) == b"x"
    finally:
        settings.fernet_key = original
