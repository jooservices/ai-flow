"""Two-factor sign-in: TOTP (RFC 6238, standard library only) and single-use recovery codes.

The authenticator secret is stored encrypted (core/secretbox.py) with a key that is not in the database; recovery codes
are stored as keyed hashes, so a database dump reveals neither. The key is `AIFLOW4_SECRET_KEY` (or its file); when none
is configured the encryption key is derived from the console session secret, which also lives outside the database.
Legacy plaintext authenticator secrets are upgraded to ciphertext at control startup. A code is accepted once (the last
used time step is kept), and a recovery code is consumed by using it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from ..core import secretbox

STEP_S = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next 30-second step, for clock drift
RECOVERY_COUNT = 10
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def key_for(session_secret: str) -> bytes:
    """The configured secret key, else one derived from the session secret (both are outside the database)."""
    return secretbox.load_key() or hashlib.sha256(b"aiflow4-mfa-key\0" + session_secret.encode()).digest()


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return str(number).zfill(DIGITS)


def totp_code(secret: str, now: float | None = None) -> str:
    return _code_at(secret, int((time.time() if now is None else now) // STEP_S))


def match_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step the 6-digit `code` belongs to, or None."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = int((time.time() if now is None else now) // STEP_S)
    found = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(_code_at(secret, step), code):
            found = step
    return found


def otpauth_uri(secret: str, email: str, issuer: str = "AI Flow") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


def new_recovery_codes(count: int = RECOVERY_COUNT) -> list[str]:
    return ["-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)) for _ in range(count)]


def normalize_recovery(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def looks_like_recovery(code: str) -> bool:
    return len(normalize_recovery(code)) == 12


def recovery_hash(code: str, key: bytes) -> str:
    return hmac.new(key, b"recovery:" + normalize_recovery(code).encode(), hashlib.sha256).hexdigest()


# --- database -----------------------------------------------------------------------------------------------------
def store_pending_secret(db, email: str, secret: str, session_secret: str) -> None:
    sealed = secretbox.encrypt(secret, key_for(session_secret), "totp:" + email)
    db.run("UPDATE console_users SET totp_secret=%s, totp_enabled=false, totp_last_step=0, updated_at=now() WHERE email=%s",
           (sealed, email))


def read_secret(row: dict, email: str, session_secret: str) -> str:
    return secretbox.decrypt(row["totp_secret"], key_for(session_secret), "totp:" + email) if row["totp_secret"] else ""


def migrate_plaintext_secrets(db, session_secret: str) -> int:
    """Upgrade legacy plaintext TOTP values before they can be used by a running control API."""
    configured_key = secretbox.load_key()
    if configured_key is None and len(session_secret.encode()) < 32:
        return 0
    key = configured_key or key_for(session_secret)
    with db.transaction() as c:
        rows = c.execute("SELECT email, totp_secret FROM console_users WHERE totp_secret<>''"
                          " AND totp_secret NOT LIKE %s", (secretbox.PREFIX + "%",)).fetchall()
        for row in rows:
            sealed = secretbox.encrypt(row["totp_secret"], key, "totp:" + row["email"])
            c.execute("UPDATE console_users SET totp_secret=%s WHERE email=%s AND totp_secret=%s",
                      (sealed, row["email"], row["totp_secret"]))
    return len(rows)


def replace_recovery_codes(db, email: str, session_secret: str) -> list[str]:
    codes = new_recovery_codes()
    key = key_for(session_secret)
    with db.transaction() as c:
        c.execute("DELETE FROM console_recovery_codes WHERE email=%s", (email,))
        for code in codes:
            c.execute("INSERT INTO console_recovery_codes (email, code_hash) VALUES (%s,%s)", (email, recovery_hash(code, key)))
    return codes


def recovery_codes_left(db, email: str) -> int:
    return int(db.one("SELECT count(*) AS n FROM console_recovery_codes WHERE email=%s AND used_at IS NULL", (email,))["n"])


def clear(db, email: str) -> None:
    with db.transaction() as c:
        c.execute("UPDATE console_users SET totp_secret='', totp_enabled=false, totp_last_step=0, updated_at=now()"
                  " WHERE email=%s", (email,))
        c.execute("DELETE FROM console_recovery_codes WHERE email=%s", (email,))


def check_second_factor(db, row: dict, email: str, code: str, session_secret: str) -> str:
    """'totp' or 'recovery' when `code` is a fresh authenticator code or an unused recovery code (which is then
    consumed), else ''."""
    if looks_like_recovery(code):
        hit = db.one("UPDATE console_recovery_codes SET used_at=now() WHERE email=%s AND code_hash=%s AND used_at IS NULL"
                     " RETURNING 1 AS ok", (email, recovery_hash(code, key_for(session_secret))))
        return "recovery" if hit else ""
    step = match_step(read_secret(row, email, session_secret), code) if row["totp_enabled"] else None
    if step is None:
        return ""
    used = db.one("UPDATE console_users SET totp_last_step=%s WHERE email=%s AND totp_last_step<%s RETURNING 1 AS ok",
                  (step, email, step))
    return "totp" if used else ""
