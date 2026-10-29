"""Support bundle (moved out of api.py)."""
from __future__ import annotations

import logging

import base64
import json
import secrets
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import route_deps, support_bundle

log = logging.getLogger("aiflow4.api")  # the logger name the handlers always used
router = APIRouter()


class SupportBundleBody(BaseModel):
    ticket_key: str = Field(default="", max_length=40)


@router.post("/v1/support/bundles")
def create_support_bundle(body: SupportBundleBody, request: Request) -> dict:
    ctx = route_deps.context(request)
    creator = route_deps.console_user_for(request, {"console.manage"}, target="support_bundle", action="create")
    ticket_key = body.ticket_key.strip().upper() or None
    try:
        archive, inventory = support_bundle.build_support_bundle(ctx, ticket_key)
    except KeyError as exc:
        raise HTTPException(404, "unknown ticket") from exc
    except ValueError as exc:
        log.warning("support bundle scan blocked; sensitive content remained after sanitization")
        raise HTTPException(422, "bundle scan failed; no bundle was offered") from exc
    bundle_id = secrets.token_urlsafe(24)
    key = ctx.leases._k("support_bundle", bundle_id)
    payload = json.dumps({"archive": base64.b64encode(archive).decode("ascii"), "inventory": inventory,
                          "creator": creator.subject}, separators=(",", ":"))
    try:
        ctx.leases.r.set(key, payload, ex=600)
    except Exception as exc:  # noqa: BLE001 - do not return an unreviewable or non-shared in-memory bundle
        log.warning("temporary support bundle storage is unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "temporary bundle storage is unavailable") from exc
    return {"bundle_id": bundle_id, "inventory": inventory, "download_url": f"/v1/support/bundles/{bundle_id}/download"}


@router.get("/v1/support/bundles/{bundle_id}/download")
def download_support_bundle(bundle_id: str, request: Request) -> Response:
    ctx = route_deps.context(request)
    user = route_deps.console_user_for(request, {"console.manage"})
    key = ctx.leases._k("support_bundle", bundle_id)
    try:
        stored = ctx.leases.r.get(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("temporary support bundle storage is unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "temporary bundle storage is unavailable") from exc
    if not stored:
        raise HTTPException(404, "bundle expired or unknown")
    item = json.loads(stored)
    if not item.get("creator") or not secrets.compare_digest(str(item["creator"]), user.subject):
        raise HTTPException(403, "bundle belongs to the user who created it")
    ticket_key = item["inventory"].get("ticket_key")
    filename = f"support-bundle-{ticket_key}.zip" if ticket_key else "support-bundle.zip"
    archive = base64.b64decode(item["archive"], validate=True)
    return Response(archive, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


