"""Install script download (moved out of api.py)."""
from __future__ import annotations

import ipaddress
import re

import shlex
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pathlib import Path

from .. import route_deps

router = APIRouter()


def valid_control_host(value: str) -> bool:
    """Accept only a DNS name or IP literal, optionally followed by a TCP port."""
    if not value or any(char.isspace() or ord(char) < 0x21 or ord(char) > 0x7e for char in value):
        return False

    port_text = ""
    if value.startswith("["):
        match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]{1,5}))?", value)
        if not match:
            return False
        try:
            if ipaddress.ip_address(match.group(1)).version != 6:
                return False
        except ValueError:
            return False
        port_text = match.group(2) or ""
    else:
        if value.count(":") > 1:
            return False
        hostname, separator, port_text = value.partition(":")
        if separator and not re.fullmatch(r"[0-9]{1,5}", port_text):
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if not re.fullmatch(
                r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?",
                hostname,
            ):
                return False
        else:
            if address.version != 4:
                return False

    return not port_text or 1 <= int(port_text) <= 65535


@router.get("/v1/install.sh", response_class=PlainTextResponse)
def node_install_script(request: Request) -> PlainTextResponse:
    script = Path(__file__).resolve().parents[4] / "deploy" / "v4" / "bin" / "aiflow4-bootstrap"
    if not script.is_file():
        raise HTTPException(404, "node bootstrap installer is unavailable")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    scheme = (forwarded_proto or request.url.scheme).lower()
    host = forwarded_host or request.headers.get("host", "") or request.url.netloc
    if scheme not in ("http", "https") or not valid_control_host(host):
        raise HTTPException(400, "invalid public control URL")
    base_url = f"{scheme}://{host}"
    return PlainTextResponse(script.read_text(encoding="utf-8").replace("__AIFLOW4_CONTROL_URL__", shlex.quote(base_url)))


