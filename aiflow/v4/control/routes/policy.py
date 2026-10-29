"""Policy decision (moved out of api.py)."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request

from .. import route_deps
from ..policy import Decision, decide, decide_read_only, explain
from ..route_deps import h

router = APIRouter()


@router.post("/v1/policy/decide")
async def policy_decide(request: Request, authorization: str = Header(default="")) -> dict:
    ctx = route_deps.context(request)
    payload = await request.json()
    token = authorization.removeprefix("Bearer ").strip()
    run = ctx.db.one("SELECT * FROM runs WHERE token_hash=%s AND status='running'", (h(token),)) if token else None
    tool = str(payload.get("tool_name", ""))
    if not run:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "no active AI Flow run for this token"}}
    if run["step"] == "master":
        d = Decision("deny", "master runs return an action plan; all effects go through the fenced master API")
    elif run["step"] == "triage":
        d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                             str(payload.get("cwd") or ""),
                             "a triage run reads the base branch and cannot edit files")
    elif run["step"] == "verify" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        d = Decision("deny", "a verify run reviews and tests; it does not edit code")
    elif run["step"] == "intake":
        d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                             str(payload.get("cwd") or ""), "an intake run rewrites the ticket text; it does not change files")
    elif run["step"] == "impact":
        d = decide_read_only(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                             str(payload.get("cwd") or ""), "an impact run assesses a change request; it does not change files")
    else:
        details = run.get("details") or {}
        selected = details.get("repository_settings") or {}
        d = decide(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                   str(payload.get("cwd") or ""), containers=bool(selected.get("containers")))
    d = explain(d, tool, payload.get("tool_input") or {})  # every refusal names the allowed alternative
    ctx.events.emit("policy.decision", ticket=run["ticket_key"], run_id=str(run["id"]), account=run["account_id"],
                    node=run["node"], outcome=d.decision, reason=d.reason[:200], tool=tool,
                    command=str((payload.get("tool_input") or {}).get("command", ""))[:300])
    return d.hook_output()


