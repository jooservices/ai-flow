"""Encrypt stored secrets at rest (docs/v4/security.md).

AES-256-GCM (the `cryptography` package that pyjwt[crypto] already brings). The key never lives in the database:
it comes from `AIFLOW4_SECRET_KEY` (urlsafe base64 of 32 bytes) or from the file named by `AIFLOW4_SECRET_KEY_FILE`
(mode 0600, same format), so a database dump or a copy of the secret files alone reveals nothing. Ciphertext is
text, `aiflow-enc:v1:<base64 nonce+ciphertext>`; a value without that prefix is legacy plaintext and is still readable,
so turning encryption on never breaks an installation (the next write encrypts it).
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "aiflow-enc:v1:"


class SecretBoxError(ValueError):
    """The key is missing or wrong, or the stored value is damaged. The message never contains a secret."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _decode_key(value: str) -> bytes:
    value = value.strip()
    try:
        key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise SecretBoxError("the secret encryption key is not valid base64") from None
    if len(key) != 32:
        raise SecretBoxError("the secret encryption key must be 32 bytes (urlsafe base64)")
    return key


def load_key() -> bytes | None:
    """The key from the environment or the key file; None when encryption is not configured."""
    value = os.environ.get("AIFLOW4_SECRET_KEY", "")
    if not value:
        path = os.environ.get("AIFLOW4_SECRET_KEY_FILE", "")
        if not path:
            return None
        try:
            value = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise SecretBoxError(f"cannot read the secret key file: {e.strerror}") from None
    return _decode_key(value)


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt(plaintext: str, key: bytes | None = None, context: str = "") -> str:
    """Encrypt with a fresh nonce; `context` (for example the row or file name) is authenticated, not hidden."""
    key = key or load_key()
    if key is None:
        raise SecretBoxError("no secret encryption key is configured (AIFLOW4_SECRET_KEY or AIFLOW4_SECRET_KEY_FILE)")
    nonce = secrets.token_bytes(12)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt(value: str, key: bytes | None = None, context: str = "") -> str:
    """Plain text for an encrypted value; legacy plaintext is returned as it is."""
    if not is_encrypted(value):
        return value
    key = key or load_key()
    if key is None:
        raise SecretBoxError("a stored secret is encrypted but no secret encryption key is configured")
    try:
        body = value[len(PREFIX):]
        blob = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return AESGCM(key).decrypt(blob[:12], blob[12:], context.encode("utf-8")).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError):
        raise SecretBoxError("a stored secret cannot be decrypted with the configured key") from None


def seal(plaintext: str, context: str = "") -> str:
    """Encrypt when a key is configured, else keep plaintext (installations without a key keep working)."""
    key = load_key()
    return encrypt(plaintext, key, context) if key else plaintext
