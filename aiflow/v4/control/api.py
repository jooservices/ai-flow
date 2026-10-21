"""Control API: the single point nodes and workers talk to.

Every write to Jira or GitHub goes through here and is accepted only with a valid (run_id, fence) pair, so
fencing is enforced in exactly one place; every accepted or refused write is recorded in write_audit.
"""
from __future__ import annotations

import hashlib
import base64
import binascii
import ipaddress
import re
import logging
import secrets
import shlex
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import VERSION
from ..core.controlrepo import ControlRepo
from ..core.fencing import Leases
from ..core.settings import Settings
from ..core.toolchains import missing_tools, pinned_version, toolchain_gaps
from . import business, knowledge, memory
from .console_auth import ConsoleUser, COOKIE_NAME, verify_session
from . import projects as project_lifecycle
from .incidents import flow_bug_candidates, suggested_flow_fix
from .db import DB, J
from .events import Events
from .github import GitHubApp
from .jira import Jira
from .policy import Decision, decide, decide_master_action, decide_read_only
from .report import ReportNotFound, build_report, render_csv, render_html, render_markdown
from . import notify

log = logging.getLogger("aiflow4.api")
PRIORITY = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4}
MASTER_SCOPE = "master:global"
MASTER_CYCLE_S = 60
RELEASES = Path("/srv/aiflow/control/releases")
_NON_ROUTABLE_JOIN_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32",
))
# A ticket whose recent dev runs failed waits before the next try: 2, 4, 8, 16 then 30 minutes after the last
# failure (consecutive failures since the last run that produced work), so a broken ticket cannot spin.
BACKOFF_SQL = """
WITH last AS (
  SELECT ticket_key, outcome, ended_at,
         row_number() OVER (PARTITION BY ticket_key ORDER BY started_at DESC) AS n
  FROM runs WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
), streak AS (
  SELECT l.ticket_key, count(*) AS fails, max(l.ended_at) AS last_end
  FROM last l
  WHERE l.outcome IN ('failed', 'timeout', 'lost')
    AND NOT EXISTS (SELECT 1 FROM last g WHERE g.ticket_key = l.ticket_key AND g.n < l.n
                    AND g.outcome NOT IN ('failed', 'timeout', 'lost'))
  GROUP BY l.ticket_key
)
SELECT ticket_key, fails FROM streak
WHERE last_end > now() - make_interval(mins => LEAST(30, (2 ^ LEAST(fails, 5))::int))
"""
# An Intake ticket whose last intake run failed waits 10 minutes before the next refinement try (the same for the
# impact assessment of a change request).
INTAKE_BACKOFF_SQL = """
SELECT DISTINCT ON (ticket_key) ticket_key, outcome, ended_at FROM runs
WHERE step = %(step)s AND status <> 'running' AND started_at > now() - interval '1 day'
ORDER BY ticket_key, started_at DESC
"""


def h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validated_join_control_url(value: str) -> str:
    """Validate the operator-configured API address advertised to a new machine."""
    control = str(value or "").strip()
    if not control or "\\" in control or any(ord(char) < 32 or char.isspace() for char in control):
        raise ValueError("join control URL is not configured")
    try:
        parsed = urlsplit(control)
        host = parsed.hostname or ""
        # Accessing .port validates malformed ports as well.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("join control URL is invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
            or parsed.query or parsed.fragment or "?" in control or "#" in control):
        raise ValueError("join control URL must be an HTTP(S) URL without credentials, query, or fragment")
    normalized_host = host.rstrip(".").lower()
    if normalized_host == "localhost" or normalized_host.endswith(".localhost"):
        raise ValueError("join control URL must not point to localhost")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            # Some URL stacks accept abbreviated or integer-form IPv4 literals; normalize those before checking.
            address = ipaddress.ip_address(socket.inet_aton(normalized_host))
        except (OSError, ValueError):
            address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address and (address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast
                    or address.is_reserved or any(address.version == net.version and address in net
                                                  for net in _NON_ROUTABLE_JOIN_NETWORKS)):
        raise ValueError("join control URL must use an address reachable from joining nodes")
    return control.rstrip("/")


def join_bootstrap_command(token: str, name: str, control: str, version: str, checksum: str) -> str:
    """One line for a machine without AI Flow: fetch the signed release with the join token, check its SHA-256 and
    cosign signature, install it with the release's own aiflow4-node-install, then join with the full binary path.
    The script travels to `sudo bash -s` on stdin, so the token never appears in a process argument list."""
    identity = f"https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/{version}"
    steps = [
        "set -euo pipefail",
        f"T={shlex.quote(token)}", f"C={shlex.quote(control)}", f"V={shlex.quote(version)}",
        f"S={shlex.quote(checksum)}", f"N={shlex.quote(name)}",
        'D=$(mktemp -d)', "trap 'rm -rf \"$D\"' EXIT", 'A="$D/aiflow4-$V.tar.gz"',
        "get() { curl -fsS --max-time 600 -K <(printf 'header = \"Authorization: Bearer %s\"\\n' \"$T\")"
        ' -o "$2" "$C$1"; }',
        'get "/v1/releases/$V.tar.gz" "$A"', 'get "/v1/releases/$V.tar.gz.sigstore.json" "$A.sigstore.json"',
        'echo "$S  $A" | sha256sum -c --quiet -',
        'command -v cosign >/dev/null || { echo "install cosign to verify the AI Flow release" >&2; exit 3; }',
        f'cosign verify-blob "$A" --bundle "$A.sigstore.json" --certificate-identity {shlex.quote(identity)}'
        " --certificate-oidc-issuer https://token.actions.githubusercontent.com",
        'mkdir "$D/src"', 'tar -xzf "$A" -C "$D/src"',
        "install -d -m 0755 /etc/aiflow",
        "[ -e /etc/aiflow/node.env ] || printf 'AIFLOW4_CONTROL_URL=%s\\nAIFLOW4_NODE_NAME=%s\\nAIFLOW4_SLOTS=1\\n'"
        ' "$C" "$N" > /etc/aiflow/node.env',
        'bash "$D/src/deploy/v4/bin/aiflow4-node-install" "$A" "$V" "$A.sigstore.json" "$S"',
        '/srv/aiflow/current/.venv/bin/aiflow4 node join --name "$N" --join-token-stdin --control "$C" <<<"$T"',
        "systemctl restart aiflow4-node-agent",
    ]
    return f"printf '%s\\n' {shlex.quote('; '.join(steps))} | sudo bash -s"


@dataclass
class Ctx:
    settings: Settings
    db: DB
    leases: Leases
    events: Events
    repo: Callable[[], ControlRepo]
    jira: Jira | None = None
    github: GitHubApp | None = None
    refresh_repo: Callable[[], None] | None = None


def notify_human_review(ctx: Ctx, key: str, ticket: dict | None = None) -> list[str]:
    """Notify the Jira assignee, or the configured owner, when a required human review starts."""
    ticket = ticket or ctx.db.one("SELECT key, project, labels, summary, fields FROM tickets WHERE key=%s", (key,))
    if not ticket or "require-human" not in (ticket.get("labels") or []):
        return []
    fields = ticket.get("fields") or {}
    assignee = ticket.get("assignee") or fields.get("assignee") or {}
    email = assignee.get("email") if isinstance(assignee, dict) else ""
    title = f"{key} is ready for human review"
    detail = ticket.get("summary") or key
    link = f"{ctx.settings.jira_site.rstrip('/')}/browse/{key}" if ctx.settings.jira_site else ""
    sent = notify.send("info", title, detail, link, recipient=email or "")
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES ('notify','human-review',%s,'control',true,%s,%s)",
               (key, ",".join(sent) or "no channel configured", J({"assignee": bool(email)})))
    ctx.events.emit("notify.human_review", ticket=key, project=ticket.get("project", ""), account="control",
                    outcome="sent" if sent else "no_channel", reason="require-human")
    return sent


# --- request bodies ---------------------------------------------------------------------------------------
class JoinBody(BaseModel):
    name: str
    join_token: str
    ip: str = ""


class HeartbeatBody(BaseModel):
    slots_total: int = 1
    slots_used: int = 0
    release_version: str = ""
    release_protocol: int = 0
    tools: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    workers: list[dict[str, Any]] = Field(default_factory=list)
    credential_expiries: dict[str, datetime | None] = Field(default_factory=dict)


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
    credential_expires_at: datetime | None = None


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


class TriageAlertBody(BaseModel):
    run_id: str
    fence: int
    detail: str


class ReviewerBody(BaseModel):
    comment: str = ""


class MasterActionBody(BaseModel):
    run_id: str
    fence: int
    action: str
    ticket: str = ""
    intent: str = ""
    category: str = ""  # infrastructure | secret | production_release: always escalated to the owner
    headline: str = "follow up"
    what: str = ""
    why: str = ""
    result: str = ""
    next: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)
    kind: str = "master_follow_up"
    subject: str = ""
    severity: str = "warning"
    title: str = ""
    detail: str = ""
    target_status: str = ""


class MasterFlowBugsBody(BaseModel):
    run_id: str
    fence: int


class ChangeRequestBody(BaseModel):
    title: str = ""
    description: str = ""
    epic: str = ""


class CloseProjectBody(BaseModel):
    actor: str = "owner"


class KnowledgeBody(BaseModel):
    id: str = ""
    type: str
    title: str
    content: str
    actor: str
    archived: bool = False
    version: int | None = None


class KnowledgeApprovalBody(BaseModel):
    approved_by: str = "owner"


class KnowledgeRestoreBody(BaseModel):
    version: int
    actor: str


class StakeholderVerdictBody(BaseModel):
    change_key: str
    target_key: str = ""
    environment: Literal["develop", "production"]
    verdict: Literal["expected", "not_expected"]
    sender: str = Field(min_length=1, max_length=200)
    release_commit: str = Field(default="", max_length=500)
    expected: str = Field(default="", max_length=10_000)
    actual: str = Field(default="", max_length=10_000)
    screenshot_data: str = Field(default="", max_length=7_000_000)
    screenshot_filename: str = Field(default="screenshot.png", max_length=255)


class EventsBody(BaseModel):
    events: list[dict[str, Any]]


class WorktreeSweepBody(BaseModel):
    dirs: list[str] = []   # "<project>/<directory>" entries found under /srv/aiflow/work on the node


class ReleaseFailureBody(BaseModel):
    release_version: str
    detail: str = ""


class NodeOperationBody(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["install", "repair"]
    component: str = ""


class NodeOperationResultBody(BaseModel):
    status: Literal["completed", "failed"]
    result: dict[str, Any] = Field(default_factory=dict)


class JoinCommandBody(BaseModel):
    model_config = {"extra": "forbid"}
    name: str


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
            # Keep the IP a node reported at join: a restart must not replace where it was last seen with
            # the control repo value. Only a first insert (no join yet) takes the configured address.
            ctx.db.run("INSERT INTO nodes (name, ip, role, slots_total) VALUES (%s,%s,%s,%s)"
                       " ON CONFLICT (name) DO UPDATE SET role=EXCLUDED.role, slots_total=EXCLUDED.slots_total",
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

    def release_reader(authorization: str = Header(default="")) -> str:
        """A node token, or an unexpired, unconsumed join token so a new machine can fetch the signed release."""
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            row = ctx.db.one("SELECT node FROM node_join_tokens WHERE token_hash=%s AND consumed_at IS NULL"
                             " AND expires_at > now()", (h(token),))
            if row:
                return row["node"]
        return node_auth(authorization)

    def reviewer_auth(authorization: str = Header(default="")) -> str:
        configured = ctx.settings.reviewer_token
        if not configured:
            raise HTTPException(503, "reviewer actions are not configured")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or not secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            raise HTTPException(401, "reviewer authorization required",
                                headers={"WWW-Authenticate": "Bearer"})
        return "reviewer"

    def owner_auth(authorization: str = Header(default="")) -> str:
        configured = ctx.settings.owner_token
        if not configured:
            raise HTTPException(503, "owner actions are not configured")
        scheme, separator, token = authorization.partition(" ")
        if (not separator or scheme.lower() != "bearer" or not token.strip()
                or not secrets.compare_digest(token.strip().encode("utf-8"), configured.encode("utf-8"))):
            raise HTTPException(401, "owner authorization required",
                                headers={"WWW-Authenticate": "Bearer"})
        return "owner"

    def audit(target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str, accepted: bool,
              result: str, payload: dict | None = None) -> None:
        with ctx.db.pool.connection() as connection:
            audit_in(connection, target, action, ticket, run_id, fence, actor, accepted, result, payload)

    def audit_in(connection, target: str, action: str, ticket: str, run_id: str, fence: int | None, actor: str,
                  accepted: bool, result: str, payload: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result, payload)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (target, action, ticket, run_id, fence, actor, accepted, result[:500], J(payload or {})))

    def console_user(request: Request, roles: set[str], target: str = "console", action: str = "read",
                     node: str = "") -> ConsoleUser:
        """Require a signed, unexpired console session. Role and subject headers are deliberately ignored."""
        secret = ctx.settings.console_session_secret
        if len(secret.encode()) < 32:
            if action != "read":
                audit(target, action, node, "", None, "", False, "console session signing key is not configured")
            raise HTTPException(503, "console session authentication is not configured")
        user = verify_session(request.cookies.get(COOKIE_NAME, ""), secret)
        if not user:
            if action != "read":
                audit(target, action, node, "", None, "", False, "authenticated console session required")
            raise HTTPException(401, "authenticated console session required")
        if user.role not in roles:
            if action != "read":
                audit(target, action, node, "", None, user.subject, False, "owner role required")
            raise HTTPException(403, "owner role required")
        return user

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
        if run["step"] == "master" and target != "master":
            audit(target, action, run["ticket_key"], run_id, fence, run["account_id"], False,
                  "master writes must use the restricted master action API", payload)
            ctx.events.emit("write.refused", ticket=run["ticket_key"], run_id=run_id, fence=fence,
                            account=run["account_id"], node=run["node"],
                            reason=f"{target}.{action}: master writes require the restricted action API")
            raise HTTPException(403, "master writes must use the restricted master action API")
        return run

    def master_run(run_id: str, fence: int, node: str, action: str, payload: dict | None = None) -> dict:
        run = fenced(run_id, fence, "master", action, payload)
        if run["step"] != "master":
            raise HTTPException(403, "only a master run can use this endpoint")
        if run["node"] != node:
            raise HTTPException(403, "master run belongs to another node")
        return run

    def reviewer_run(key: str) -> tuple[dict, str, int, int]:
        ticket = ctx.db.one("SELECT key, project, status, labels, summary FROM tickets WHERE key=%s", (key,))
        if not ticket:
            raise HTTPException(404, "unknown ticket")
        if ticket["status"] != "Human review":
            raise HTTPException(409, "ticket is no longer in Human review")
        if not ctx.jira or not ctx.github:
            raise HTTPException(503, "Jira or GitHub is unavailable")
        pr_run = ctx.db.one("SELECT pr_url FROM runs WHERE ticket_key=%s AND pr_url<>''"
                            " ORDER BY started_at DESC LIMIT 1", (key,))
        match = re.search(r"/pull/(\d+)(?:/|$)", (pr_run or {}).get("pr_url", ""))
        if not match:
            raise HTTPException(409, "ticket has no pull request to review")
        run_id = str(uuid.uuid4())
        claim = ctx.leases.claim(key, run_id, ctx.settings.lease_ms)
        if not claim.ok:
            raise HTTPException(409, "ticket is already being changed")
        try:
            ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,token_hash)"
                        " VALUES (%s,%s,%s,'qa','reviewer-console','human','control',%s,'')",
                        (run_id, key, ticket["project"], claim.fence))
        except Exception:
            ctx.leases.release(key, run_id, claim.fence)
            raise
        return ticket, run_id, claim.fence, int(match.group(1))

    def finish_reviewer_run(key: str, run_id: str, fence: int, outcome: str, summary: str) -> None:
        ctx.db.run("UPDATE runs SET status='finished', outcome=%s, summary=%s, ended_at=now() WHERE id=%s",
                   (outcome, summary[:2000], run_id))
        ctx.leases.release(key, run_id, fence)
        ctx.events.emit("run.finished", ticket=key, project=key.split("-", 1)[0], step="qa", run_id=run_id,
                        fence=fence, account="reviewer-console", provider="human", node="control", outcome=outcome,
                        reason=summary[:200])

    def merge_command(run: dict, number: int, title: str = "", method: str = "squash", node: str = "") -> dict:
        project = ctx.repo().projects.get(run["project"])
        if not project:
            raise HTTPException(404, "project is not configured")
        res = ctx.github.merge_pr(project.repo_name, number, title, method)
        ok = bool(res.get("merged"))
        audit("github", "merge", run["ticket_key"], str(run["id"]), run["fence"], run["account_id"], ok,
              res.get("sha") or res.get("message", ""), {"number": number, "method": method})
        ctx.events.emit("github.merge", ticket=run["ticket_key"], run_id=str(run["id"]), fence=run["fence"],
                        account=run["account_id"], node=node or run["node"],
                        outcome="merged" if ok else "refused", reason=res.get("message", "")[:200])
        return res

    def master_snapshot() -> dict:
        now = utcnow()
        repo = ctx.repo()
        incidents = ctx.db.all("SELECT id, opened_at, severity, kind, subject, title, detail FROM incidents"
                               " WHERE closed_at IS NULL ORDER BY opened_at")
        needs_owner = ctx.db.all("SELECT key, project, summary, description, priority, labels, jira_updated"
                                 " FROM tickets WHERE status='Needs owner' AND NOT ('owner-escalated'=ANY(labels))"
                                 " ORDER BY priority, jira_created NULLS LAST, key")
        running = ctx.db.all("SELECT id, ticket_key, project, step, account_id, provider, model, node, fence, started_at"
                             " FROM runs WHERE status='running' AND step<>'master' ORDER BY started_at")
        clocks = []
        for run in running:
            project = repo.projects.get(run["project"])
            limits = project.work_minutes() if project else {"target": 30, "warn": 45, "hard": 60}
            elapsed = max(0, (now - run["started_at"]).total_seconds() / 60)
            clocks.append({"run_id": str(run["id"]), "ticket": run["ticket_key"], "project": run["project"],
                           "step": run["step"], "account": run["account_id"], "provider": run["provider"],
                           "model": run["model"], "node": run["node"], "fence": run["fence"],
                           "started_at": run["started_at"].isoformat(), "elapsed_minutes": round(elapsed, 1),
                           "work_minutes": limits,
                           "passed": [name for name in ("target", "warn", "hard") if elapsed >= limits[name]]})
        offline_nodes = ctx.db.all("SELECT name, last_heartbeat FROM nodes WHERE status='offline' ORDER BY name")
        quota_incidents = [i for i in incidents if i["kind"] in ("account_quota", "quota_high")]
        high_subjects = {i["subject"].split(":", 1)[0] for i in quota_incidents if i["kind"] == "quota_high"}
        quota_accounts = []
        for account in ctx.db.all("SELECT id, provider, node, status, cooldown_until, last_error, quota FROM accounts"):
            cooling = ctx.leases.cooling(account["id"])
            if account["status"] in ("cooldown", "signed_out", "error") or cooling or account["id"] in high_subjects:
                quota_accounts.append({**account, "cooldown": {"reason": cooling[0], "seconds": cooling[1]}
                                      if cooling else None})
        return {"scope": "global", "captured_at": now.isoformat(), "open_incidents": incidents,
                "needs_owner": needs_owner, "running_clocks": clocks, "offline_nodes": offline_nodes,
                "quota_accounts": quota_accounts, "quota_incidents": quota_incidents}

    def process_flow_bugs(run: dict) -> list[dict]:
        """Create or update detected TESTFLOW Bugs from this valid, fenced master cycle."""
        if not ctx.jira:
            return []
        reports: list[dict] = []
        terminal = {"done", "closed", "resolved", "cancelled", "canceled"}
        for candidate in flow_bug_candidates(ctx):
            tracker = ctx.db.one("SELECT ticket_key, run_ids, repeat_count, in_intake FROM flow_bug_tracker"
                                 " WHERE signature=%s", (candidate["signature"],))
            if tracker:
                ticket_key = tracker["ticket_key"]
                try:
                    status = ctx.jira.status(ticket_key)
                except Exception as exc:  # Jira reads can fail transiently; retry on the next master cycle.
                    log.warning("could not check status of flow-bug %s: %s", ticket_key, type(exc).__name__)
                    continue
                ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, ticket_key))
                if status.casefold() not in terminal:
                    if not tracker["in_intake"]:  # created earlier, but the move to Intake failed: retry only that
                        if status == "Intake":
                            ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now()"
                                       " WHERE signature=%s", (candidate["signature"],))
                        elif move_flow_bug_to_intake(run, ticket_key, candidate["signature"]):
                            reports.append({"action": "moved_to_intake", "ticket": ticket_key,
                                            "signature": candidate["signature"]})
                    known = set(tracker["run_ids"] or [])
                    new_runs = [r for r in candidate["runs"] if r["id"] not in known]
                    if new_runs:
                        repeat_count = int(tracker["repeat_count"]) + len(new_runs)
                        total = len(known) + len(new_runs)
                        body = ("AI Flow · master · repeated flow error\n"
                                f"Pattern: {candidate['pattern']}\n"
                                f"Repeat count: {repeat_count} additional failed runs since this Bug was opened "
                                f"({total} matching runs recorded).\n"
                                f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                                "New run IDs: " + ", ".join(r["id"] for r in new_runs))
                        comment_id = ctx.jira.comment(ticket_key, body)
                        all_ids = sorted(known | {r["id"] for r in new_runs})
                        ctx.db.run("UPDATE flow_bug_tracker SET run_ids=%s, repeat_count=%s, updated_at=now()"
                                   " WHERE signature=%s", (all_ids, repeat_count, candidate["signature"]))
                        audit("jira", "flow_bug_repeat", ticket_key, str(run["id"]), run["fence"],
                              run["account_id"], True, comment_id,
                              {"signature": candidate["signature"], "repeat_count": repeat_count,
                               "new_run_ids": [r["id"] for r in new_runs]})
                        ctx.events.emit("flow_bug.repeated", ticket=ticket_key, project="TESTFLOW",
                                        run_id=str(run["id"]), fence=run["fence"], account=run["account_id"],
                                        node=run["node"], outcome="commented", reason=candidate["pattern"],
                                        data={"repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                        reports.append({"action": "repeated", "ticket": ticket_key,
                                        "repeat_count": repeat_count, "new_run_ids": [r["id"] for r in new_runs]})
                    continue

            if not candidate["creation_eligible"]:
                # The tracker keeps low-volume recurrences visible for open Bugs, but a closed Bug only
                # gets replaced after the normal repetition threshold or an aged ticket_stuck incident.
                continue

            run_lines = [f"- {item['id']} · {item['ticket']} · {item['step']} · node {item['node']} · release {item['release']}"
                         for item in candidate["runs"]]
            reason = ("at least three matching failed runs across at least two TESTFLOW tickets in 30 minutes"
                      if len(candidate["runs"]) >= 3 and len(candidate["tickets"]) >= 2 else
                      "a ticket_stuck incident for this error pattern remained open over 30 minutes")
            description = ("Automatically detected by the AI Flow master cycle.\n\n"
                           f"Fingerprint: {candidate['signature']}\n"
                           f"Pattern: {candidate['pattern']}\n"
                           f"Error kind: {candidate['error_kind']}\n"
                           f"Detection: {reason}.\n"
                           f"Affected tickets: {', '.join(candidate['tickets'])}\n"
                           f"Steps: {', '.join(candidate['steps'])}\n"
                           f"Nodes: {', '.join(candidate['nodes'])}\n"
                           f"AI Flow releases: {', '.join(candidate['releases'])}\n"
                           "Related runs:\n" + "\n".join(run_lines) + "\n\n"
                           f"Suggested fix: {suggested_flow_fix(candidate)}")
            summary = f"[Flow bug] {candidate['pattern'].splitlines()[0]}"[:240]
            try:
                ticket_key = ctx.jira.create_issue("TESTFLOW", summary, description, issuetype="Bug",
                                                   labels=["flow-bug"])
            except Exception as exc:  # Keep the monitoring snapshot available and retry detection next cycle.
                audit("jira", "flow_bug_create", "", str(run["id"]), run["fence"], run["account_id"], False,
                      "Jira issue creation failed", {"signature": candidate["signature"], "error": type(exc).__name__})
                log.warning("could not create flow-bug for signature %s: %s",
                            candidate["signature"], type(exc).__name__)
                continue
            # Record the new Bug before any further Jira call, so a failure below never leads to a second Bug for
            # this signature. The status is refreshed from Jira on the next cycle; it is not Intake until moved.
            run_ids = sorted(item["id"] for item in candidate["runs"])
            ctx.db.run("INSERT INTO flow_bug_tracker (signature, ticket_key, run_ids, repeat_count, in_intake)"
                       " VALUES (%s,%s,%s,0,false) ON CONFLICT (signature) DO UPDATE SET ticket_key=EXCLUDED.ticket_key,"
                       " run_ids=EXCLUDED.run_ids, repeat_count=0, in_intake=false, updated_at=now()",
                       (candidate["signature"], ticket_key, run_ids))
            ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                       " VALUES (%s,'TESTFLOW','Created','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                       " type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                       " description=EXCLUDED.description, synced_at=now()",
                       (ticket_key, ["flow-bug"], summary, description))
            audit("jira", "flow_bug_create", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
                  "created", {"signature": candidate["signature"], "run_ids": run_ids,
                              "tickets": candidate["tickets"], "labels": ["flow-bug"]})
            status = move_flow_bug_to_intake(run, ticket_key, candidate["signature"])
            ctx.events.emit("flow_bug.created", ticket=ticket_key, project="TESTFLOW", run_id=str(run["id"]),
                            fence=run["fence"], account=run["account_id"], node=run["node"],
                            outcome=status or "created", reason=candidate["pattern"],
                            data={"signature": candidate["signature"], "run_ids": run_ids,
                                  "tickets": candidate["tickets"]})
            reports.append({"action": "created", "ticket": ticket_key, "signature": candidate["signature"],
                            "run_ids": run_ids, "in_intake": status == "Intake"})
        return reports

    def move_flow_bug_to_intake(run: dict, ticket_key: str, signature: str) -> str | None:
        """Move a tracked flow-bug to Intake once; returns "Intake", or None when the move failed (retried later)."""
        try:
            status = ctx.jira.transition_to(ticket_key, "Intake")
            if status != "Intake":
                raise RuntimeError("flow-bug did not reach Intake")
        except Exception as exc:  # The tracker row keeps the key; the next master cycle retries only the move.
            audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], False,
                  "Intake transition failed", {"signature": signature, "error": type(exc).__name__})
            log.warning("could not move flow-bug %s to Intake: %s", ticket_key, type(exc).__name__)
            return None
        ctx.db.run("UPDATE flow_bug_tracker SET in_intake=true, updated_at=now() WHERE signature=%s", (signature,))
        ctx.db.run("UPDATE tickets SET status='Intake', synced_at=now() WHERE key=%s", (ticket_key,))
        audit("jira", "flow_bug_intake", ticket_key, str(run["id"]), run["fence"], run["account_id"], True,
              "Intake", {"signature": signature})
        return "Intake"

    # --- health ---------------------------------------------------------------------------------------------
    @app.get("/v1/console/session")
    def console_session(request: Request) -> dict:
        user = console_user(request, {"owner", "lead"})
        return {"authenticated": True, "user": user.subject, "role": user.role}

    @app.get("/healthz")
    def healthz() -> dict:
        ctx.db.one("SELECT 1 AS ok")
        ctx.leases.r.ping()
        return {"ok": True, "version": VERSION}

    # --- nodes ----------------------------------------------------------------------------------------------
    @app.post("/v1/nodes/join")
    def join(body: JoinBody) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", body.name):
            raise HTTPException(400, "invalid node name")
        shared_token_ok = bool(ctx.settings.join_token) and secrets.compare_digest(body.join_token,
                                                                                   ctx.settings.join_token)
        token = secrets.token_urlsafe(32)
        with ctx.db.pool.connection() as connection:
            one_time = None
            if not shared_token_ok and body.join_token:
                one_time = connection.execute(
                    "UPDATE node_join_tokens SET consumed_at=now() WHERE token_hash=%s AND node=%s"
                    " AND consumed_at IS NULL AND expires_at > now() RETURNING node",
                    (h(body.join_token), body.name)).fetchone()
            if not shared_token_ok and not one_time:
                raise HTTPException(403, "invalid join token")
            connection.execute(
                "INSERT INTO nodes (name, ip, token_hash, status) VALUES (%s,%s,%s,'joined')"
                " ON CONFLICT (name) DO UPDATE SET token_hash=EXCLUDED.token_hash,"
                " ip=coalesce(nullif(EXCLUDED.ip,''), nodes.ip), status='joined'",
                (body.name, body.ip, h(token)))
            audit_in(connection, "node", "join", body.name, "", None, "node-agent", True, "node joined",
                     {"ip_present": bool(body.ip)})
        ctx.events.emit("node.joined", node=body.name)
        return {"node": body.name, "node_token": token}

    @app.post("/v1/nodes/{name}/heartbeat")
    def node_heartbeat(name: str, body: HeartbeatBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        prev = ctx.db.one("SELECT status FROM nodes WHERE name=%s", (node,))
        ctx.db.run("UPDATE nodes SET status='online', slots_total=%s, slots_used=%s, release_version=%s, tools=%s,"
                   " metrics=%s, last_heartbeat=now(), idle_since=CASE WHEN %s=0 THEN coalesce(idle_since,now())"
                   " ELSE NULL END WHERE name=%s",
                   (body.slots_total, body.slots_used, body.release_version, J(body.tools), J(body.metrics),
                    body.slots_used, node))
        for account_id, expires_at in body.credential_expiries.items():
            ctx.db.run("UPDATE accounts SET credential_expires_at=%s WHERE id=%s AND node=%s",
                       (expires_at, account_id, node))
        from .sa import update_slot_pressure
        update_slot_pressure(ctx)
        ctx.events.ship("node.heartbeat", node=node, outcome="online", slots_total=body.slots_total,
                        slots_used=body.slots_used, release_version=body.release_version, metrics=body.metrics)
        if prev and prev["status"] != "online":
            ctx.events.emit("node.online", node=node, reason=f"was {prev['status']}")
            from .incidents import close_incident
            close_incident(ctx, "node_down", node, "heartbeats are back")
        merged: dict[str, dict] = {}
        rank = {"working": 5, "probing": 4, "idle": 3, "unavailable": 2, "error": 1, "stopped": 0}
        for w in body.workers:
            acc = str(w.get("account", ""))
            if not acc:
                continue
            st = str(w.get("state", "unknown")) if w.get("alive", True) else "stopped"
            cur = merged.get(acc)
            if cur is None or rank.get(st, 0) > rank.get(cur["_state"], 0):
                merged[acc] = {**w, "_state": st, "instances": (cur or {}).get("instances", 0) + 1}
            else:
                cur["instances"] = cur.get("instances", 1) + 1
        for acc, w in merged.items():
            state = w.pop("_state")
            # "unavailable" (last probe failed) keeps the status the probe set: cooldown, signed_out or error
            ctx.db.run("UPDATE accounts SET worker_state=%s, status=CASE WHEN status='signed_out' THEN status"
                       " WHEN cooldown_until > now() THEN 'cooldown' WHEN %s = 'unavailable' THEN status"
                       " WHEN status = 'working' AND %s = 'idle' THEN status ELSE %s END,"
                       " updated_at=now() WHERE id=%s AND node=%s", (J(w), state, state, state, acc, node))
            ctx.db.run("UPDATE accounts SET idle_since=CASE WHEN status='idle' THEN coalesce(idle_since,now()) ELSE NULL END,"
                       " cooldown_since=CASE WHEN status='cooldown' THEN coalesce(cooldown_since,now()) ELSE NULL END"
                       " WHERE id=%s AND node=%s", (acc, node))
        repo = ctx.repo()
        instances = [a.id if i == 1 else f"{a.id}.{i}" for a in repo.accounts_on(node)
                     for i in range(1, max(1, int(a.parallel or 1)) + 1)]
        # Legacy agents may execute an installer from an archive before verifying that archive. Keep their
        # automatic release target empty until a trusted signed-release verifier has been bootstrapped manually.
        release = current_release(node) if body.release_protocol >= 1 else ""
        configured = repo.nodes.get(node)
        project_stacks = {key: repo.projects[key].stack for key in (configured.projects if configured else [])
                          if key in repo.projects}
        return {"desired": {"accounts": [a.id for a in repo.accounts_on(node)], "workers": instances,
                            "release": release,
                            "projects": configured.projects if configured else [],
                            "project_stacks": project_stacks,
                            "tools": repo.tools_for_node(node), "versions": repo.versions}}

    # --- owner-operated node installation and repair ---------------------------------------------------------
    def operation_plan(name: str, body: NodeOperationBody, node_row: dict) -> dict:
        repo = ctx.repo()
        configured = repo.nodes.get(name)
        packages = (configured.packages or []) if configured else []
        services = (configured.services or []) if configured else []
        if not isinstance(packages, list) or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:_-]{0,127}", p)
                for p in packages):
            raise HTTPException(400, "node packages must be a list of safe package names")
        if not isinstance(services, list) or any(not isinstance(s, dict) for s in services):
            raise HTTPException(400, "node services must be a list of service definitions")
        for service in services:
            unit = service.get("unit")
            if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,180}\.service", unit):
                raise HTTPException(400, "node service unit is invalid")
            healthcheck = service.get("healthcheck", "")
            if not isinstance(healthcheck, str):
                raise HTTPException(400, "service health check must be an HTTP URL")
            if healthcheck:
                parsed = urlsplit(healthcheck)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                        or parsed.password or parsed.fragment):
                    raise HTTPException(400, "service health check must be an HTTP URL without embedded credentials")
        required = repo.tools_for_node(name)
        stacks = [stack for p in (configured.projects if configured else [])
                  if p in repo.projects for stack in repo.projects[p].stacks]
        missing = set(toolchain_gaps(node_row.get("tools") or {}, required, repo.versions, stacks,
                                     require_pins=bool(stacks)))
        selected: list[str]
        if body.action == "repair":
            if body.component:
                raise HTTPException(400, "repair reconciles the full desired state; omit component")
            selected = list(required)
            if packages:
                selected.append("packages")
        else:
            component = body.component.strip()
            if component == "packages":
                if not packages:
                    raise HTTPException(400, "no OS packages are declared for this node")
                selected = [component]
            elif component:
                if component not in required:
                    raise HTTPException(400, "component is not required by this node's control repo state")
                if component not in missing:
                    raise HTTPException(409, "component is already present on this node")
                selected = [component]
            else:
                selected = sorted(missing)
            if not selected:
                raise HTTPException(409, "node has no missing required components")

        # AI CLIs are installed with npm. Include Node.js in the server-authored plan so a clean node
        # receives the prerequisite before any CLI installation; the agent skips it when already healthy.
        if any(tool in {"claude", "codex", "grok", "opencode"} for tool in selected) and "node" not in selected:
            selected.append("node")

        tools = []
        for tool in selected:
            tools.append({"name": tool, "version": pinned_version(repo.versions, tool, stacks),
                          "packages": list(packages) if tool == "packages" else []})
        desired_services = list(services) if body.action == "repair" else []
        return {"action": body.action, "tools": tools, "services": desired_services}

    @app.post("/v1/nodes/{name}/join-command")
    def create_join_command(name: str, body: JoinCommandBody, request: Request) -> dict:
        user = console_user(request, {"owner"}, target="node", action="join_command", node=name)
        if name != body.name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            audit("node", "join_command", name, "", None, user.subject, False, "invalid node name")
            raise HTTPException(400, "invalid node name")
        try:
            control = validated_join_control_url(ctx.settings.join_control_url)
        except ValueError as exc:
            audit("node", "join_command", name, "", None, user.subject, False, str(exc))
            raise HTTPException(503, "configure AIFLOW4_JOIN_CONTROL_URL to an address reachable from joining nodes") from exc
        version = current_release()
        try:
            _, _, _, checksum = release_assets(version)
        except HTTPException as exc:
            audit("node", "join_command", name, "", None, user.subject, False, "no signed release is published")
            raise HTTPException(409, "no signed release is published; run aiflow4-deploy first") from exc
        token = secrets.token_urlsafe(32)
        expires_at = utcnow() + timedelta(minutes=10)
        audit_payload = {"expires_at": expires_at.isoformat(), "ssh_password_saved": False}
        with ctx.db.pool.connection() as connection:
            connection.execute("INSERT INTO node_join_tokens (token_hash, node, expires_at) VALUES (%s,%s,%s)",
                               (h(token), name, expires_at))
            connection.execute("INSERT INTO nodes (name, status) VALUES (%s,'joined')"
                               " ON CONFLICT (name) DO NOTHING", (name,))
            audit_in(connection, "node", "join_command", name, "", None, user.subject, True,
                     "one-time join command created", audit_payload)
        command = join_bootstrap_command(token, name, control, version, checksum)
        ctx.events.emit("node.join_command.created", node=name, outcome="created",
                        reason="one-time token expires in 10 minutes", data={"actor": user.subject})
        return {"node": name, "command": command, "expires_at": expires_at.isoformat(), "expires_in_s": 600}

    @app.post("/v1/nodes/{name}/operations", status_code=202)
    def create_node_operation(name: str, body: NodeOperationBody, request: Request) -> dict:
        user = console_user(request, {"owner"}, target="node", action=body.action, node=name)
        node_row = ctx.db.one("SELECT name, status, token_hash, tools, last_heartbeat"
                              " FROM nodes WHERE name=%s", (name,))
        if not node_row:
            audit("node", body.action, name, "", None, user.subject, False, "node is not registered")
            raise HTTPException(404, "node is not registered")
        heartbeat = node_row["last_heartbeat"]
        if (node_row["status"] != "online" or not node_row["token_hash"] or not heartbeat
                or (utcnow() - heartbeat).total_seconds() > 75):
            audit("node", body.action, name, "", None, user.subject, False, "node agent is not online")
            raise HTTPException(409, "node agent must be online to install or repair")
        try:
            payload = operation_plan(name, body, node_row)
        except HTTPException as exc:
            audit("node", body.action, name, "", None, user.subject, False, str(exc.detail))
            raise
        operation_id = str(uuid.uuid4())
        components = [str(t["name"]) for t in payload["tools"]]
        with ctx.db.pool.connection() as connection:
            connection.execute("INSERT INTO node_operations (id,node,action,actor,payload) VALUES (%s,%s,%s,%s,%s)",
                               (operation_id, name, body.action, user.subject, J(payload)))
            audit_in(connection, "node", body.action, name, operation_id, None, user.subject, True, "queued",
                     {"operation_id": operation_id, "components": components,
                      "service_count": len(payload["services"])})
        ctx.events.emit("node.operation.queued", node=name, outcome="queued",
                        reason=f"{body.action}: {', '.join(components) or 'services'}",
                        data={"operation_id": operation_id, "action": body.action, "actor": user.subject})
        return {"id": operation_id, "node": name, "action": body.action, "status": "queued",
                "components": components}

    @app.get("/v1/nodes/{name}/operations")
    def node_operations(name: str, request: Request) -> dict:
        console_user(request, {"owner", "lead"})
        rows = ctx.db.all("SELECT id, node, action, actor, status, result, created_at, started_at, completed_at"
                          " FROM node_operations WHERE node=%s ORDER BY created_at DESC LIMIT 50", (name,))
        for row in rows:
            row["id"] = str(row["id"])
        return {"node": name, "operations": rows}

    @app.get("/v1/nodes/{name}/operations/next")
    def next_node_operation(name: str, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        row = ctx.db.one(
            "WITH next AS (SELECT id FROM node_operations WHERE node=%s AND (status='queued' OR"
            " (status='running' AND started_at < now() - interval '30 minutes'))"
            " ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)"
            " UPDATE node_operations AS op SET status='running', started_at=now() FROM next"
            " WHERE op.id=next.id RETURNING op.id, op.action, op.payload", (node,))
        if not row:
            return {"operation": None}
        return {"operation": {"id": str(row["id"]), "action": row["action"], "payload": row["payload"]}}

    @app.post("/v1/nodes/{name}/operations/{operation_id}/result")
    def finish_node_operation(name: str, operation_id: str, body: NodeOperationResultBody,
                              node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        try:
            parsed_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise HTTPException(404, "unknown node operation") from exc
        status = body.status
        updated = ctx.db.one(
            "UPDATE node_operations SET status=%s, result=%s, completed_at=now()"
            " WHERE id=%s AND node=%s AND status='running' RETURNING id, action",
            (status, J(body.result), parsed_id, node))
        if not updated:
            raise HTTPException(404, "unknown or completed node operation")
        ctx.events.emit(f"node.operation.{status}", node=node, outcome=status,
                        reason=f"{updated['action']} finished", data={"operation_id": parsed_id,
                                                                     "healthy": body.result.get("healthy")})
        return {"id": parsed_id, "status": status}

    # --- releases: nodes follow a tagged, signed release written by aiflow4-deploy ---------------------------
    def current_release(node: str = "") -> str:
        # During a canary, only the selected node sees the candidate. The file is written by the host updater
        # and shared read-only with control containers; CURRENT remains the fleet-wide stable release.
        try:
            import json
            canary = json.loads((RELEASES / "CANARY.json").read_text())
            if canary.get("node") == node and canary.get("target"):
                return str(canary["target"])
        except (OSError, ValueError, AttributeError):
            pass
        try:
            return (RELEASES / "CURRENT").read_text().strip()
        except OSError:
            return ""

    def tagged_release(version: str) -> Path:
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
            raise HTTPException(400, "bad release version")
        return RELEASES / version

    def release_assets(version: str) -> tuple[Path, Path, Path, str]:
        directory = tagged_release(version)
        tar_name = f"aiflow4-{version}.tar.gz"
        tarball = directory / tar_name
        bundle = directory / f"{tar_name}.sigstore.json"
        sums = directory / "SHA256SUMS"
        if not all(path.is_file() for path in (tarball, bundle, sums)):
            raise HTTPException(404, "no such signed release")
        checksum = re.compile(rf"^([0-9a-f]{{64}})  {re.escape(tar_name)}$")
        matches = [m.group(1) for line in sums.read_text().splitlines() if (m := checksum.fullmatch(line))]
        if len(matches) != 1:
            raise HTTPException(404, "release checksum manifest is incomplete")
        return tarball, bundle, sums, matches[0]

    @app.get("/v1/releases/{version}/manifest")
    def release_manifest(version: str, _node: str = Depends(node_auth)) -> dict:
        _, _, _, checksum = release_assets(version)
        base = f"/v1/releases/{version}"
        tar_name = f"aiflow4-{version}.tar.gz"
        return {"version": version, "sha256": checksum, "tarball": f"{base}.tar.gz",
                "signature": f"{base}.tar.gz.sigstore.json", "checksums": f"{base}/SHA256SUMS",
                "filename": tar_name}

    @app.get("/v1/releases/{version}/SHA256SUMS")
    def release_checksums(version: str, _node: str = Depends(node_auth)) -> FileResponse:
        _, _, sums, _ = release_assets(version)
        return FileResponse(sums, media_type="text/plain", filename="SHA256SUMS")

    @app.get("/v1/releases/{version}.tar.gz.sigstore.json")
    def release_signature(version: str, _node: str = Depends(release_reader)) -> FileResponse:
        _, bundle, _, _ = release_assets(version)
        return FileResponse(bundle, media_type="application/json", filename=f"aiflow4-{version}.tar.gz.sigstore.json")

    @app.get("/v1/releases/{sha}.tar.gz")
    def release_file(sha: str, node: str = Depends(release_reader)) -> FileResponse:
        if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", sha):
            f, _, _, _ = release_assets(sha)
        elif re.fullmatch(r"[0-9a-f]{7,40}", sha):  # old, already-installed releases remain downloadable
            f = RELEASES / f"aiflow4-{sha}.tar.gz"
        else:
            raise HTTPException(400, "bad release id")
        if not f.is_file():
            raise HTTPException(404, "no such release")
        return FileResponse(f, media_type="application/gzip")

    @app.post("/v1/nodes/{name}/release-failures")
    def release_failure(name: str, body: ReleaseFailureBody, node: str = Depends(node_auth)) -> dict:
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", body.release_version):
            raise HTTPException(400, "bad release version")
        detail = body.detail.strip()[:1000] or "release signature or checksum verification failed"
        title = f"node {node} rejected release {body.release_version}"
        from .incidents import open_incident
        opened = open_incident(ctx, "release_verification", f"{node}:{body.release_version}", "critical", title, detail)
        ctx.events.emit("node.release_rejected", node=node, release_version=body.release_version, detail=detail)
        return {"recorded": True, "opened": opened}

    @app.post("/v1/nodes/{name}/worktrees/sweep")
    def worktrees_sweep(name: str, body: WorktreeSweepBody, node: str = Depends(node_auth)) -> dict:
        """Which of a node's worktree directories can go: every directory of a ticket that is Done, and the
        per-run checkouts (intake, impact, triage) of a ticket that has no run in progress."""
        if name != node:
            raise HTTPException(403, "token belongs to another node")
        remove: list[str] = []
        for entry in body.dirs[:2000]:
            project, _, directory = entry.partition("/")
            m = re.match(r"([A-Z][A-Z0-9]*-\d+)(.*)$", directory)
            if not m or not project or "/" in directory or ".." in entry:
                continue
            key, rest = m.group(1), m.group(2)
            ticket = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))
            running = ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND status='running' LIMIT 1", (key,))
            per_run = bool(re.fullmatch(r"--(intake|impact|triage-.+)", rest))
            if running:
                continue
            if per_run or (ticket and ticket["status"] == "Done"):
                remove.append(entry)
        return {"remove": remove}

    # --- work -----------------------------------------------------------------------------------------------
    def last_dev(key: str, done: bool = False) -> dict | None:
        """The latest finished dev run; done=True: the latest one that delivered work (a failed or blocked run
        in between does not answer the reviewer's findings)."""
        return ctx.db.one("SELECT * FROM runs WHERE ticket_key=%s AND step='dev' AND status<>'running'"
                          + (" AND outcome='done'" if done else "") + " ORDER BY started_at DESC LIMIT 1", (key,))

    def eligible(account_id: str, node: str) -> list[dict]:
        """Tickets this account may take now: verify and triage before new development."""
        repo = ctx.repo()
        acc = repo.account(account_id)
        if not acc or acc.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if ctx.leases.cooling(account_id):
            return []
        node_row = ctx.db.one("SELECT tools FROM nodes WHERE name=%s", (node,)) or {"tools": {}}
        projects = [p for p in (repo.nodes[node].projects if node in repo.nodes else []) if p in repo.projects]
        # A project with a declared stack is offered only after this node reports every pinned tool healthy.
        # Filtering here keeps another project on the same node available while a stack install is pending.
        projects = [p for p in projects if not repo.projects[p].stacks or not toolchain_gaps(
            node_row.get("tools") or {}, repo.projects[p].tools, repo.versions, repo.projects[p].stacks,
            require_pins=True)]
        roles = set(acc.roles or ["dev", "verify"])
        held = set(ctx.leases.held_keys())
        out: list[dict] = []
        if not projects:
            return []
        prio = {k: int(getattr(p, "priority", 5) or 5) for k, p in repo.projects.items()}
        if "verify" in roles:
            verify_cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "verify"})}
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'In Verify'"
                                " AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held or t["key"] in verify_cooling:
                    continue
                dev = last_dev(t["key"], done=True)
                if not dev or not dev["pr_url"]:
                    continue
                if dev["account_id"] == acc.id:  # never grade your own work
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                out.append({**t, "step": "verify"})
        if "master" in roles:
            triage_cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "triage"})}
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Needs owner'"
                                " AND NOT ('owner-escalated' = ANY(labels)) AND NOT ('aiflow-hold' = ANY(labels))",
                                (projects,)):
                if t["key"] in held or t["key"] in triage_cooling:
                    continue
                dev = last_dev(t["key"])
                if dev and dev["account_id"] == acc.id:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                out.append({**t, "step": "triage"})
        if "intake_refiner" in roles:
            def backoff(step: str) -> set[str]:
                return {r["ticket_key"] for r in ctx.db.all(INTAKE_BACKOFF_SQL, {"step": step})
                        if r["outcome"] in ("failed", "timeout", "lost") and r["ended_at"]
                        and r["ended_at"] > utcnow() - timedelta(minutes=10)}
            waiting = backoff("intake")
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Intake'"
                                " AND NOT ('aiflow-hold' = ANY(labels))"
                                " ORDER BY jira_created NULLS LAST, key", (projects,)):
                if t["key"] in held or t["key"] in waiting:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                out.append({**t, "step": "intake"})
            # change requests from the Business view: assess the impact once, while they wait in Backlog
            waiting = backoff("impact")
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'Backlog'"
                                " AND 'change-request' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))"
                                " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = tickets.key"
                                " AND r.step = 'impact' AND r.outcome = 'assessed')"
                                " ORDER BY jira_created NULLS LAST, key", (projects,)):
                if t["key"] in held or t["key"] in waiting:
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in t["labels"]:
                    continue
                out.append({**t, "step": "impact"})
        out.sort(key=lambda t: (prio.get(t["project"], 5), t["jira_created"] or utcnow()))
        if "dev" in roles:
            done = {r["key"] for r in ctx.db.all("SELECT key FROM tickets WHERE status = 'Done'")}
            cooling = {r["ticket_key"] for r in ctx.db.all(BACKOFF_SQL, {"step": "dev"})}
            dev_rows = []
            for t in ctx.db.all("SELECT * FROM tickets WHERE project = ANY(%s) AND status = 'To Do'"
                                " AND 'aiflow-ready' = ANY(labels) AND NOT ('aiflow-hold' = ANY(labels))", (projects,)):
                if t["key"] in held or t["key"] in cooling or any(b not in done for b in t["blocked_by"]):
                    continue
                labels = set(t["labels"])
                if "tier-senior" in labels and acc.tier != "senior":
                    continue
                if acc.explicit_only and f"ai-{acc.provider}" not in labels:
                    continue
                dev_rows.append({**t, "step": "dev"})
            dev_rows.sort(key=lambda t: (prio.get(t["project"], 5), "rework" not in t["labels"],
                                         PRIORITY.get(t["priority"], 2), t["jira_created"] or utcnow()))
            out += dev_rows
        # Keep the normal workflow order stable, but put a generated flow bug first whenever it is eligible.
        out.sort(key=lambda t: "flow-bug" not in (t.get("labels") or []))
        return out

    def master_ready(account_id: str, node: str) -> bool:
        if ctx.leases.cooling(account_id):
            return False
        ready = ctx.db.one("SELECT a.status, n.status AS node_status FROM accounts a JOIN nodes n ON n.name=a.node"
                           " WHERE a.id=%s AND a.node=%s", (account_id, node))
        return bool(ready) and ready["status"] in ("idle", "working", "probing") and ready["node_status"] == "online"

    def master_candidate(account_id: str, node: str) -> dict | None:
        repo = ctx.repo()
        account = repo.account(account_id)
        if not account or account.node != node:
            raise HTTPException(403, f"account {account_id} does not run on {node}")
        if "master" not in (account.roles or []) or not master_ready(account_id, node):
            return None
        held = set(ctx.leases.held_keys())
        if MASTER_SCOPE in held:
            return None
        recent = ctx.db.one("SELECT id, fence, ticket_key, provider, status, outcome, ended_at FROM runs"
                            " WHERE step='master' ORDER BY started_at DESC LIMIT 1")
        failover = False
        if recent:
            lease_lost = (recent["status"] == "running" and
                          ctx.leases.master_holder() != f"{recent['id']}:{recent['fence']}")
            failed_over = recent["outcome"] in ("quota", "lost") or lease_lost
            # Prefer the other provider for one cycle after a failure, and only while it has a ready master
            # account. Otherwise any ready master account may take over: cooldown and node checks above
            # already exclude the account that failed, so the flow never waits on an unavailable provider.
            failed_at = recent["ended_at"] or utcnow()
            if (failed_over and recent["provider"] == account.provider
                    and failed_at > utcnow() - timedelta(seconds=MASTER_CYCLE_S)
                    and any(a.provider != account.provider and "master" in (a.roles or [])
                            and master_ready(a.id, a.node) for a in repo.accounts)):
                return None
            # A lost or quota-stopped holder is an account failure, not a failing cycle: no scope backoff.
            failover = failed_over
        backoff = ctx.db.all(BACKOFF_SQL, {"step": "master"})
        if not failover and any(row["ticket_key"] == MASTER_SCOPE for row in backoff):
            return None
        if recent and recent["ended_at"] and recent["ended_at"] > utcnow() - timedelta(seconds=MASTER_CYCLE_S):
            if recent["outcome"] == "done":
                return None
        return {"key": MASTER_SCOPE, "project": "", "status": "Master", "type": "Master", "priority": "Highest",
                "labels": [], "summary": "Master monitoring cycle",
                "description": "Review incidents and follow up on blocked or slow work.",
                "jira_created": None, "step": "master"}

    @app.get("/v1/work/candidates")
    def candidates(account: str, node: str = Depends(node_auth)) -> dict:
        return {"tickets": [{"key": t["key"], "summary": t["summary"], "priority": t["priority"], "labels": t["labels"],
                             "step": t["step"]} for t in eligible(account, node)[:20]]}

    @app.get("/v1/master/candidate")
    def master_cycle_candidate(account: str, node: str = Depends(node_auth)) -> dict:
        return {"candidate": master_candidate(account, node)}

    def related_hits(t: dict) -> list[dict]:
        """Up to 3 past entries related to the ticket summary; memory is advice, so an error gives none."""
        try:
            return memory.related(ctx.db, t["project"], t["summary"], exclude_ticket=t["key"], limit=3)
        except Exception as e:  # noqa: BLE001
            log.warning("memory lookup for %s failed: %s", t["key"], e)
            return []

    @app.post("/v1/work/claim")
    def claim(body: ClaimBody, node: str = Depends(node_auth)) -> Any:
        candidates = [master_candidate(body.account, node)] if body.step == "master" else eligible(body.account, node)
        tickets = {(t["key"], t["step"]): t for t in candidates if t}
        t = tickets.get((body.ticket, body.step))
        if not t:
            return JSONResponse({"claimed": False, "reason": "not eligible"}, status_code=409)
        repo = ctx.repo()
        acc = repo.account(body.account)
        triage_context: dict[str, Any] = {}
        if body.step == "triage":
            verify = ctx.db.one("SELECT summary, details FROM runs WHERE ticket_key=%s AND step='verify'"
                                " AND status<>'running' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            comments = ctx.jira.recent_comments(t["key"], limit=10) if ctx.jira else []
            triage_context = {"last_dev_summary": (last_dev(t["key"]) or {}).get("summary", ""),
                              "verify_findings": ((verify["details"] or {}).get("findings") or verify["summary"])
                              if verify else "", "recent_comments": comments}
        run_id = str(uuid.uuid4())
        c = ctx.leases.claim(t["key"], run_id, ctx.settings.lease_ms)
        if not c.ok:
            return JSONResponse({"claimed": False, "reason": f"held by {c.holder}"}, status_code=409)
        model, effort = repo.model_for(acc.provider, "senior" if "tier-senior" in t["labels"] else acc.tier)
        run_token = secrets.token_urlsafe(32)
        ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, provider, model, node, fence, token_hash)"
                   " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                   (run_id, t["key"], t["project"], body.step, acc.id, acc.provider, model, node, c.fence, h(run_token)))
        ctx.db.run("UPDATE accounts SET status='working', updated_at=now(), idle_since=NULL, cooldown_since=NULL WHERE id=%s",
                   (acc.id,))
        ctx.events.emit("run.claimed", ticket=t["key"], project=t["project"], step=body.step, run_id=run_id,
                        fence=c.fence, account=acc.id, provider=acc.provider, model=model, node=node)
        p = repo.projects.get(t["project"])
        extra: dict[str, Any] = {}
        dev = None if body.step == "master" else (last_dev(t["key"], done=True) or last_dev(t["key"]))
        if body.step == "verify" and dev:
            extra["pr"] = {"url": dev["pr_url"], "number": int(dev["pr_url"].rstrip("/").split("/")[-1]),
                           "branch": dev["branch"], "head_sha": dev["head_sha"], "dev_account": dev["account_id"],
                           "dev_summary": dev["summary"]}
        if body.step == "dev":
            v = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s"
                           " AND step IN ('verify','qa')"
                           " AND outcome='rework' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if v and (not dev or v["started_at"] > dev["started_at"]):
                extra["findings"] = (v["details"] or {}).get("findings") or v["summary"]
            if dev and dev["branch"]:
                extra["branch"] = dev["branch"]
            # the master AI's answer to the question a dev run stopped on, until a dev run finishes or blocks again
            a = ctx.db.one("SELECT summary, details, started_at FROM runs WHERE ticket_key=%s AND step='triage'"
                           " AND outcome='answered' ORDER BY started_at DESC LIMIT 1", (t["key"],))
            if a and not ctx.db.one("SELECT 1 AS x FROM runs WHERE ticket_key=%s AND step='dev' AND started_at > %s"
                                    " AND outcome IN ('done', 'blocked') LIMIT 1", (t["key"], a["started_at"])):
                q = ctx.db.one("SELECT summary FROM runs WHERE ticket_key=%s AND step='dev' AND outcome='blocked'"
                               " AND started_at < %s ORDER BY started_at DESC LIMIT 1", (t["key"], a["started_at"]))
                extra["triage_answer"] = (a["details"] or {}).get("answer") or a["summary"]
                extra["triage_question"] = (q or {}).get("summary") or ""
        if body.step == "triage":
            extra.update(triage_context)
        if body.step == "intake":
            # owners answer intake questions in comments as often as in the description
            try:
                extra["recent_comments"] = ctx.jira.recent_comments(t["key"], limit=10) if ctx.jira else []
            except Exception as e:  # noqa: BLE001
                log.warning("comments of %s for intake failed: %s", t["key"], e)
        if body.step == "impact":
            # the board the assessment compares against: the project's epics and its open work
            extra["project_tickets"] = ctx.db.all(
                "SELECT key, type, status, summary, parent_key FROM tickets WHERE project=%s AND status<>'Done'"
                " AND key<>%s ORDER BY type<>'Epic', key LIMIT 300", (t["project"], t["key"]))
        if body.step == "master":
            extra["master_scope"] = "global"
        else:
            extra["related"] = related_hits(t)
        project_data = ({"key": "", "repo": "", "base": "", "hotfix_base": "", "checks": [], "stack": "",
                         "node": node,
                         "work_minutes": {"target": 30, "warn": 45, "hard": 60},
                         "merge": {"method": "squash", "auto": False, "require_human": True}}
                        if body.step == "master" else
                        {"key": p.key, "repo": p.repo, "base": p.base, "hotfix_base": p.hotfix_base,
                         "checks": p.checks, "stack": p.stack, "work_minutes": p.work_minutes(),
                         "merge": {"method": (p.merge or {}).get("method", "squash"),
                                   "auto": bool((p.merge or {}).get("auto", True)),
                                   "require_human": bool((p.merge or {}).get("require_human", False))}}
                        if p else {})
        return {**extra, "claimed": True, "step": body.step, "run_id": run_id, "fence": c.fence, "run_token": run_token,
                "lease_ms": ctx.settings.lease_ms, "model": model, "effort": effort, "provider": acc.provider,
                "account": acc.id, "identity": bot_identity(),
                "ticket": {**{k: t[k] for k in ("key", "project", "summary", "description", "labels", "priority", "type")},
                           "parent_key": t.get("parent_key")},
                "project": project_data}

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
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s, updated_at=now(),"
                       " cooldown_since=CASE WHEN %s='quota' THEN coalesce(cooldown_since,now()) ELSE NULL END,"
                       " idle_since=NULL WHERE id=%s",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300],
                        body.error_kind, run["account_id"]))
        else:
            ctx.db.run("UPDATE accounts SET status='idle', last_error=CASE WHEN %s IN ('failed','timeout') THEN %s ELSE '' END,"
                       " updated_at=now(), idle_since=coalesce(idle_since,now()), cooldown_since=NULL"
                       " WHERE id=%s AND status='working'", (body.outcome, body.summary[:300], run["account_id"]))
        dur = int((utcnow() - run["started_at"]).total_seconds() * 1000)
        if ctx.jira and run["step"] == "dev" and body.outcome not in ("lost",):
            try:  # developer time on the ticket (each dev run, rework included); the detailed timeline is in OpenObserve
                ctx.jira.add_worklog(run["ticket_key"], max(60, dur // 1000), run["started_at"],
                                     f"AI Flow {run['step']} by {run['account_id']} ({run['provider']}"
                                     f"{'/' + run['model'] if run['model'] else ''}) on {node}: {body.outcome}."
                                     f" Run {run_id}, fence {body.fence}.")
            except Exception as e:  # noqa: BLE001
                log.warning("worklog for %s failed: %s", run["ticket_key"], e)
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

    @app.get("/v1/master/context")
    def master_context(run_id: str, fence: int, node: str = Depends(node_auth)) -> dict:
        master_run(run_id, fence, node, "context")
        return master_snapshot()

    @app.post("/v1/master/flow-bugs")
    def master_flow_bugs(body: MasterFlowBugsBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, "flow-bug-scan")
        return {"reports": process_flow_bugs(run)}

    @app.post("/v1/master/actions")
    def master_action(body: MasterActionBody, node: str = Depends(node_auth)) -> dict:
        run = master_run(body.run_id, body.fence, node, body.action,
                         {"ticket": body.ticket, "intent_chars": len(body.intent), "action": body.action})
        ticket = body.ticket.strip()
        ticket_row = ctx.db.one("SELECT project FROM tickets WHERE key=%s", (ticket,)) if ticket else None
        policy_project = ticket_row["project"] if ticket_row else None
        policy = ctx.repo().policy_for(policy_project)
        if not body.intent.strip():
            reason = "master action must include the request or observation intent"
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], run_id=body.run_id,
                            fence=body.fence, account=run["account_id"], node=node, outcome="denied", reason=reason,
                            action=body.action)
            raise HTTPException(400, reason)
        # Only the request is matched: what/why/result/detail describe observations (an offline node, a run
        # past its hard limit) and must not turn an allowed follow-up into an owner escalation.
        decision = decide_master_action(policy, body.action, body.intent, body.category)
        ctx.events.emit("policy.decision", ticket=ticket or run["ticket_key"], step="master", run_id=body.run_id,
                        fence=body.fence, account=run["account_id"], node=node, outcome=decision.decision,
                        reason=decision.reason[:200], action=body.action)

        def master_event(outcome: str, reason: str) -> None:
            ctx.events.emit("master.action", ticket=ticket or run["ticket_key"], project=policy_project or "",
                            step="master", run_id=body.run_id, fence=body.fence, account=run["account_id"],
                            provider=run["provider"], model=run["model"], node=node, outcome=outcome,
                            reason=reason[:300], action=body.action)

        def formatted_comment(headline: str, result: str, next_step: str) -> str:
            from ..worker.notes import note
            comment_context = type("MasterCommentContext", (), {
                "step": "master", "account": run["account_id"], "provider": run["provider"], "model": run["model"],
                "run_id": body.run_id, "fence": body.fence,
            })()
            return note(comment_context, headline, what=body.what or body.intent or "Review the monitoring snapshot",
                        why=body.why or "The master follow-up requires an action", result=result,
                        next_=next_step or body.next or "Continue monitoring in the next master cycle", node=node)

        if decision.decision == "escalate":
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False,
                  decision.reason, {"intent_chars": len(body.intent)})
            link = (ctx.settings.jira_site.rstrip("/") + f"/browse/{ticket}") if ticket and ctx.settings.jira_site else ""
            comment_id = ""
            if ticket and ctx.jira and ctx.db.one("SELECT key FROM tickets WHERE key=%s", (ticket,)):
                comment_id = ctx.jira.comment(ticket, formatted_comment(
                    "owner decision required", "Escalated to the owner; no protected action was performed.",
                    "The owner decides whether and how this change may proceed."))
                audit("jira", "comment", ticket, body.run_id, body.fence, run["account_id"], True, comment_id,
                      {"master_action": body.action, "escalate_to_owner": decision.reason})
            sent = notify.send("warning", f"{ticket or 'AI Flow'} requires owner decision",
                               f"{decision.reason}. {body.intent[:2500]}", link)
            audit("notify", "master-escalation", ticket, body.run_id, body.fence, run["account_id"], True,
                  ",".join(sent) or "no channel configured", {"reason": decision.reason})
            master_event("escalated", decision.reason)
            return {"ok": True, "escalated": True, "reason": decision.reason, "commented": bool(comment_id),
                    "sent": sent}

        if decision.decision != "allow":
            audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False,
                  decision.reason, {"intent_chars": len(body.intent)})
            master_event("denied", decision.reason)
            raise HTTPException(403, decision.reason)

        allowed_ticket_actions = {"jira_comment", "jira_update", "dev_reply", "requeue"}
        ticket_row = None
        if body.action in allowed_ticket_actions:
            ticket_row = ctx.db.one("SELECT key, project FROM tickets WHERE key=%s", (ticket,)) if ticket else None
            if not ticket_row:
                reason = "a valid Jira ticket is required for this master action"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            if not ctx.jira:
                raise HTTPException(503, "Jira is unavailable")

        result = "ok"
        if body.action in ("jira_comment", "dev_reply"):
            result = ctx.jira.comment(ticket, formatted_comment(
                body.headline, body.result or body.what or "Follow-up recorded", body.next))
        elif body.action == "jira_update":
            if body.fields:
                ctx.jira.set_fields(ticket, body.fields)
            for label in body.add_labels:
                ctx.jira.add_label(ticket, label)
                ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s=ANY(labels) THEN labels ELSE array_append(labels,%s) END"
                           " WHERE key=%s", (label, label, ticket))
            for label in body.remove_labels:
                ctx.jira.remove_label(ticket, label)
                ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (label, ticket))
            result = "updated"
        elif body.action == "requeue":
            target = body.target_status or "To Do"
            if target != "To Do":
                reason = "master may only requeue tickets to To Do"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(403, reason)
            ctx.jira.transition_to(ticket, "To Do")
            ctx.db.run("UPDATE tickets SET status='To Do', synced_at=now() WHERE key=%s", (ticket,))
            ctx.jira.comment(ticket, formatted_comment(body.headline or "ticket requeued",
                                                       body.result or "The ticket is back in To Do.", body.next))
            result = "To Do"
        elif body.action == "incident_open":
            from .incidents import open_incident
            if not body.kind or not body.subject or body.severity not in ("info", "warning", "critical"):
                reason = "incident_open requires kind, subject, and a valid severity"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            result = "opened" if open_incident(ctx, body.kind, body.subject, body.severity,
                                                body.title or body.headline, body.detail or body.result) else "already open"
        elif body.action == "incident_close":
            from .incidents import close_incident
            if not body.kind or not body.subject:
                reason = "incident_close requires kind and subject"
                audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], False, reason)
                master_event("denied", reason)
                raise HTTPException(400, reason)
            close_incident(ctx, body.kind, body.subject, body.result or body.why)
            result = "closed"
        elif body.action == "notify_owner":
            sent = notify.send(body.severity if body.severity in ("info", "warning", "critical") else "warning",
                               body.title or body.headline, body.detail or body.result,
                               ctx.settings.jira_site.rstrip("/") + f"/browse/{ticket}" if ticket and ctx.settings.jira_site else "")
            result = ",".join(sent) or "no channel configured"
        audit("master", body.action, ticket, body.run_id, body.fence, run["account_id"], True, str(result)[:500],
              {"intent_chars": len(body.intent), "fields": list(body.fields), "labels_added": body.add_labels,
               "labels_removed": body.remove_labels})
        master_event("completed", str(result))
        return {"ok": True, "result": result, "escalated": False}

    # --- Jira writes (fenced) ---------------------------------------------------------------------------------
    @app.post("/v1/jira/{key}/transition")
    def jira_transition(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "transition", {"to": body.to})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] == "impact":
            audit("jira", "transition", key, body.run_id, body.fence, run["account_id"], False,
                  "an impact run only comments", {"to": body.to})
            raise HTTPException(403, "an impact run only comments; the owner moves the ticket")
        previous = ctx.db.one("SELECT project, type, status, labels, summary, fields FROM tickets WHERE key=%s", (key,))
        status = ctx.jira.transition_to(key, body.to)
        ctx.db.run("UPDATE tickets SET status=%s, done_at=CASE WHEN status <> 'Done' AND %s='Done'"
                   " THEN now() ELSE done_at END, synced_at=now() WHERE key=%s", (status, status, key))
        audit("jira", "transition", key, body.run_id, body.fence, run["account_id"], True, status, {"to": body.to})
        ctx.events.emit("jira.transition", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                        node=node, outcome=status)
        if previous and previous["type"] == "Epic" and previous["status"] != "Done" and status == "Done":
            try:
                project_lifecycle.report_done_epic(ctx, previous["project"], key)
            except Exception as e:  # noqa: BLE001
                log.warning("could not generate completion report for epic %s: %s", key, e)
        if previous and previous["status"] != "Human review" and status == "Human review":
            notify_human_review(ctx, key, {**previous, "key": key})
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
        if run["step"] == "impact":
            raise HTTPException(403, "an impact run only comments")
        if body.fields:
            ctx.jira.set_fields(key, body.fields)
        for lab in body.add_labels:
            ctx.jira.add_label(key, lab)
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN %s = ANY(labels) THEN labels ELSE array_append(labels,%s) END"
                       " WHERE key=%s", (lab, lab, key))
        for lab in body.remove_labels:
            ctx.jira.remove_label(key, lab)
            ctx.db.run("UPDATE tickets SET labels=array_remove(labels,%s) WHERE key=%s", (lab, key))
        audit("jira", "fields", key, body.run_id, body.fence, run["account_id"], True, "ok",
              {"fields": list(body.fields), "add": body.add_labels, "remove": body.remove_labels})
        return {"ok": True}

    @app.post("/v1/notify/triage")
    def triage_alert(body: TriageAlertBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "notify", "triage-alert", {"chars": len(body.detail)})
        if run["step"] != "triage":
            raise HTTPException(403, "only a triage run can alert the owner")
        blocks = ctx.db.one("SELECT 1 FROM tickets WHERE status <> 'Done' AND %s::text = ANY(blocked_by) LIMIT 1",
                            (run["ticket_key"],))
        severity = "critical" if blocks else "warning"
        sent = notify.send(severity, f"{run['ticket_key']} needs owner decision", body.detail[:3000],
                           ctx.settings.jira_site.rstrip("/") + f"/browse/{run['ticket_key']}"
                           if ctx.settings.jira_site else "")
        audit("notify", "triage-alert", run["ticket_key"], body.run_id, body.fence, run["account_id"], True,
              ",".join(sent) or "no channel configured", {"severity": severity})
        return {"ok": True, "severity": severity, "sent": sent}

    @app.post("/v1/jira/{key}/description")
    def jira_description(key: str, body: JiraWrite, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "jira", "description", {"chars": len(body.body)})
        if run["ticket_key"] != key:
            raise HTTPException(403, "run is for another ticket")
        if run["step"] != "intake":
            raise HTTPException(403, "only an intake run replaces the description")
        if not body.body.strip():
            raise HTTPException(400, "the new description is empty")
        ctx.jira.set_description(key, body.body)
        ctx.db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (body.body, key))
        audit("jira", "description", key, body.run_id, body.fence, run["account_id"], True, "replaced",
              {"chars": len(body.body)})
        ctx.events.emit("jira.description", ticket=key, run_id=body.run_id, fence=body.fence, account=run["account_id"],
                        node=node, outcome="replaced")
        return {"ok": True}

    # --- GitHub (fenced) --------------------------------------------------------------------------------------
    @app.post("/v1/github/token")
    def github_token(body: GitHubTokenBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "token", {"repo": body.repo})
        project = ctx.repo().projects.get(run["project"])
        if not project or project.repo.split("/")[-1] != body.repo.split("/")[-1]:
            raise HTTPException(403, "repository does not belong to the run's project")
        permissions = {"contents": "read", "metadata": "read"} if run["step"] == "triage" else {
            "contents": "write", "metadata": "read", "workflows": "write"}
        token, expires = ctx.github.token(project.repo_name, permissions)
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
            raise HTTPException(403, "only a verify run can merge")
        return merge_command(run, body.number, body.title, body.method, node)

    @app.get("/v1/reviews")
    def human_reviews() -> dict:
        """Ticket review queue with the latest PR, diff totals, verification result and gated preview link."""
        repo = ctx.repo()
        tickets = ctx.db.all("SELECT key, project, type, summary, description, labels, parent_key"
                             " FROM tickets WHERE status='Human review' ORDER BY project, key")
        epics = {r["key"]: r["status"] for r in ctx.db.all(
            "SELECT key, status FROM tickets WHERE key='TESTFLOW-5' AND type='Epic'")}
        out = []
        for ticket in tickets:
            dev = ctx.db.one("SELECT pr_url, summary FROM runs WHERE ticket_key=%s AND pr_url<>''"
                             " ORDER BY started_at DESC LIMIT 1", (ticket["key"],))
            pr_url = (dev or {}).get("pr_url", "")
            match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
            project = repo.projects.get(ticket["project"])
            pr_detail: dict[str, Any] = {}
            if project and match and ctx.github:
                try:
                    pr_detail = ctx.github.pr(project.repo_name, int(match.group(1))) or {}
                except Exception as e:  # noqa: BLE001
                    log.warning("could not load PR details for %s: %s", ticket["key"], e)
            verify = ctx.db.one("SELECT outcome, summary, details, started_at FROM runs"
                                " WHERE ticket_key=%s AND step='verify' ORDER BY started_at DESC LIMIT 1",
                                (ticket["key"],))
            details = (verify or {}).get("details") or {}
            preview_url = ""
            preview = (project.environments or {}).get("preview") if project else None
            if isinstance(preview, dict):
                preview_url = str(preview.get("url") or "")
            elif isinstance(preview, str):
                preview_url = preview
            if ticket["project"] != "TESTFLOW" or epics.get("TESTFLOW-5") != "Done":
                preview_url = ""
            out.append({
                **ticket,
                "jira_url": f"{ctx.settings.jira_site.rstrip('/')}/browse/{ticket['key']}",
                "pr": {"url": pr_url, "number": int(match.group(1)) if match else None,
                       "title": pr_detail.get("title", ""),
                       "additions": pr_detail.get("additions"), "deletions": pr_detail.get("deletions"),
                       "changed_files": pr_detail.get("changed_files")},
                "verify": {"outcome": (verify or {}).get("outcome", ""),
                           "summary": (verify or {}).get("summary", ""),
                           "verdict": details.get("verdict", ""), "checks": details.get("checks", ""),
                           "findings": details.get("findings", "")},
                "preview_url": preview_url,
            })
        return {"tickets": out}

    @app.post("/v1/reviews/{key}/approve")
    def approve_review(key: str, _reviewer: str = Depends(reviewer_auth)) -> dict:
        ticket, run_id, fence, number = reviewer_run(key)
        try:
            run = fenced(run_id, fence, "github", "merge", {"number": number})
            project = ctx.repo().projects[ticket["project"]]
            result = merge_command(run, number, f"{key}: {ticket['summary']}",
                                   (project.merge or {}).get("method", "squash"), "control")
            if not result.get("merged"):
                finish_reviewer_run(key, run_id, fence, "failed", result.get("message", "merge refused"))
                return {"merged": False, "message": result.get("message", "GitHub refused the merge")}
            comment_run = fenced(run_id, fence, "jira", "comment", {"chars": len(result.get("sha", ""))})
            note = f"Human review approved in AI Flow Console. PR #{number} merged as {result.get('sha', '')[:10]}."
            comment_id = ctx.jira.comment(key, note)
            audit("jira", "comment", key, run_id, fence, comment_run["account_id"], True, comment_id,
                  {"source": "human-review", "chars": len(note)})
            transition_run = fenced(run_id, fence, "jira", "transition", {"to": "Done"})
            status = ctx.jira.transition_to(key, "Done")
            ctx.db.run("UPDATE tickets SET status=%s, done_at=coalesce(done_at,now()), synced_at=now() WHERE key=%s",
                       (status, key))
            audit("jira", "transition", key, run_id, fence, transition_run["account_id"], True, status,
                  {"to": "Done", "source": "human-review"})
            ctx.events.emit("jira.transition", ticket=key, run_id=run_id, fence=fence, account="reviewer-console",
                            node="control", outcome=status, reason="human review approved")
            finish_reviewer_run(key, run_id, fence, "merged", note)
            return {"merged": True, "sha": result.get("sha", ""), "status": status}
        except HTTPException:
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review approval failed for %s", key)
            finish_reviewer_run(key, run_id, fence, "failed", "review approval failed")
            raise HTTPException(502, "could not complete the review approval") from e

    @app.post("/v1/reviews/{key}/rework")
    def request_review_rework(key: str, body: ReviewerBody, _reviewer: str = Depends(reviewer_auth)) -> dict:
        comment = body.comment.strip()
        if not comment:
            raise HTTPException(400, "a rework comment is required")
        ticket, run_id, fence, _number = reviewer_run(key)
        try:
            comment_run = fenced(run_id, fence, "jira", "comment", {"chars": len(comment)})
            comment_id = ctx.jira.comment(key, f"Human review requested rework:\n\n{comment}")
            audit("jira", "comment", key, run_id, fence, comment_run["account_id"], True, comment_id,
                  {"source": "human-review", "chars": len(comment)})
            fields_run = fenced(run_id, fence, "jira", "fields", {"add": ["rework"]})
            ctx.jira.add_label(key, "rework")
            ctx.db.run("UPDATE tickets SET labels=CASE WHEN 'rework'=ANY(labels) THEN labels"
                       " ELSE array_append(labels,'rework') END WHERE key=%s", (key,))
            audit("jira", "fields", key, run_id, fence, fields_run["account_id"], True, "rework label added",
                  {"source": "human-review", "add": ["rework"]})
            transition_run = fenced(run_id, fence, "jira", "transition", {"to": "To Do"})
            status = ctx.jira.transition_to(key, "To Do")
            ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, key))
            audit("jira", "transition", key, run_id, fence, transition_run["account_id"], True, status,
                  {"to": "To Do", "source": "human-review"})
            ctx.events.emit("jira.transition", ticket=key, run_id=run_id, fence=fence, account="reviewer-console",
                            node="control", outcome=status, reason="human review requested rework")
            finish_reviewer_run(key, run_id, fence, "rework", comment)
            return {"status": status, "comment_id": comment_id}
        except HTTPException:
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("human review rework failed for %s", key)
            finish_reviewer_run(key, run_id, fence, "failed", "review rework failed")
            raise HTTPException(502, "could not return the ticket for rework") from e

    @app.post("/v1/github/update-branch")
    def github_update_branch(body: PRCommentBody, node: str = Depends(node_auth)) -> dict:
        run = fenced(body.run_id, body.fence, "github", "update-branch", {"number": body.number})
        if run["step"] != "verify":
            raise HTTPException(403, "only a verify run updates a PR branch")
        project = ctx.repo().projects[run["project"]]
        res = ctx.github.update_branch(project.repo_name, body.number)
        audit("github", "update-branch", run["ticket_key"], body.run_id, body.fence, run["account_id"], res["ok"],
              res["message"][:200], {"number": body.number})
        return res

    @app.get("/v1/github/checks")
    def github_checks(run_id: str, fence: int, sha: str, node: str = Depends(node_auth)) -> dict:
        run = run_row(run_id)
        if run["node"] != node or not ctx.leases.valid(run["ticket_key"], run_id, fence):
            raise HTTPException(409, "not the owner of this run")
        project = ctx.repo().projects[run["project"]]
        return {"checks": ctx.github.checks(project.repo_name, sha)}

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
        if "credential_expires_at" in body.model_fields_set:
            ctx.db.run("UPDATE accounts SET credential_expires_at=%s WHERE id=%s",
                       (body.credential_expires_at, account_id))
        if body.error_kind in ("quota", "auth"):
            until = body.reset_at or (utcnow() + timedelta(minutes=30))
            seconds = max(60, int((until - utcnow()).total_seconds()))
            ctx.leases.cooldown(account_id, seconds, body.error_kind)
            ctx.db.run("UPDATE accounts SET status=%s, cooldown_until=%s, last_error=%s,"
                       " cooldown_since=CASE WHEN %s='quota' THEN coalesce(cooldown_since,now()) ELSE NULL END,"
                       " idle_since=NULL WHERE id=%s AND status<>'working'",
                       ("cooldown" if body.error_kind == "quota" else "signed_out", until, body.summary[:300],
                        body.error_kind, account_id))
        elif body.ok:
            ctx.leases.uncool(account_id)
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'idle' END,"
                       " cooldown_until=NULL, last_error='', cooldown_since=NULL,"
                       " idle_since=CASE WHEN status='working' THEN NULL ELSE coalesce(idle_since,now()) END"
                       " WHERE id=%s", (account_id,))
        else:
            ctx.db.run("UPDATE accounts SET status=CASE WHEN status='working' THEN status ELSE 'error' END, last_error=%s,"
                       " cooldown_since=NULL, idle_since=NULL"
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
    @app.get("/v1/projects/{key}/report")
    def project_report(key: str, epic: str | None = None, format: str = Query(default="json")):
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        if format not in ("json", "md", "csv", "html"):
            raise HTTPException(400, "format must be json, md, csv or html")
        try:
            report = build_report(ctx, key, epic)
        except ReportNotFound as e:
            raise HTTPException(404, str(e)) from e
        if format == "md":
            filename = f"{epic or key}-progress.md"
            return PlainTextResponse(render_markdown(report), media_type="text/markdown",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "csv":
            filename = f"{epic or key}-progress.csv"
            return PlainTextResponse(render_csv(report), media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        if format == "html":
            return HTMLResponse(render_html(report))
        return report

    @app.post("/v1/projects/{key}/close")
    def close_project(key: str, body: CloseProjectBody | None = None) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        result = project_lifecycle.close_project(ctx, key, body.actor if body else "owner")
        return {"project": key, "status": result["status"], "version": result["version"],
                "report": result.get("report"), "markdown": result.get("markdown"),
                "study_case_ref": result["study_case_ref"], "created": result["created"],
                "sent_channels": result["sent_channels"]}

    @app.get("/v1/projects/{key}/study-cases")
    def project_study_cases(key: str, limit: int = Query(50, ge=1, le=100)) -> dict:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        cases = project_lifecycle.study_cases(ctx.db, key, limit)
        return {"project": key, "study_cases": cases}

    # --- Business view: change requests (same access as the console, no node token) -----------------------------
    def business_project(key: str) -> str:
        if key not in ctx.repo().projects:
            raise HTTPException(404, f"unknown project {key}")
        return key

    @app.get("/v1/business/{key}/change-requests")
    def business_change_requests(key: str) -> dict:
        project = business_project(key)
        return {"project": project, "jira_site": ctx.settings.jira_site, "epics": business.epics(ctx.db, project),
                "change_requests": business.change_requests(ctx.db, project, ctx.settings.jira_site)}

    @app.post("/v1/business/{key}/change-requests")
    def business_change_request(key: str, body: ChangeRequestBody) -> dict:
        project = business_project(key)
        try:
            title, description, epic = business.validate(ctx.db, project, body.title, body.description, body.epic)
        except business.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        created = business.create(ctx, project, title, description, epic)
        audit("jira", "create", created["key"], "", None, business.ACTOR, True, created["status"],
              {"project": project, "type": "Story", "labels": [business.LABEL], "epic": epic,
               "chars": len(description)})
        ctx.events.emit("business.change_request", ticket=created["key"], project=project, outcome=created["status"],
                        reason=title[:200])
        return created

    # --- Stakeholder acceptance and UAT feedback --------------------------------------------------------------
    def stakeholder_project(key: str):
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, f"unknown project {key}")
        return project

    def stakeholder_changes(project: str) -> list[dict[str, Any]]:
        rows = ctx.db.all(
            "SELECT DISTINCT ON (t.key) t.key, t.summary, t.type, t.status, t.parent_key, p.type AS parent_type,"
            " r.branch, r.head_sha, r.summary AS change_summary, r.details, r.started_at, r.ended_at"
            " FROM runs r JOIN tickets t ON t.key=r.ticket_key LEFT JOIN tickets p ON p.key=t.parent_key"
            " WHERE r.project=%s AND t.project=%s AND r.step='dev' AND r.status<>'running'"
            " AND r.outcome IN ('done','merged')"
            " ORDER BY t.key, r.started_at DESC", (project, project))
        rows.sort(key=lambda r: r["ended_at"] or r["started_at"], reverse=True)
        out = []
        for row in rows[:30]:
            details = row["details"] or {}
            epic = row["key"] if row["type"] == "Epic" else (
                row["parent_key"] if row["parent_type"] == "Epic" else "")
            release_commit = (details.get("release") or details.get("version") or row["head_sha"] or row["branch"] or "")
            out.append({"key": row["key"], "summary": row["summary"], "type": row["type"],
                        "status": row["status"], "epic": epic, "branch": row["branch"],
                        "commit": row["head_sha"], "release_commit": release_commit,
                        "change_summary": row["change_summary"],
                        "changed_at": (row["ended_at"] or row["started_at"]).isoformat()})
        return out

    @app.get("/v1/stakeholder/{key}")
    def stakeholder_view(key: str) -> dict:
        project = stakeholder_project(key)
        configured = project.urls or {}
        return {"project": key, "urls": {"develop": str(configured.get("develop") or ""),
                                          "production": str(configured.get("production") or "")},
                "changes": stakeholder_changes(key)}

    @app.post("/v1/stakeholder/{key}/verdict")
    def stakeholder_verdict(key: str, body: StakeholderVerdictBody) -> dict:
        project = stakeholder_project(key)
        if not ctx.jira:
            raise HTTPException(503, "Jira is unavailable")
        sender = body.sender.strip()
        if not sender:
            raise HTTPException(400, "sender is required")
        change = ctx.db.one("SELECT key, summary, type, parent_key FROM tickets WHERE key=%s AND project=%s",
                            (body.change_key, key))
        if not change:
            raise HTTPException(400, "change must belong to this project")
        target = body.target_key.strip() or body.change_key
        if target != body.change_key and target != change["parent_key"]:
            raise HTTPException(400, "acceptance target must be the related ticket or its epic")
        if target != body.change_key and not ctx.db.one(
                "SELECT key FROM tickets WHERE key=%s AND project=%s AND type='Epic'", (target, key)):
            raise HTTPException(400, "acceptance target must be the related epic")
        if body.verdict == "not_expected" and (not body.expected.strip() or not body.actual.strip()):
            raise HTTPException(400, "expected and actual behaviour are required for UAT feedback")

        screenshot = None
        if body.screenshot_data:
            match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=]+)", body.screenshot_data)
            if not match:
                raise HTTPException(400, "screenshot must be a PNG, JPEG, or WebP data URL")
            try:
                content = base64.b64decode(match.group(2), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(400, "screenshot data is invalid") from exc
            if not content or len(content) > 5_000_000:
                raise HTTPException(400, "screenshot must be between 1 byte and 5 MB")
            mime = match.group(1)
            filename = Path(body.screenshot_filename).name
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:200] or "screenshot"
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
            if not filename.lower().endswith(ext):
                filename = filename.rsplit(".", 1)[0] + ext
            screenshot = (filename, content, mime)

        page_url = str((project.urls or {}).get(body.environment) or "").strip()
        page_label = page_url or "chưa deploy"
        release_commit = body.release_commit.strip() or "chưa có dữ liệu"
        if body.verdict == "expected":
            comment = (f"Comment nghiệm thu\nMôi trường: {body.environment}\n"
                       f"Release hoặc commit: {release_commit}\nNgười gửi: {sender}")
            try:
                comment_id = ctx.jira.comment(target, comment)
            except Exception as exc:  # Jira adapters and HTTP clients can raise different transport exceptions.
                log.warning("Stakeholder acceptance comment failed for %s", target)
                raise HTTPException(502, "Jira could not record the acceptance") from exc
            audit("jira", "stakeholder_acceptance", target, "", None, sender, True, comment_id,
                  {"project": key, "change": body.change_key, "environment": body.environment})
            ctx.events.emit("stakeholder.acceptance", ticket=target, project=key, outcome="accepted",
                            account=sender, reason=body.environment)
            return {"ok": True, "verdict": body.verdict, "ticket": target, "comment_id": comment_id}

        description = (f"Related change: {change['key']} - {change['summary']}\n"
                       f"Page URL: {page_label}\nEnvironment: {body.environment}\n"
                       f"Release or commit: {release_commit}\n"
                       f"Expected behaviour (mong đợi):\n{body.expected.strip()}\n\n"
                       f"Actual behaviour (thực tế):\n{body.actual.strip()}\n\n"
                       f"Sender (người gửi): {sender}")
        summary = f"[UAT {body.environment}] {change['key']}: {body.actual.strip().splitlines()[0][:120]}"
        try:
            issue_key = ctx.jira.create_issue(key, summary, description, issuetype="Bug", labels=["uat-bug"])
            status = ctx.jira.transition_to(issue_key, "Intake")
            if status != "Intake":
                raise RuntimeError("new UAT issue did not reach Intake")
            if screenshot:
                ctx.jira.add_attachment(issue_key, screenshot[0], screenshot[1], screenshot[2])
        except Exception as exc:  # Never tell the stakeholder a partial Jira write succeeded.
            log.warning("Stakeholder UAT issue creation failed for project %s", key)
            raise HTTPException(502, "Jira could not record the UAT feedback") from exc
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created, jira_updated)"
                   " VALUES (%s,%s,'Intake','Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO UPDATE SET"
                   " status='Intake', type='Bug', labels=EXCLUDED.labels, summary=EXCLUDED.summary,"
                   " description=EXCLUDED.description, synced_at=now()",
                   (issue_key, key, ["uat-bug"], summary, description))
        audit("jira", "stakeholder_bug", issue_key, "", None, sender, True, status,
              {"project": key, "change": body.change_key, "environment": body.environment,
               "screenshot": bool(screenshot)})
        ctx.events.emit("stakeholder.feedback", ticket=issue_key, project=key, outcome="created",
                        account=sender, reason=body.environment, data={"related": body.change_key})
        return {"ok": True, "verdict": body.verdict, "key": issue_key, "status": status}

    @app.get("/v1/state")
    def state() -> dict:
        from .state import snapshot
        return snapshot(ctx)

    @app.get("/v1/tickets/{key}/timeline")
    def ticket_timeline(key: str) -> dict:
        from .timeline import timeline
        tl = timeline(ctx.db, key)
        if tl is None:
            raise HTTPException(404, "unknown ticket")
        return tl

    @app.get("/v1/analytics/daily")
    def analytics_daily(days: int = Query(14, ge=1, le=90), project: str = "") -> dict:
        from .analytics import daily
        return {"days": days, "project": project, "rows": daily(ctx.db, days, project)}

    # --- memory: past tickets, run summaries and verify findings ------------------------------------------------
    @app.get("/v1/memory/search")
    def memory_search(q: str = "", project: str = "", limit: int = 10) -> dict:
        return {"project": project, "q": q, "hits": memory.search(ctx.db, project, q, limit)}

    @app.get("/v1/memory/study-cases")
    def memory_study_cases(project: str = "", limit: int = Query(50, ge=1, le=100)) -> dict:
        return {"project": project, "study_cases": project_lifecycle.study_cases(ctx.db, project, limit)}

    # --- Knowledge screen: versioned control-repo resources -------------------------------------------------
    def load_managed_resources() -> None:
        knowledge.import_local(ctx.db, ctx.repo().root)

    def submit_resource_change(kind: str, resource_id: str, title: str, content: str, archived: bool,
                               actor: str, action: str, expected_version: int | None = None) -> dict:
        try:
            kind, resource_id, title, content, actor = knowledge.validate(kind, resource_id, title, content, actor)
        except knowledge.Invalid as e:
            raise HTTPException(400, str(e)) from e
        if not ctx.github:
            raise HTTPException(503, "control repo bot is unavailable")
        load_managed_resources()
        current = knowledge.get_resource(ctx.db, kind, resource_id)
        if expected_version is not None and (not current or int(current["version"]) != expected_version):
            raise HTTPException(409, "resource changed since it was loaded; refresh and try again")
        path = knowledge.path_for(kind, resource_id)
        repo_name = Path(ctx.settings.control_repo).name
        payload = knowledge.file_content(kind, resource_id, title, content, archived)
        try:
            base = ctx.github.default_branch(repo_name)
            if kind == "policy":
                branch = f"aiflow/policy-{resource_id.lower()}-{secrets.token_hex(4)}"
                ctx.github.create_branch(repo_name, branch, base)
                sha = ctx.github.commit_file(repo_name, branch, path, payload,
                                             f"policy: propose {action} {resource_id}")
                pr = ctx.github.create_control_pr(repo_name, branch, base, f"Policy: {title}",
                                                  f"Policy {action} for `{resource_id}` proposed by {actor}.\n\n"
                                                  "Merge only after owner approval in the AI Flow Knowledge screen.")
                ctx.db.run("INSERT INTO managed_resource_proposals (resource_type,resource_id,title,content,archived,"
                           "base_title,base_content,base_archived,base_version,actor,action,branch,pr_number,pr_url)"
                           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                           (kind, resource_id, title, content, archived, current["title"] if current else "",
                            current["content"] if current else "", current["archived"] if current else False,
                            current["version"] if current else 0, actor, action, branch, int(pr["number"]),
                            str(pr.get("html_url") or "")))
                audit("aiflow-control", "policy-pr", "", "", None, actor, True, str(pr["html_url"]),
                      {"type": kind, "id": resource_id, "branch": branch, "pr": int(pr["number"]), "commit": sha})
                ctx.events.emit("resource.policy_proposed", project=resource_id if resource_id != "default" else "",
                                account=actor, outcome="pending", reason=f"PR #{pr['number']}",
                                data={"type": kind, "id": resource_id})
                return {"pending": True, "type": kind, "id": resource_id, "title": title,
                        "pr_number": int(pr["number"]), "pr_url": str(pr.get("html_url") or ""), "version":
                        int(current["version"]) if current else 0}

            sha = ctx.github.commit_file(repo_name, base, path, payload, f"{kind}: {action} {resource_id}")
        except HTTPException:
            raise
        except Exception as e:  # GitHub errors vary between App and transport implementations.
            log.warning("control repo write failed for %s/%s", kind, resource_id)
            raise HTTPException(502, "control repo could not save the resource") from e
        row = knowledge.save_current(ctx.db, kind=kind, resource_id=resource_id, title=title, content=content,
                                     archived=archived, actor=actor, action=action, path=path, commit_sha=sha)
        audit("aiflow-control", "resource-commit", "", "", None, actor, True, sha,
              {"type": kind, "id": resource_id, "version": row["version"], "path": path})
        ctx.events.emit("resource.changed", account=actor, outcome=action, reason=f"{kind}/{resource_id}",
                        data={"type": kind, "id": resource_id, "version": row["version"], "commit": sha})
        return row

    @app.get("/v1/knowledge/resources")
    def knowledge_resources(include_archived: bool = True) -> dict:
        load_managed_resources()
        return {"resources": knowledge.list_resources(ctx.db, include_archived),
                "proposals": knowledge.proposals(ctx.db)}

    @app.get("/v1/knowledge/resources/{kind}/{resource_id}/history")
    def knowledge_history(kind: str, resource_id: str) -> dict:
        load_managed_resources()
        if kind not in knowledge.KINDS:
            raise HTTPException(404, "unknown resource type")
        return {"type": kind, "id": resource_id, "history": knowledge.history(ctx.db, kind, resource_id),
                "proposals": knowledge.proposals(ctx.db, kind, resource_id)}

    @app.post("/v1/knowledge/resources")
    def knowledge_save(body: KnowledgeBody) -> dict:
        if not body.id.strip():
            raise HTTPException(400, "resource id is required")
        action = "archive" if body.archived else "save"
        return submit_resource_change(body.type, body.id, body.title, body.content, body.archived, body.actor, action,
                                      body.version)

    @app.post("/v1/knowledge/resources/{kind}/{resource_id}/restore")
    def knowledge_restore(kind: str, resource_id: str, body: KnowledgeRestoreBody) -> dict:
        load_managed_resources()
        version = ctx.db.one("SELECT title,content,archived FROM managed_resource_history WHERE resource_type=%s"
                             " AND resource_id=%s AND version=%s", (kind, resource_id, body.version))
        if not version:
            raise HTTPException(404, "resource version not found")
        return submit_resource_change(kind, resource_id, version["title"], version["content"], version["archived"],
                                      body.actor, "restore")

    @app.post("/v1/knowledge/proposals/{proposal_id}/approve")
    def knowledge_approve(proposal_id: int, body: KnowledgeApprovalBody,
                          _owner: str = Depends(owner_auth)) -> dict:
        proposal = ctx.db.one("SELECT * FROM managed_resource_proposals WHERE id=%s", (proposal_id,))
        if not proposal:
            raise HTTPException(404, "policy proposal not found")
        if proposal["status"] != "pending":
            raise HTTPException(409, "policy proposal is no longer pending")
        if not ctx.github:
            raise HTTPException(503, "control repo bot is unavailable")
        repo_name = Path(ctx.settings.control_repo).name
        owner = body.approved_by.strip() or "owner"
        try:
            merged = ctx.github.merge_pr(repo_name, int(proposal["pr_number"]),
                                         f"Policy: {proposal['title']}", "squash")
        except Exception as e:  # Do not expose provider responses or credentials through the UI.
            log.warning("policy proposal merge failed for PR #%s", proposal["pr_number"])
            raise HTTPException(502, "policy proposal could not be merged") from e
        if not merged.get("merged"):
            return {"merged": False, "message": merged.get("message", "GitHub refused the merge")}
        resource = knowledge.save_current(
            ctx.db, kind="policy", resource_id=proposal["resource_id"], title=proposal["title"],
            content=proposal["content"], archived=proposal["archived"], actor=proposal["actor"],
            action=proposal["action"], path=knowledge.path_for("policy", proposal["resource_id"]),
            commit_sha=str(merged.get("sha") or ""), approved_by=owner)
        ctx.db.run("UPDATE managed_resource_proposals SET status='merged',merged_at=now(),approved_by=%s WHERE id=%s",
                   (owner, proposal_id))
        audit("aiflow-control", "policy-merge", "", "", None, owner, True, str(merged.get("sha") or ""),
              {"id": proposal["resource_id"], "pr": proposal["pr_number"], "version": resource["version"]})
        ctx.events.emit("resource.policy_merged", project=proposal["resource_id"] if proposal["resource_id"] != "default" else "",
                        account=owner, outcome="merged", reason=f"PR #{proposal['pr_number']}",
                        data={"type": "policy", "id": proposal["resource_id"], "version": resource["version"]})
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception:
                log.warning("control repo refresh after policy merge failed")
        return {"merged": True, "sha": merged.get("sha", ""), "resource": resource}

    return app
