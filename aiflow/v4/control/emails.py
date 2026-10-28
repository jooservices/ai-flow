"""Email address normalization shared by sign-in and first-run setup."""
from __future__ import annotations

import re

EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}")


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL.fullmatch(email):
        raise ValueError("enter a valid email address")
    return email
