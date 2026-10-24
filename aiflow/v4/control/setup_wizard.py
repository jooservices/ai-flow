"""Browser first-run setup for a new control plane.

The first Administrator and public address are created together from the setup screen. Later setup actions reuse
the existing Integrations, Machines and AI, Projects and node-join APIs; this module keeps their readiness, skip,
resume and sample-ticket confirmation visible until a sample issue is confirmed in Jira Intake.
"""
from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from . import licensing
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
    row = ctx.db.one("SELECT public_url,skipped_steps,sample_ticket_key,completed_at FROM setup_wizard_state WHERE id=true")
    if not row:
        ctx.db.run("INSERT INTO setup_wizard_state (id) VALUES (true) ON CONFLICT (id) DO NOTHING")
        row = ctx.db.one("SELECT public_url,skipped_steps,sample_ticket_key,completed_at FROM setup_wizard_state WHERE id=true")
    return row or {"public_url": "", "skipped_steps": [], "sample_ticket_key": "", "completed_at": None}


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
    sample_ready = False
    sample_status = ""
    if sample_key and jira_ready and ctx.jira:
        try:
            sample_status = ctx.jira.status(sample_key)
            sample_ready = sample_status == "Intake"
        except Exception:  # noqa: BLE001 - explain the observable state without exposing provider details
            sample_status = "unavailable"
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
    all_ready = all(item["ready"] for item in steps.values())
    if all_ready and not row.get("completed_at"):
        ctx.db.run("UPDATE setup_wizard_state SET completed_at=now(),updated_at=now() WHERE id=true"
                   " AND completed_at IS NULL")
    elif not all_ready and row.get("completed_at"):
        ctx.db.run("UPDATE setup_wizard_state SET completed_at=NULL,updated_at=now() WHERE id=true")
    return {"admin_exists": admin_exists, "public_url": url, "completed": all_ready,
            "locked": admin_exists, "sample_ticket": sample_key, "sample_status": sample_status,
            "projects": projects,
            "steps": [{"id": step_id, "title": STEP_LABELS[step_id], **steps[step_id],
                       "action": STEP_ACTIONS.get(step_id)} for step_id in STEP_IDS]}


def _audit(ctx, action: str, actor: str, accepted: bool, result: str, payload: dict | None = None) -> None:
    ctx.db.run("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
               " VALUES ('setup',%s,'',%s,%s,%s,%s)", (action, actor, accepted, result[:300], J(payload or {})))


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


def install(app: FastAPI, ctx) -> None:
    """Mount bootstrap and progress routes. Bootstrap is the only route available before the first user exists."""
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

    @app.get("/v1/setup/status")
    def setup_status(request: Request):
        if user_count(ctx):
            _administrator(request, ctx)
        data = status(ctx)
        if not user_count(ctx):
            nonce, created = _bootstrap_nonce(ctx, request)
            data["bootstrap_token"] = nonce
            response = JSONResponse(data, headers={"Cache-Control": "no-store"})
            if created:
                response.set_cookie(SETUP_COOKIE, nonce, max_age=SETUP_TTL_S, httponly=True,
                                    secure=request.url.scheme == "https",
                                    samesite="strict", path="/v1/setup")
            return response
        response = JSONResponse(data, headers={"Cache-Control": "no-store"})
        return response

    @app.post("/v1/setup/bootstrap")
    async def bootstrap(request: Request):
        nonce = request.cookies.get(SETUP_COOKIE, "")
        supplied = request.headers.get("x-aiflow4-setup", "")
        if (not nonce or not supplied or not secrets.compare_digest(nonce, supplied)
                or not ctx.leases.r.get(_nonce_key(ctx, nonce))):
            raise HTTPException(403, "Reload the setup screen before creating the first Administrator.")
        if ctx.settings.console_login and len(ctx.settings.console_session_secret.encode()) < 32:
            raise HTTPException(503, "console sign-in is not configured")
        try:
            payload = await request.json()
            body = BootstrapBody.model_validate(payload)
        except (ValueError, ValidationError, TypeError):
            raise HTTPException(422, "Enter a public address, a valid email, and a password with at least 12 characters.") from None
        try:
            public_url = validate_public_url(body.public_url)
            email = normalize_email(body.email)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if len(body.password) < 12:
            raise HTTPException(422, "Use a password with at least 12 characters.")
        password_hash = hash_password(body.password)
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
                connection.execute("INSERT INTO console_users (email,display_name,role,roles,password_hash)"
                                   " VALUES (%s,'','administrator',ARRAY['administrator']::text[],%s)",
                                   (email, password_hash))
                connection.execute("UPDATE setup_wizard_state SET public_url=%s,updated_at=now() WHERE id=true",
                                   (public_url,))
                connection.execute("INSERT INTO write_audit (target,action,ticket,actor,accepted,result,payload)"
                                   " VALUES ('setup','bootstrap','',%s,true,'first Administrator created',%s)",
                                   (email, J({"public_url": public_url})))
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

    @app.post("/v1/setup/steps/{step_id}")
    def update_step(step_id: str, body: SkipBody, request: Request):
        user = _administrator(request, ctx)
        if step_id not in STEP_IDS or step_id in NON_SKIPPABLE:
            raise HTTPException(404, "that setup step cannot be skipped")
        if body.skipped:
            ctx.db.run("UPDATE setup_wizard_state SET skipped_steps=array_append(array_remove(skipped_steps,%s),%s),"
                       "updated_at=now() WHERE id=true", (step_id, step_id))
        else:
            ctx.db.run("UPDATE setup_wizard_state SET skipped_steps=array_remove(skipped_steps,%s),updated_at=now()"
                       " WHERE id=true", (step_id,))
        _audit(ctx, "skip_step" if body.skipped else "resume_step", user.subject, True,
               "setup step skipped" if body.skipped else "setup step resumed", {"step": step_id})
        return status(ctx)

    @app.post("/v1/setup/sample-ticket")
    def sample_ticket(body: SampleBody, request: Request):
        user = _administrator(request, ctx)
        if setup_complete(ctx):
            raise HTTPException(409, "Setup is complete. Follow ticket progress on the normal ticket and Dashboard screens.")
        if not jira_connected(ctx) or not ctx.jira:
            raise HTTPException(409, "Connect Jira in Integrations before sending the sample ticket.")
        project = body.project.strip().upper()
        if project not in ctx.repo().projects:
            raise HTTPException(422, "Choose a project already configured in Projects.")
        row = _row(ctx)
        key = str(row.get("sample_ticket_key") or "")
        if not key:
            summary = "AI Flow setup sample ticket"
            description = ("Sample ticket created by the AI Flow first-run setup wizard.\n\n"
                           "This confirms that Jira is connected and this project can receive work in Intake.")
            try:
                key = ctx.jira.create_issue(project, summary, description, issuetype="Task",
                                            labels=["aiflow-setup-sample"])
                ctx.db.run("UPDATE setup_wizard_state SET sample_ticket_key=%s,updated_at=now() WHERE id=true",
                           (key,))
            except Exception as exc:  # noqa: BLE001 - return a plain recovery action, never provider internals
                _audit(ctx, "sample_ticket", user.subject, False, "could not create sample issue", {"project": project})
                raise HTTPException(502, "AI Flow could not create the sample ticket in Jira. Check the connection and try again.") from exc
        try:
            current = ctx.jira.status(key)
            if current != "Intake":
                current = ctx.jira.transition_to(key, "Intake")
            if current != "Intake":
                raise RuntimeError("Jira did not confirm Intake")
        except Exception as exc:  # noqa: BLE001
            _audit(ctx, "sample_ticket", user.subject, False, "sample issue not confirmed in Intake",
                   {"project": project, "ticket": key})
            raise HTTPException(502, f"Ticket {key} exists, but AI Flow could not confirm it in Intake. Check the Jira workflow and try again.") from exc
        ctx.db.run("INSERT INTO tickets (key,project,status,type,labels,summary,description,jira_created,jira_updated)"
                   " VALUES (%s,%s,'Intake','Task',%s,'AI Flow setup sample ticket',%s,now(),now())"
                   " ON CONFLICT (key) DO UPDATE SET status='Intake',synced_at=now()",
                   (key, project, ["aiflow-setup-sample"],
                    "Sample ticket created by the AI Flow first-run setup wizard."))
        completed = status(ctx)["completed"]
        _audit(ctx, "sample_ticket", user.subject, True, "sample ticket confirmed in Intake",
               {"project": project, "ticket": key, "status": "Intake"})
        ctx.events.emit("setup.sample_ticket", ticket=key, project=project, outcome="Intake",
                        account=user.subject, reason="first-run setup sample confirmed")
        tail = ("Setup is complete; follow its progress on the ticket and Dashboard screens." if completed
                else "Skipped or unfinished steps stay on the Dashboard until you finish them.")
        return {"ok": True, "ticket": key, "status": "Intake", "completed": completed,
                "message": f"Sample ticket {key} was created in Intake. {tail}"}

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
