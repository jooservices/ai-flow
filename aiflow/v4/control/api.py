"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from ..core.fencing import Leases
from ..core.settings import Settings
from .db import DB, J
from .events import Events
from .github import GitHubApp
from .jira import Jira
from .policy import Decision, decide

log = logging.getLogger("aiflow4.api")
PRIORITY = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}


def h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Ctx:
    settings: Settings
    db: DB
    leases: Leases
    events: Events
    repo: Callable[[], ControlRepo]
    jira: Jira | None = None
    github: GitHubApp | None = None


# --- request bodies ---------------------------------------------------------------------------------------
class JoinBody(BaseModel):
    name: str
    join_token: str
    ip: str = ""


class HeartbeatBody(BaseModel):
    slots_total: int = 1
    slots_used: int = 0
    release_version: str = ""
    tools: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    workers: list[dict[str, Any]] = Field(default_factory=list)


class ClaimBody(BaseModel):
    ticket: str
    account: str
    step: str = "dev"


class FenceBody(BaseModel):
    fence: int
    progress: dict[str, Any] = Field(default_factory=dict)


class ResultBody(BaseModel):
    fence: int
    outcome: str
    error_kind: str = ""
    reset_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    branch: str = ""
    pr_url: str = ""
    head_sha: str = ""
    summary: str = ""
    quota: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class JiraWrite(BaseModel):
    run_id: str
    fence: int
    to: str = ""
    body: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)


class GitHubTokenBody(BaseModel):
    run_id: str
    fence: int
    repo: str


class PRBody(BaseModel):
    run_id: str
    fence: int
    repo: str
    head: str
    base: str
    title: str
    body: str = ""
    draft: bool = True


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


class MergeBody(BaseModel):
    run_id: str
    fence: int
    number: int
    title: str = ""
    method: str = "squash"


class PRCommentBody(BaseModel):
    run_id: str
    fence: int
    number: int
    body: str


class EventsBody(BaseModel):
    events: list[dict[str, Any]]


# --- app --------------------------------------------------------------------------------------------------
def create_app(ctx: Ctx) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        try:
            _app.state.sync_accounts()
        except Exception as e:  # noqa: BLE001
            log.warning("account sync failed: %s", e)
        yield

    app = FastAPI(title="AI Flow v4 control API", version=VERSION, lifespan=lifespan)
    app.state.ctx = ctx

    def sync_accounts() -> None:
        repo = ctx.repo()
        for a in repo.accounts:
            ctx.db.run(
                "INSERT INTO accounts (id, provider, node, tier, roles) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider, node=EXCLUDED.node, tier=EXCLUDED.tier,"
                " roles=EXCLUDED.roles, updated_at=now()", (a.id, a.provider, a.node, a.tier, a.roles))
        for n in repo.nodes.values():
            ctx.db.run("INSERT INTO nodes (name, ip, role, slots_total) VALUES (%s,%s,%s,%s)"
                       " ON CONFLICT (name) DO UPDATE SET ip=EXCLUDED.ip, role=EXCLUDED.role, slots_total=EXCLUDED.slots_total",
                       (n.name, n.ip, n.role, n.slots))

    app.state.sync_accounts = sync_accounts
    _identity: list[tuple[str, str]] = []

    def bot_identity() -> tuple[str, str]:
        if not _identity:
            _identity.append(ctx.github.bot_identity() if ctx.github else ("aiflow4", "aiflow4@localhost"))
        return _identity[0]

    # --- auth -----------------------------------------------------------------------------------------------
    def node_auth(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        row = ctx.db.one("SELECT name FROM nodes WHERE token_hash = %s AND token_hash <> ''", (h(token),)) if token else None
        if not row:
            raise HTTPException(401, "unknown node token")
        return row["name"]

    def audit(target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
              result: str, payload: dict | None = None) -> None:
        ctx.db.run("INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (target, action, ticket, run_id, fence, actor, accepted, result[:500], J(payload or {})))

    def run_row(run_id: str) -> dict:
        try:
            uuid.UUID(run_id)
        except ValueError:
            raise HTTPException(404, "unknown run")
        row = ctx.db.one("SELECT * FROM runs WHERE id = %s", (run_id,))
        if not row:
            raise HTTPException(404, "unknown run")
        return row

    def fenced(run_id: str, fence: int, target: str, action: str, payload: dict | None = None) -> dict:
        run = run_row(run_id)
        ok = int(run["fence"]) == int(fence) and ctx.leases.valid(run["ticket_key"], str(run["id"]), int(fence))
        if not ok:
            audit(target, action, run["ticket_key"], run_id, fence, run["account_id"], False, "stale fencing token", payload)
            ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                            account=run["account_id"], node=run["node"], reason=f"{target}.{action}: stale fence")
            raise HTTPException(409, "fencing token is no longer valid")
        return run

    # --- health ---------------------------------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        ctx.db.one("SELECT 1 AS ok")
        ctx.leases.r.ping()
        return {"ok": True, "version": VERSION}

    # --- nodes ----------------------------------------------------------------------------------------------
    @app.post("/v1/nodes/join")
    def join(body: JoinBody) -> dict:
        if not ctx.settings.join_token or not secrets.compare_digest(body.join_token, ctx.settings.join_token):
            raise HTTPException(403, "invalid join token")
        token = secrets.token_urlsafe(32)
        ctx.db.run("INSERT INTO nodes (name, ip, token_hash, status) VALUES (%s,%s,%s,'joined')"
                   " ON CONFLICT (name) DO UPDATE SET token_hash=EXCLUDED.token_hash, ip=coalesce(nullif(EXCLUDED.ip,''), nodes.ip),"
                   " status='joined'", (body.name, body.ip, h(token)))
        ctx.events.emit("node.joined", node=body.name)
        return {"node": body.name, "node_token": token}

    @app.post("/v1/nodes/{name}/heartbeat")
    def node_heartbeat(name: str, body: HeartbeatBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        prev = ctx.db.one("SELECT status FROM nodes WHERE name=%s", (node,))
        ctx.db.run("UPDATE nodes SET status='online', slots_total=%s, slots_used=%s, release_version=%s, tools=%s,"
                   " metrics=%s, last_heartbeat=now() WHERE name=%s",
                   (body.slots_total, body.slots_used, body.release_version, J(body.tools), J(body.metrics), node))
        if prev and prev["status"] != "online":
            ctx.events.emit("node.online", node=node, reason=f"was {prev['status']}")
            ctx.db.run("UPDATE incidents SET closed_at=now() WHERE kind='node_down' AND subject=%s AND closed_at IS NULL", (node,))
        for w in body.workers:
            acc = str(w.get("account", ""))
            if not acc:
                continue
            state = str(w.get("state", "unknown"))
            if not w.get("alive", True):
                state = "stopped"
            # "unavailable" (last probe failed) keeps the status the probe set: cooldown, signed_out or error
            ctx.db.run("UPDATE accounts SET worker_state=%s, status=CASE WHEN cooldown_until > now() THEN 'cooldown'"
                       " WHEN %s = 'unavailable' THEN status WHEN status = 'working' AND %s = 'idle' THEN status ELSE %s END,"
                       " updated_at=now() WHERE id=%s AND node=%s", (J(w), state, state, state, acc, node))
        repo = ctx.repo()
        return {"desired": {"accounts": [a.id for a in repo.accounts_on(node)],
                            "projects": repo.nodes[node].projects if node in repo.nodes else [],
                            "versions": repo.versions}}

    # --- work -----------------------------------------------------------------------------------------------
    def last_dev(key: str) -> dict | None:
        return ctx.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                          " ORDER BY started_at DESC LIMIT 1", (key,))

    def eligible(account_id: str, node: str) -> list[dict]:
        """Tickets this account may take now: verification of finished work first, then development."""
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if ctx.leases.cooling(account_id):
            return []
        projects = [p for p in (repo.nodes[node].projects if node in repo.nodes else []) if p in repo.projects]
        if not projects:
            return []
        roles = set(acc.roles or ["dev", "verify"])
        held = set(ctx.leases.held_keys())
        out: list[dict] = []
        if "verify" in roles:
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                                " AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held:
                    continue
                dev = last_dev(t["key"])
                if not dev or dev["outcome"] != "done" or not dev["pr_url"]:
                    continue
                if dev["account_id"] == acc.id:  # never grade your own work
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                out.append({**t, "step": "verify"})
        if "dev" in roles:
            done = {r["key"] for r in ctx.db.all("SELECT key FROM tickets WHERE status = 'Done'")}
            dev_rows = []
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'"
                                " AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held or any(b not in done for b in t["blocked_by"]):
                    continue
                labels = set(t["labels"])
                if "tier-senior" in labels and acc.tier != "senior":
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in labels:
                    continue
                dev_rows.append({**t, "step": "dev"})
            dev_rows.sort(key=lambda t: ("rework" not in t["labels"], PRIORITY.get(t["priority"], 2),
                                         t["jira_created"] or utcnow()))
            out += dev_rows
        return out

    @app.get("/v1/work/candidates")
    def candidates(account: str, node: str = Depends(node_auth)) -> dict:
        return {"tickets": [{"key": t["key"], "summary": t["summary"], "priority": t["priority"], "labels": t["labels"],
                             "step": t["step"]} for t in eligible(account, node)[:20]]}

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(node_auth)) -> Any:
        tickets = {(t["key"], t["step"]): t for t in eligible(body.account, node)}
        t = tickets.get((body.ticket, body.step))
        if not t:
            return JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
        repo = ctx.repo()
        acc = repo.account(body.account)
        run_id = str(uuid.uuid4())
        c = ctx.leases.claim(t["key"], run_id, ctx.settings.lease_ms)
        if not c.ok:
            return JSONResponse({"claimed": False, "reason": f"held by {c.holder}"}, status_code=409)
        model, effort = repo.model_for(acc.provider, "senior" if "tier-senior" in t["labels"] else acc.tier)
        run_token = secrets.token_urlsafe(32)
        ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, model, node, fence, token_hash)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (run_id, t["key"], t["project"], body.step, acc.id, acc.provider, model, node, c.fence, h(run_token)))
        ctx.db.run("UPDATE accounts SET status='working', updated_at=now() WHERE id=%s", (acc.id,))
        ctx.events.emit("run.claimed", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                        fence=c.fence, account=acc.id, provider=acc.provider, model=model, node=node)
        p = repo.projects[t["project"]]
        extra: dict[str, Any] = {}
        dev = last_dev(t["key"])
        if body.step == "verify" and dev:
            extra["pr"] = {"url": dev["pr_url"], "number": int(dev["pr_url"].rstrip("/").split("/")[-1]),
                           "branch": dev["branch"], "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
                           "dev_summary": dev["summary"]}
        if body.step == "dev":
            v = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='verify'"
                           " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if v and (not dev or v["started_at"] > dev["started_at"]):
                extra["findings"] = (v["details"] or {}).get("findings") or v["summary"]
            if dev and dev["branch"]:
                extra["branch"] = dev["branch"]
        return {**extra, "claimed": True, "step": body.step, "run_id": run_id, "fence": c.fence, "run_token": run_token,
                "lease_ms": ctx.settings.lease_ms, "model": model, "effort": effort, "provider": acc.provider,
                "account": acc.id, "identity": bot_identity(),
                "ticket": {k: t[k] for k in ("key", "project", "summary", "description", "labels", "priority", "type")},
                "project": {"key": p.key, "repo": p.repo, "base": p.base, "hotfix_base": p.hotfix_base,
                            "checks": p.checks, "stack": p.stack, "work_minutes": p.work_minutes(),
                            "merge": {"method": (p.merge or {}).get("method", "squash"),
                                      "auto": bool((p.merge or {}).get("auto", True)),
                                      "require_human": bool((p.merge or {}).get("require_human", False))}}}

    # --- runs -----------------------------------------------------------------------------------------------
    @app.post("/v1/runs/{run_id}/heartbeat")
    def run_heartbeat(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        ok = int(run["fence"]) == body.fence and ctx.leases.renew(run["ticket_key"], run_id, body.fence, ctx.settings.lease_ms)
        if not ok:
            ctx.db.run("UPDATE runs SET status='lost', outcome='lost' WHERE id=%s AND status='running'", (run_id,))
            ctx.events.emit("run.lost", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                            account=run["account_id"], node=node, reason="lease expired or taken")
            raise HTTPException(409, "lease lost")
        ctx.db.run("UPDATE runs SET heartbeat_at=now(), progress=progress || %s WHERE id=%s", (J(body.progress), run_id))
        return {"ok": True, "lease_ms": ctx.settings.lease_ms}

    @app.post("/v1/runs/{run_id}/result")
    def run_result(run_id: str, body: ResultBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node:
            raise HTTPException(403, "run belongs to another node")
        still_owner = ctx.leases.valid(run["ticket_key"], run_id, body.fence)
        ctx.db.run("UPDATE runs SET outcome=%s, error_kind=%s, reset_at=%s, tokens_in=tokens_in+%s, tokens_out=tokens_out+%s,"
                   " cost_usd=cost_usd+%s, branch=coalesce(nullif(%s,''),branch), pr_url=coalesce(nullif(%s,''),pr_url),"
                   " head_sha=coalesce(nullif(%s,''),head_sha), summary=%s, details=details || %s, status='finished', ended_at=now()"
                   " WHERE id=%s",
                   (body.outcome, body.error_kind, body.reset_at, body.tokens_in, body.tokens_out, body.cost_usd,
                    body.branch, body.pr_url, body.head_sha, body.summary[:2000], J(body.details), run_id))
        if body.quota:
            ctx.db.run("UPDATE accounts SET quota=%s, updated_at=now() WHERE id=%s", (J(body.quota), run["account_id"]))
        if body.error_kind in ("quota", "auth"):
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            ctx.leases.cooldown(run["account_id"], seconds, body.error_kind)
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s, updated_at=now() WHERE id=%s",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300], run["account_id"]))
        else:
            ctx.db.run("UPDATE accounts SET status='idle', last_error=CASE WHEN %s IN ('failed','timeout') THEN %s ELSE '' END,"
                       " updated_at=now() WHERE id=%s AND status='working'", (body.outcome, body.summary[:300], run["account_id"]))
        dur = int((utcnow() - run["started_at"]).total_seconds() * 1000)
        ctx.events.emit("run.finished", ticket=run["ticket_key"], project=run["project"], step=run["step"], run_id=run_id,
                        fence=body.fence, account=run["account_id"], provider=run["provider"], model=run["model"],
                        node=node, duration_ms=dur, outcome=body.outcome, reason=body.error_kind or body.summary[:200],
                        tokens_in=body.tokens_in, tokens_out=body.tokens_out, cost_usd=body.cost_usd,
                        still_owner=still_owner, pr_url=body.pr_url)
        return {"ok": True, "still_owner": still_owner}

    @app.post("/v1/runs/{run_id}/release")
    def run_release(run_id: str, body: FenceBody, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        released = ctx.leases.release(run["ticket_key"], run_id, body.fence)
        ctx.db.run("UPDATE runs SET status=CASE WHEN status='running' THEN 'released' ELSE status END,"
                   " ended_at=coalesce(ended_at, now()) WHERE id=%s", (run_id,))
        ctx.db.run("UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'", (run["account_id"],))
        ctx.events.emit("run.released", ticket=run["ticket_key"], run_id=run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="released" if released else "not_owner")
        return {"released": released}

    # --- Jira writes (fenced) ---------------------------------------------------------------------------------
    @app.post("/v1/jira/{key}/transition")
    def jira_transition(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "transition", {"to": body.to})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        status = ctx.jira.transition_to(key, body.to)
        ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, key))
        audit("jira", "transition", key, body.run_id, body.fence, run["account_id"], True, status, {"to": body.to})
        ctx.events.emit("jira.transition", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                        node=node, outcome=status)
        return {"status": status}

    @app.post("/v1/jira/{key}/comment")
    def jira_comment(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "comment", {"chars": len(body.body)})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        cid = ctx.jira.comment(key, body.body)
        audit("jira", "comment", key, body.run_id, body.fence, run["account_id"], True, cid, {"chars": len(body.body)})
        return {"comment_id": cid}

    @app.post("/v1/jira/{key}/fields")
    def jira_fields(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "fields", {"fields": list(body.fields), "add": body.add_labels})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if body.fields:
            ctx.jira.set_fields(key, body.fields)
        for lab in body.add_labels:
            ctx.jira.add_label(key, lab)
        for lab in body.remove_labels:
            ctx.jira.remove_label(key, lab)
        audit("jira", "fields", key, body.run_id, body.fence, run["account_id"], True, "ok",
              {"fields": list(body.fields), "add": body.add_labels, "remove": body.remove_labels})
        return {"ok": True}

    # --- GitHub (fenced) --------------------------------------------------------------------------------------
    @app.post("/v1/github/token")
    def github_token(body: GitHubTokenBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        if not project or project.repo.split("/")[-1] != body.repo.split("/")[-1]:
            raise HTTPException(403, "repository does not belong to the run's project")
        token, expires = ctx.github.token(project.repo_name, {"contents": "write", "metadata": "read", "workflows": "write"})
        audit("github", "token", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "issued",
              {"repo": project.repo, "expires": expires.isoformat()})
        return {"token": token, "expires_at": expires.isoformat()}

    @app.post("/v1/github/pr")
    def github_pr(body: PRBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "pr", {"repo": body.repo, "head": body.head})
        project = ctx.repo().projects.get(run["project"])
        if not project or project.repo_name != body.repo.split("/")[-1]:
            raise HTTPException(403, "repository does not belong to the run's project")
        pr = ctx.github.create_pr(project.repo_name, body.head, body.base, body.title, body.body, body.draft)
        ctx.db.run("UPDATE runs SET pr_url=%s, branch=%s WHERE id=%s", (pr["html_url"], body.head, body.run_id))
        audit("github", "pr", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, pr["html_url"],
              {"number": pr.get("number"), "draft": body.draft})
        ctx.events.emit("github.pr", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="opened", pr_url=pr["html_url"])
        return {"url": pr["html_url"], "number": pr.get("number")}

    @app.post("/v1/github/merge")
    def github_merge(body: MergeBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "merge", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run merges")
        project = ctx.repo().projects[run["project"]]
        res = ctx.github.merge_pr(project.repo_name, body.number, body.title, body.method)
        ok = bool(res.get("merged"))
        audit("github", "merge", run["ticket_key"], body.run_id, body.fence, run["account_id"], ok,
              res.get("sha") or res.get("message", ""), {"number": body.number, "method": body.method})
        ctx.events.emit("github.merge", ticket=run["ticket_key"], run_id=body.run_id, fence=body.fence,
                        account=run["account_id"], node=node, outcome="merged" if ok else "refused",
                        reason=res.get("message", "")[:200])
        return res

    @app.post("/v1/github/comment")
    def github_comment(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "comment", {"number": body.number})
        project = ctx.repo().projects[run["project"]]
        ctx.github.comment(project.repo_name, body.number, body.body)
        audit("github", "comment", run["ticket_key"], body.run_id, body.fence, run["account_id"], True, "ok",
              {"number": body.number})
        return {"ok": True}

    # --- policy hook ------------------------------------------------------------------------------------------
    @app.post("/v1/policy/decide")
    async def policy_decide(request: Request, authorization: str = Header(default="")) -> dict:
        payload = await request.json()
        token = authorization.removeprefix("Bearer ").strip()
        run = ctx.db.one("SELECT * FROM runs WHERE token_hash=%s AND status='running'", (h(token),)) if token else None
        tool = str(payload.get("tool_name", ""))
        if not run:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "no active AI Flow run for this token"}}
        if run["step"] == "verify" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            d = Decision("deny", "a verify run reviews and tests; it does not edit code")
        else:
            d = decide(ctx.repo().policy_for(run["project"]), tool, payload.get("tool_input") or {},
                       str(payload.get("cwd") or ""))
        ctx.events.emit("policy.decision", ticket=run["ticket_key"], run_id=str(run["id"]), account=run["account_id"],
                        node=run["node"], outcome=d.decision, reason=d.reason[:200], tool=tool,
                        command=str((payload.get("tool_input") or {}).get("command", ""))[:300])
        return d.hook_output()

    # --- events from nodes ------------------------------------------------------------------------------------
    # --- account probes: a tiny prompt tells whether an account can work and how much quota is left -------------
    @app.get("/v1/accounts/{account_id}/spec")
    def account_spec(account_id: str, node: str = Depends(node_auth)) -> dict:
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        model, effort = repo.model_for(acc.provider, acc.tier)
        return {"account": acc.id, "provider": acc.provider, "tier": acc.tier, "model": model, "effort": effort}

    @app.post("/v1/accounts/{account_id}/probe")
    def account_probe(account_id: str, body: ProbeBody, node: str = Depends(node_auth)) -> dict:
        acc = ctx.repo().account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if body.quota:
            ctx.db.run("UPDATE accounts SET quota=%s WHERE id=%s", (J(body.quota), account_id))
        ctx.db.run("UPDATE accounts SET last_probe_at=now(), updated_at=now() WHERE id=%s", (account_id,))
        if body.error_kind in ("quota", "auth"):
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            ctx.leases.cooldown(account_id, seconds, body.error_kind)
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s WHERE id=%s AND status<>'working'",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300], account_id))
        elif body.ok:
            ctx.leases.uncool(account_id)
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'idle' END,"
                       " cooldown_until=NULL, last_error='' WHERE id=%s", (account_id,))
        else:
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'error' END, last_error=%s"
                       " WHERE id=%s", (body.summary[:300] or body.error_kind, account_id))
        ctx.events.emit("account.probe", account=account_id, provider=acc.provider, node=node,
                        outcome="ok" if body.ok else (body.error_kind or "error"), reason=body.summary[:200],
                        duration_ms=body.duration_ms, tokens_in=body.tokens_in, tokens_out=body.tokens_out,
                        cost_usd=body.cost_usd)
        return {"ok": True}

    @app.post("/v1/events")
    def events_in(body: EventsBody, node: str = Depends(node_auth)) -> dict:
        n = 0
        for e in body.events[:500]:
            e = dict(e)
            t = str(e.pop("type", "node.event"))
            e["node"] = node
            ctx.events.emit(t, **e)
            n += 1
        return {"accepted": n}

    # --- state for dashboards ---------------------------------------------------------------------------------
    @app.get("/v1/state")
    def state() -> dict:
        from .state import snapshot
        return snapshot(ctx)

    return app
