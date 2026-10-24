"""Browser first-run setup for a new control plane.

The first Administrator and public address are created together from the setup screen. Later setup actions reuse
the existing Integrations, Machines and AI, Projects and node-join APIs; this module keeps their readiness, skip,
resume and sample-ticket confirmation visible on the Dashboard until every step is ready.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from . import licensing, setup_service
from .console_auth import COOKIE_NAME, has_permission, issue_session, resolve_session_user, verify_session
from .db import J
from .login import SESSION_TTL_S, hash_password, normalize_email

PAGE = Path(__file__).with_name("ui") / "setup.html"
PROGRESS_SCRIPT = Path(__file__).with_name("ui") / "setup-progress.js"
COOKIE_PATH = "/"
NON_SKIPPABLE = {"public_address", "administrator"}
STEP_IDS = ("public_address", "administrator", "jira", "github", "machine", "ai_accounts", "project",
            "sample_ticket")
STEP_LABELS = {
    "public_address": "Public address",
    "administrator": "Administrator",
    "jira": "Connect Jira",
    "github": "Connect GitHub",
    "machine": "Add the first machine",
    "ai_accounts": "Add AI accounts",
    "project": "Add the first project and repositories",
    "sample_ticket": "Send a sample ticket to Intake",
}
STEP_ACTIONS = {
    "jira": {"url": "/integrations", "label": "Connect Jira in Integrations"},
    "github": {"url": "/integrations", "label": "Connect GitHub in Integrations"},
    "machine": {"url": "/machines", "label": "Open Machines and AI"},
    "ai_accounts": {"url": "/machines", "label": "Add AI accounts in Machines and AI"},
    "project": {"url": "/projects", "label": "Add a project in Projects"},
}
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}\Z")
SETUP_COOKIE = "aiflow4_setup_nonce"
SETUP_TTL_S = 1800
OWNERSHIP_HEADER = "x-aiflow4-ownership"


class BootstrapBody(BaseModel):
    model_config = {"extra": "forbid"}
    public_url: str = Field(min_length=1, max_length=300)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)


class SampleBody(BaseModel):
    model_config = {"extra": "forbid"}
    project: str = Field(min_length=2, max_length=32)


class SkipBody(BaseModel):
    model_config = {"extra": "forbid"}
    skipped: bool


def user_count(ctx) -> int:
    return int(ctx.db.one("SELECT count(*) AS count FROM console_users")["count"])


def setup_complete(ctx) -> bool:
    row = ctx.db.one("SELECT completed_at FROM setup_wizard_state WHERE id=true")
    return bool(row and row.get("completed_at"))


def jira_connected(ctx) -> bool:
    """The Jira client exists on every control process; readiness comes from its credential source."""
    auth = getattr(ctx, "jira_auth", None)
    if auth is not None:
        try:
            return bool(auth.available())
        except Exception:  # noqa: BLE001 - report a missing connection without exposing provider details
            return False
    return bool(ctx.jira)


def validate_public_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        parts = None
    if (not parts or parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.path or parts.query or parts.fragment or len(value) > 300):
        raise ValueError("Enter a public http or https address with no path, query, or password.")
    return value


def _row(ctx) -> dict:
    columns = "public_url,skipped_steps,sample_ticket_key,sample_confirmed_at,completed_at"
    row = ctx.db.one(f"SELECT {columns} FROM setup_wizard_state WHERE id=true")
    if not row:
        ctx.db.run("INSERT INTO setup_wizard_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING")
        row = ctx.db.one(f"SELECT {columns} FROM setup_wizard_state WHERE id=true")
    return row or {"public_url": "", "skipped_steps": [], "sample_ticket_key": "", "sample_confirmed_at": None,
                   "completed_at": None}


def _administrator(request: Request, ctx):
    claims = verify_session(request.cookies.get(COOKIE_NAME, ""), ctx.settings.console_session_secret)
    user = resolve_session_user(ctx.db, claims) if claims else None
    if not user:
        raise HTTPException(401, "sign in as the Administrator to continue setup")
    if not user.is_administrator or not has_permission(user.effective_roles, "integrations.manage"):
        raise HTTPException(403, "only the Administrator can change first-run setup")
    return user


def _step(ready: bool, missing: str, skipped: bool = False) -> dict:
    return {"ready": bool(ready), "missing": "" if ready else missing, "skipped": bool(skipped)}


def status(ctx) -> dict:
    """Check each setup step from its real source and keep its missing action in plain language."""
    row = _row(ctx)
    skipped = set(row.get("skipped_steps") or [])
    admin_exists = user_count(ctx) > 0
    stored_url = str(row.get("public_url") or "").strip()
    url = stored_url or str(ctx.settings.public_url or "").strip()
    from .api import github_connection_list

    try:
        github_ready = bool(github_connection_list(ctx))
    except Exception:  # noqa: BLE001 - the setup screen reports a missing connection, not internal details
        github_ready = False
    jira_ready = jira_connected(ctx)
    worker_nodes = ctx.db.all("SELECT name FROM nodes WHERE role='worker' AND removed_at IS NULL ORDER BY name")
    worker_names = {str(item["name"]) for item in worker_nodes}
    account_rows = ctx.db.all("SELECT id,node,status FROM accounts ORDER BY id")
    account_ready = any(str(item.get("node") or "") in worker_names
                        and str(item.get("status") or "") in {"idle", "working", "probing"}
                        for item in account_rows)
    projects = sorted(ctx.repo().projects)
    sample_key = str(row.get("sample_ticket_key") or "")
    # Ready from the stored Jira confirmation, never from the live status: the flow moves the sample on, and the
    # Dashboard poll must not call Jira.
    sample_ready = bool(row.get("sample_confirmed_at"))
    sample_status = "Intake" if sample_ready else ""
    steps = {
        "public_address": _step(bool(stored_url or (admin_exists and ctx.settings.public_url)),
                                "Choose the address people use to open AI Flow."),
        "administrator": _step(admin_exists, "Create the first Administrator with an email and password."),
        "jira": _step(jira_ready, "Connect a Jira account in Integrations."),
        "github": _step(github_ready, "Connect a GitHub App or an existing App in Integrations."),
        "machine": _step(bool(worker_names), "Add a machine and use its one-time join command."),
        "ai_accounts": _step(account_ready, "Add an AI account to a machine and finish its sign-in."),
        "project": _step(bool(projects), "Add a Jira project and at least one repository in Projects."),
        "sample_ticket": _step(sample_ready,
                                "Choose a configured project and send a sample ticket. It must appear in Jira Intake."),
    }
    for step_id in STEP_IDS:
        steps[step_id]["skipped"] = step_id in skipped
    # Setup is finished only when every step is ready; a skipped step keeps the Dashboard panel and stays resumable.
    # Completion is sticky: once every step was ready it is never cleared by a later change.
    all_ready = bool(row.get("completed_at")) or all(item["ready"] for item in steps.values())
    if all_ready and not row.get("completed_at"):
        ctx.db.run("UPDATE setup_wizard_state SET completed_at=now(),updated_at=now() WHERE id=true"
                   " AND completed_at IS NULL")
    return {"admin_exists": admin_exists, "public_url": url, "completed": all_ready,
            "locked": admin_exists, "sample_ticket": sample_key, "sample_status": sample_status,
            "projects": projects,
            "steps": [{"id": step_id, "title": STEP_LABELS[step_id], **steps[step_id],
                       "action": STEP_ACTIONS.get(step_id)} for step_id in STEP_IDS]}


def _nonce_key(ctx, nonce: str) -> str:
    import hashlib
    return ctx.leases.p + "setup_bootstrap:" + hashlib.sha256(nonce.encode()).hexdigest()


def _bootstrap_nonce(ctx, request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(SETUP_COOKIE, "")
    if existing and ctx.leases.r.get(_nonce_key(ctx, existing)):
        return existing, False
    nonce = secrets.token_urlsafe(32)
    ctx.leases.r.set(_nonce_key(ctx, nonce), "1", ex=SETUP_TTL_S)
    return nonce, True


def _validate_bootstrap_request(request: Request, ctx) -> tuple[str, str]:
    if not _secure_setup_transport(request, ctx):
        raise HTTPException(400, "Use HTTPS to create the first Administrator from a remote browser.")
    nonce = request.cookies.get(SETUP_COOKIE, "")
    supplied_nonce = request.headers.get("x-aiflow4-setup", "")
    if (not nonce or not supplied_nonce or not secrets.compare_digest(nonce, supplied_nonce)
            or not ctx.leases.r.get(_nonce_key(ctx, nonce))):
        raise HTTPException(403, "Reload the setup screen before creating the first Administrator.")
    capability = request.headers.get(OWNERSHIP_HEADER, "")
    expected = str(getattr(ctx.settings, "setup_ownership_capability", "") or "")
    if not expected:
        raise HTTPException(503, "The installation ownership capability is not configured.")
    if not capability or not secrets.compare_digest(capability, expected):
        raise HTTPException(403, "Enter the installation ownership capability to create the first Administrator.")
    return nonce, capability


def _secure_setup_transport(request: Request, ctx) -> bool:
    """Require TLS remotely; allow direct loopback development and trusted TLS-terminating proxies."""
    return _request_is_https(request, ctx) or _loopback_client(request)


def _request_is_https(request: Request, ctx) -> bool:
    if request.url.scheme.lower() == "https":
        return True
    peer = request.client.host if request.client else ""
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for value in getattr(ctx.settings, "trusted_proxies", ()) or ():
        try:
            if peer_address in ipaddress.ip_network(value, strict=False):
                forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
                return forwarded_proto.lower() == "https"
        except ValueError:
            continue
    return False


def _loopback_client(request: Request) -> bool:
    try:
        return bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
    except ValueError:
        return False


async def _bootstrap_payload(request: Request) -> BootstrapBody:
    try:
        payload = await request.json()
        return BootstrapBody.model_validate(payload)
    except (ValueError, ValidationError, TypeError):
        raise HTTPException(422, "Enter a public address, a valid email, and a password with at least 12 characters.") from None


def _normalized_bootstrap(body: BootstrapBody) -> tuple[str, str]:
    try:
        return validate_public_url(body.public_url), normalize_email(body.email)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _create_first_admin(ctx, public_url: str, email: str, password_hash: str, capability: str) -> None:
    expected = str(getattr(ctx.settings, "setup_ownership_capability", "") or "")
    if not expected or not secrets.compare_digest(capability, expected):
        raise HTTPException(403, "The installation ownership capability is invalid.")
    with ctx.db.pool.connection() as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                               ("aiflow4-first-admin",))
            if connection.execute("SELECT 1 FROM console_users LIMIT 1").fetchone():
                raise HTTPException(409, "An Administrator already exists. Sign in to continue.")
            try:
                licensing.lock_resource_admission(connection)
                count = int(connection.execute(
                    "SELECT count(*) AS count FROM console_users WHERE disabled=false").fetchone()["count"])
                licensing.require_count_capacity(ctx.db, "users", count,
                                                 public_key=ctx.settings.license_public_key,
                                                 connection=connection)
            except licensing.LicenseError as exc:
                raise HTTPException(423, str(exc)) from None
            claimed = connection.execute(
                "UPDATE setup_wizard_state SET ownership_claimed_at=now(),updated_at=now()"
                " WHERE id=true AND ownership_claimed_at IS NULL RETURNING id").fetchone()
            if not claimed:
                raise HTTPException(409, "The installation ownership capability was already used.")
            connection.execute("INSERT INTO console_users (email,display_name,role,roles,password_hash)"
                               " VALUES (%s,'','administrator',ARRAY['administrator']::text[],%s)",
                               (email, password_hash))
            connection.execute("UPDATE setup_wizard_state SET public_url=%s,updated_at=now() WHERE id=true",
                               (public_url,))
            connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                               " VALUES ('setup','bootstrap','',%s,true,'first Administrator created',%s)",
                               (email, J({"public_url": public_url})))


def _register_setup_pages(app: FastAPI, ctx) -> None:
    @app.get("/setup", include_in_schema=False)
    def setup_page():
        # The wizard screen only creates the first Administrator; later steps live on the Dashboard panel.
        if user_count(ctx):
            return RedirectResponse("/dashboard", status_code=303)
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store",
                                                                     "Referrer-Policy": "no-referrer"})

    @app.get("/ui/setup-progress.js", include_in_schema=False)
    def setup_progress_script():
        return FileResponse(PROGRESS_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "no-store"})


def _register_status_route(app: FastAPI, ctx) -> None:
    @app.get("/v1/setup/status")
    def setup_status(request: Request):
        admin_exists = user_count(ctx) > 0
        if admin_exists:
            _administrator(request, ctx)
            return JSONResponse(status(ctx), headers={"Cache-Control": "no-store"})
        nonce, created = _bootstrap_nonce(ctx, request)
        data = {"admin_exists": False, "locked": False, "bootstrap_token": nonce}
        response = JSONResponse(data, headers={"Cache-Control": "no-store"})
        if created:
            response.set_cookie(SETUP_COOKIE, nonce, max_age=SETUP_TTL_S, httponly=True,
                                secure=_request_is_https(request, ctx),
                                samesite="strict", path="/v1/setup")
        return response


def _register_bootstrap_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/bootstrap")
    async def bootstrap(request: Request):
        nonce, capability = _validate_bootstrap_request(request, ctx)
        if ctx.settings.console_login and len(ctx.settings.console_session_secret.encode()) < 32:
            raise HTTPException(503, "console sign-in is not configured")
        body = await _bootstrap_payload(request)
        public_url, email = _normalized_bootstrap(body)
        if len(body.password) < 12:
            raise HTTPException(422, "Use a password with at least 12 characters.")
        password_hash = hash_password(body.password)
        _create_first_admin(ctx, public_url, email, password_hash, capability)
        ctx.leases.r.delete(_nonce_key(ctx, nonce))
        # Each API replica reads the saved value on requests through the setup middleware below.
        ctx.settings.public_url = public_url
        if not ctx.settings.join_control_url:
            ctx.settings.join_control_url = public_url
        token = issue_session(email, ["administrator"], int(time.time()) + SESSION_TTL_S,
                              ctx.settings.console_session_secret)
        response = JSONResponse({"created": True, "email": email, "next": "/dashboard"},
                                headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL_S, httponly=True,
                            secure=public_url.lower().startswith("https://"), samesite="strict", path=COOKIE_PATH)
        response.delete_cookie(SETUP_COOKIE, path="/v1/setup", httponly=True,
                               secure=public_url.lower().startswith("https://"), samesite="strict")
        return response


def _register_step_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/steps/{step_id}")
    def update_step(step_id: str, body: SkipBody, request: Request):
        user = _administrator(request, ctx)
        if step_id not in STEP_IDS or step_id in NON_SKIPPABLE:
            raise HTTPException(404, "that setup step cannot be skipped")
        setup_service.set_step_skipped(ctx, step_id, body.skipped, user.subject)
        return status(ctx)


@contextmanager
def _sample_ticket_lock(ctx):
    """Hold an installation-wide PostgreSQL lock while this request re-reads and confirms the sample issue."""
    lock_name = "aiflow4-setup-sample-ticket"
    with ctx.db.pool.connection() as connection:
        connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (lock_name,))
        connection.commit()
        try:
            yield connection
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (lock_name,))
            connection.commit()


def _audit_sample(connection, user, accepted: bool, result: str, project: str, key: str = "") -> None:
    connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                       " VALUES ('setup','sample_ticket','',%s,%s,%s,%s)",
                       (user.subject, accepted, result[:300], J({"project": project, "ticket": key})))


def _sample_issue_key(connection, ctx, body: SampleBody, user) -> tuple[str, str, Exception | None]:
    row = connection.execute("SELECT completed_at,sample_ticket_key,sample_confirmed_at"
                             " FROM setup_wizard_state WHERE id=true FOR UPDATE").fetchone()
    key = str(row.get("sample_ticket_key") or "")
    if row.get("completed_at"):
        raise HTTPException(409, "Setup is complete. Follow ticket progress on the normal ticket and Dashboard screens.")
    if row.get("sample_confirmed_at"):
        raise HTTPException(409, f"Sample ticket {key} was already sent to Intake. Follow it on the ticket screens.")
    if not jira_connected(ctx) or not ctx.jira:
        raise HTTPException(409, "Connect Jira in Integrations before sending the sample ticket.")
    project = body.project.strip().upper()
    if project not in ctx.repo().projects:
        raise HTTPException(422, "Choose a project already configured in Projects.")
    if key:
        return key, project, None
    summary = "AI Flow setup sample ticket"
    description = ("Sample ticket created by the AI Flow first-run setup wizard.\n\n"
                   "This confirms that Jira is connected and this project can receive work in Intake.")
    try:
        key = ctx.jira.create_issue(project, summary, description, issuetype="Task",
                                    labels=["aiflow-setup-sample"])
        connection.execute("UPDATE setup_wizard_state SET sample_ticket_key=%s,updated_at=now() WHERE id=true",
                           (key,))
        return key, project, None
    except Exception as exc:  # noqa: BLE001 - return a plain recovery action, never provider internals
        return "", project, exc


def _confirm_sample_issue(connection, ctx, user, key: str, project: str) -> Exception | None:
    try:
        current = ctx.jira.status(key)
        if current != "Intake":
            current = ctx.jira.transition_to(key, "Intake")
        if current != "Intake":
            raise RuntimeError("Jira did not confirm Intake")
    except Exception as exc:  # noqa: BLE001
        _audit_sample(connection, user, False, "sample issue not confirmed in Intake", project, key)
        return exc
    connection.execute("UPDATE setup_wizard_state SET sample_confirmed_at=now(),updated_at=now() WHERE id=true"
                       " AND sample_confirmed_at IS NULL")
    connection.execute("INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated)"
                       " VALUES (%s,%s,'Intake','Task',%s,'AI Flow setup sample ticket',%s,now(),now())"
                       " ON CONFLICT (key) DO UPDATE SET status='Intake',synced_at=now()",
                       (key, project, ["aiflow-setup-sample"],
                        "Sample ticket created by the AI Flow first-run setup wizard."))
    _audit_sample(connection, user, True, "sample ticket confirmed in Intake", project, key)
    return None


def _send_sample_ticket(ctx, body: SampleBody, user) -> dict:
    issue_error = confirmation_error = None
    with _sample_ticket_lock(ctx) as connection:
        with connection.transaction():
            key, project, issue_error = _sample_issue_key(connection, ctx, body, user)
        if issue_error:
            with connection.transaction():
                _audit_sample(connection, user, False, "could not create sample issue", project)
        else:
            with connection.transaction():
                current = connection.execute("SELECT sample_confirmed_at FROM setup_wizard_state"
                                             " WHERE id=true FOR UPDATE").fetchone()
                if current.get("sample_confirmed_at"):
                    raise HTTPException(409, f"Sample ticket {key} was already sent to Intake. Follow it on the ticket screens.")
                confirmation_error = _confirm_sample_issue(connection, ctx, user, key, project)
    if issue_error:
        raise HTTPException(502, "AI Flow could not create the sample ticket in Jira. Check the connection and try again.") from issue_error
    if confirmation_error:
        raise HTTPException(502, f"Ticket {key} exists, but AI Flow could not confirm it in Intake. Check the Jira workflow and try again.") from confirmation_error
    completed = status(ctx)["completed"]
    ctx.events.emit("setup.sample_ticket", ticket=key, project=project, outcome="Intake",
                    account=user.subject, reason="first-run setup sample confirmed")
    tail = ("Setup is complete; follow its progress on the ticket and Dashboard screens." if completed
            else "Skipped or unfinished steps stay on the Dashboard until you finish them.")
    return {"ok": True, "ticket": key, "status": "Intake", "completed": completed,
            "message": f"Sample ticket {key} was created in Intake. {tail}"}


def _register_sample_ticket_route(app: FastAPI, ctx) -> None:
    @app.post("/v1/setup/sample-ticket")
    def sample_ticket(body: SampleBody, request: Request):
        user = _administrator(request, ctx)
        return _send_sample_ticket(ctx, body, user)


def _register_public_address_middleware(app: FastAPI, ctx) -> None:
    public_url_cache = {"read_at": 0.0}

    @app.middleware("http")
    async def load_public_address(request: Request, call_next):
        # The selected address is shared by API replicas through PostgreSQL; refresh it before OAuth and join URLs
        # are built, so the browser never needs to edit the host's environment file.
        if time.monotonic() - public_url_cache["read_at"] >= 5:
            public_url_cache["read_at"] = time.monotonic()
            try:
                row = ctx.db.one("SELECT public_url FROM setup_wizard_state WHERE id=true")
                if row and row.get("public_url"):
                    ctx.settings.public_url = str(row["public_url"])
                    if not ctx.settings.join_control_url:
                        ctx.settings.join_control_url = str(row["public_url"])
            except Exception:  # noqa: BLE001 - database availability is handled by the route doing the work
                pass
        return await call_next(request)


def install(app: FastAPI, ctx) -> None:
    """Mount the bootstrap, setup progress and shared public-address behavior."""
    _register_setup_pages(app, ctx)
    _register_status_route(app, ctx)
    _register_bootstrap_route(app, ctx)
    _register_step_route(app, ctx)
    _register_sample_ticket_route(app, ctx)
    _register_public_address_middleware(app, ctx)
