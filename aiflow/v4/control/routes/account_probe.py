"""Legacy provider account probes, kept separate from quota-only refresh scheduling."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import shared_accounts
from ..db import J

router = APIRouter()


class ProbeBody(BaseModel):
    ok: bool
    error_kind: str = ""
    reset_at: datetime | None = None
    quota: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    duration_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model: str = ""
    credential_expires_at: datetime | None = None


def _node_auth(request: Request, authorization: str = Header(default="")) -> str:
    return request.app.state.control_api_auth["node_auth"](authorization)


@router.post("/v1/accounts/{account_id}/probe")
def account_probe(account_id: str, body: ProbeBody, request: Request,
                  node: str = Depends(_node_auth)) -> dict:
    ctx = request.app.state.ctx
    acc = ctx.repo().account(account_id)
    if not acc or not acc.runs_on(node):
        raise HTTPException(403, f"account {account_id} does not run on {node}")
    if body.quota:
        observed_at = datetime.now(UTC)
        ctx.db.run("UPDATE accounts SET quota=%s,quota_source='legacy_probe',quota_observed_at=%s WHERE id=%s"
                   " AND (quota_observed_at IS NULL OR quota_observed_at<=%s)",
                   (J(body.quota), observed_at, account_id, observed_at))
        ctx.db.run("INSERT INTO account_quota_samples (account_id,provider,model,quota,tokens_in,tokens_out,"
                   "cost_usd,duration_ms,source,observed_at,sampled_at)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'legacy_probe',%s,%s)",
                   (account_id, acc.provider, body.model, J(body.quota), body.tokens_in, body.tokens_out,
                    body.cost_usd, body.duration_ms, observed_at, observed_at))
    ctx.db.run("UPDATE accounts SET last_probe_at=now(),updated_at=now() WHERE id=%s", (account_id,))
    if "credential_expires_at" in body.model_fields_set:
        ctx.db.run("UPDATE accounts SET credential_expires_at=%s WHERE id=%s",
                   (body.credential_expires_at, account_id))
    if body.error_kind == "auth":
        request.app.state.control_api_auth["mark_machine_auth_failure"](account_id, node, body.summary)
    elif body.error_kind == "quota":
        until = body.reset_at or (datetime.now(UTC) + timedelta(minutes=30))
        seconds = max(60, int((until - datetime.now(UTC)).total_seconds()))
        ctx.leases.cooldown(account_id, seconds, "quota")
        ctx.db.run("UPDATE accounts SET status=%s,cooldown_until=%s,last_error=%s,"
                   "cooldown_since=coalesce(cooldown_since,now()),idle_since=NULL WHERE id=%s AND status<>'working'",
                   ("cooldown", until, body.summary[:300], account_id))
    elif body.ok:
        shared_accounts.mark_signed_in(ctx.db, account_id, node)
        ctx.leases.uncool(account_id)
        ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'idle' END,"
                   "cooldown_until=NULL,last_error='',cooldown_since=NULL,"
                   "idle_since=CASE WHEN status='working' THEN NULL ELSE coalesce(idle_since,now()) END"
                   " WHERE id=%s", (account_id,))
    else:
        ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'error' END,last_error=%s,"
                   "cooldown_since=NULL,idle_since=NULL WHERE id=%s",
                   (body.summary[:300] or body.error_kind, account_id))
    ctx.events.emit("account.probe", account=account_id, provider=acc.provider, node=node,
                    outcome="ok" if body.ok else (body.error_kind or "error"), reason=body.summary[:200],
                    duration_ms=body.duration_ms, tokens_in=body.tokens_in, tokens_out=body.tokens_out,
                    cost_usd=body.cost_usd)
    return {"ok": True}
