"""Encrypt runtime-setting handoffs so node operation API responses never contain the plaintext value."""
from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PRIVATE_KEY_FILE = Path("/srv/aiflow/state/runtime-settings.key")


def node_public_key(private_key_file: str | Path | None = None) -> str:
    """Create or load the node's root-only RSA key and return its public half for heartbeats."""
    path = Path(private_key_file or os.environ.get("AIFLOW4_NODE_OPERATION_KEY_FILE") or PRIVATE_KEY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise ValueError("node operation key path is not a regular file")
    if path.is_file():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("node operation key has an unsupported type")
        os.chmod(path, 0o600)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def encrypt_value(public_pem: str, value: str, context: str) -> dict[str, str]:
    """Use RSA-OAEP for a one-use AES-256-GCM key and bind the ciphertext to a change id."""
    public = serialization.load_pem_public_key(public_pem.encode("ascii"))
    if not isinstance(public, rsa.RSAPublicKey):
        raise ValueError("node operation public key has an unsupported type")
    key, nonce = os.urandom(32), os.urandom(12)
    aad = context.encode("ascii")
    encrypted_key = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                                      algorithm=hashes.SHA256(), label=aad))
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), aad)
    encode = lambda data: base64.urlsafe_b64encode(data).decode("ascii")
    return {"key": encode(encrypted_key), "nonce": encode(nonce), "ciphertext": encode(ciphertext)}


def decrypt_value(envelope: dict[str, str], context: str,
                  private_key_file: str | Path | None = None) -> str:
    """Decrypt only on the target node; invalid or stale envelopes fail without exposing their contents."""
    path = Path(private_key_file or os.environ.get("AIFLOW4_NODE_OPERATION_KEY_FILE") or PRIVATE_KEY_FILE)
    if path.is_symlink() or not path.is_file():
        raise ValueError("node operation private key is unavailable")
    private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(private, rsa.RSAPrivateKey):
        raise ValueError("node operation private key has an unsupported type")
    decode = base64.urlsafe_b64decode
    aad = context.encode("ascii")
    key = private.decrypt(decode(envelope["key"]), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                                                  algorithm=hashes.SHA256(), label=aad))
    plaintext = AESGCM(key).decrypt(decode(envelope["nonce"]), decode(envelope["ciphertext"]), aad)
    return plaintext.decode("utf-8")
