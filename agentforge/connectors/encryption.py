"""Fernet-based token encryption for connector credentials."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ._config import get_encryption_key

_fernet_instance: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key = get_encryption_key()
    if not key:
        raise RuntimeError(
            "Connector encryption key not set. Add encryption_key under "
            "connectors in config.yaml, or set CONNECTOR_ENCRYPTION_KEY.\n"
            "Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet_instance


def encrypt_tokens(token_dict: dict[str, Any]) -> str:
    """Encrypt a token dict to a Fernet-encoded string."""
    plaintext = json.dumps(token_dict).encode()
    return _get_fernet().encrypt(plaintext).decode()


def decrypt_tokens(encrypted: str) -> dict[str, Any]:
    """Decrypt a Fernet-encoded string back to a token dict."""
    try:
        plaintext = _get_fernet().decrypt(encrypted.encode())
        return json.loads(plaintext.decode())
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt token — key may have changed") from exc
