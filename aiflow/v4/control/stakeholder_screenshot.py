"""Decode the optional screenshot a stakeholder attaches to UAT feedback."""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from fastapi import HTTPException


def decode_screenshot(data_url: str, filename: str) -> tuple[str, bytes, str] | None:
    """(file name, content, mime type) of a PNG, JPEG or WebP data URL up to 5 MB, or None when no screenshot was sent."""
    if not data_url:
        return None
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", data_url)
    if not match:
        raise HTTPException(400, "screenshot must be a PNG, JPEG, or WebP data URL")
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "screenshot data is invalid") from exc
    if not content or len(content) > 5_000_000:
        raise HTTPException(400, "screenshot must be between 1 byte and 5 MB")
    mime = match.group(1)
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:200] or "screenshot"
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
    if not name.lower().endswith(ext):
        name = name.rsplit(".", 1)[0] + ext
    return name, content, mime
