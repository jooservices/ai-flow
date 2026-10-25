"""Use cases for browser-managed first-run setup: step readiness, the first Administrator and the sample ticket.

Routes in `setup_wizard.py` authorize and map HTTP; this module decides, and `repositories/setup_wizard.py` owns SQL
and transactions. Refusals are raised as `SetupError` with the HTTP status the route should answer.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from . import licensing
from .login import normalize_email
from .repositories import setup_wizard as repo

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
ACCOUNT_READY = {"idle", "working", "probing"}


class SetupError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class SampleBody(BaseModel):
    model_config = {"extra": "forbid"}
    project: str = Field(min_length=2, max_length=32)


def user_count(ctx) -> int:
    return repo.user_count(ctx.db)


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


def _step(ready: bool, missing: str) -> dict:
    return {"ready": bool(ready), "missing": "" if ready else missing, "skipped": False}


def _github_ready(ctx) -> bool:
    from .api import github_connection_list

    try:
        return bool(github_connection_list(ctx))
    except Exception:  # noqa: BLE001 - the setup screen reports a missing connection, not internal details
        return False


def _accounts_ready(ctx, worker_names: set[str]) -> bool:
    return any(str(item.get("node") or "") in worker_names and str(item.get("status") or "") in ACCOUNT_READY
               for item in repo.account_rows(ctx.db))


def _steps(ctx, row: dict, admin_exists: bool, stored_url: str, projects: list[str]) -> dict:
    worker_names = repo.worker_node_names(ctx.db)
    # Ready from the stored Jira confirmation, never from the live status: the flow moves the sample on, and the
    # Dashboard poll must not call Jira.
    return {
        "public_address": _step(bool(stored_url or (admin_exists and ctx.settings.public_url)),
                                "Choose the address people use to open AI Flow."),
        "administrator": _step(admin_exists, "Create the first Administrator with an email and password."),
        "jira": _step(jira_connected(ctx), "Connect a Jira account in Integrations."),
        "github": _step(_github_ready(ctx), "Connect a GitHub App or an existing App in Integrations."),
        "machine": _step(bool(worker_names), "Add a machine and use its one-time join command."),
        "ai_accounts": _step(_accounts_ready(ctx, worker_names),
                             "Add an AI account to a machine and finish its sign-in."),
        "project": _step(bool(projects), "Add a Jira project and at least one repository in Projects."),
        "sample_ticket": _step(bool(row.get("sample_confirmed_at")),
                               "Choose a configured project and send a sample ticket. It must appear in Jira Intake."),
    }


def status(ctx) -> dict:
    """Check each setup step from its real source and keep its missing action in plain language."""
    row = repo.state(ctx.db)
    skipped = set(row.get("skipped_steps") or [])
    admin_exists = user_count(ctx) > 0
    stored_url = str(row.get("public_url") or "").strip()
    projects = sorted(ctx.repo().projects)
    steps = _steps(ctx, row, admin_exists, stored_url, projects)
    for step_id in STEP_IDS:
        steps[step_id]["skipped"] = step_id in skipped
    # Setup is finished only when every step is ready; a skipped step keeps the Dashboard panel and stays resumable.
    # Completion is sticky: once every step was ready it is never cleared by a later change.
    done = bool(row.get("completed_at")) or all(item["ready"] for item in steps.values())
    if done and not row.get("completed_at"):
        repo.mark_completed(ctx.db)
    sample_ready = steps["sample_ticket"]["ready"]
    return {"admin_exists": admin_exists, "public_url": stored_url or str(ctx.settings.public_url or "").strip(),
            "completed": done, "locked": admin_exists, "sample_ticket": str(row.get("sample_ticket_key") or ""),
            "sample_status": "Intake" if sample_ready else "", "projects": projects,
            "steps": [{"id": step_id, "title": STEP_LABELS[step_id], **steps[step_id],
                       "action": STEP_ACTIONS.get(step_id)} for step_id in STEP_IDS]}


def set_step_skipped(ctx, step_id: str, skipped: bool, actor: str) -> None:
    """Record a user's skip or resume choice through the setup repository."""
    if step_id not in STEP_IDS or step_id in NON_SKIPPABLE:
        raise SetupError(404, "that setup step cannot be skipped")
    repo.set_step_skipped(ctx.db, step_id, skipped, actor)


def stored_public_url(ctx) -> str:
    return repo.stored_public_url(ctx.db)


def create_first_admin(ctx, public_url: str, email: str, password_hash: str) -> None:
    """Create the first Administrator, refusing when one exists, ownership was used or the licence is full."""
    def admission(connection) -> None:
        try:
            licensing.lock_resource_admission(connection)
            licensing.require_count_capacity(ctx.db, "users", repo.active_user_count(connection),
                                             public_key=ctx.settings.license_public_key, connection=connection)
        except licensing.LicenseError as exc:
            raise SetupError(423, str(exc)) from None

    try:
        repo.create_first_admin(ctx.db, public_url, email, password_hash, admission)
    except repo.FirstAdminRefused as exc:
        if exc.reason == "exists":
            raise SetupError(409, "An Administrator already exists. Sign in to continue.") from None
        raise SetupError(409, "The installation ownership capability was already used.") from None


def normalize_bootstrap(public_url: str, email: str) -> tuple[str, str]:
    try:
        return validate_public_url(public_url), normalize_email(email)
    except ValueError as exc:
        raise SetupError(422, str(exc)) from None


def _sample_issue_key(connection, ctx, body: SampleBody, actor: str) -> tuple[str, str, Exception | None]:
    row = repo.lock_sample_state(connection)
    key = str(row.get("sample_ticket_key") or "")
    if row.get("completed_at"):
        raise SetupError(409, "Setup is complete. Follow ticket progress on the normal ticket and Dashboard screens.")
    if row.get("sample_confirmed_at"):
        raise SetupError(409, f"Sample ticket {key} was already sent to Intake. Follow it on the ticket screens.")
    if not jira_connected(ctx) or not ctx.jira:
        raise SetupError(409, "Connect Jira in Integrations before sending the sample ticket.")
    project = body.project.strip().upper()
    if project not in ctx.repo().projects:
        raise SetupError(422, "Choose a project already configured in Projects.")
    if key:
        return key, project, None
    try:
        key = ctx.jira.create_issue(project, repo.SAMPLE_SUMMARY, repo.SAMPLE_BODY + "\n\nThis confirms that Jira is "
                                    "connected and this project can receive work in Intake.",
                                    issuetype="Task", labels=["aiflow-setup-sample"])
        repo.store_sample_key(connection, key)
        return key, project, None
    except Exception as exc:  # noqa: BLE001 - return a plain recovery action, never provider internals
        return "", project, exc


def _confirm_sample_issue(connection, ctx, actor: str, key: str, project: str) -> Exception | None:
    try:
        current = ctx.jira.status(key)
        if current != "Intake":
            current = ctx.jira.transition_to(key, "Intake")
        if current != "Intake":
            raise RuntimeError("Jira did not confirm Intake")
    except Exception as exc:  # noqa: BLE001
        repo.audit_sample(connection, actor, False, "sample issue not confirmed in Intake", project, key)
        return exc
    repo.record_sample_confirmed(connection, key, project, actor)
    return None


def send_sample_ticket(ctx, body: SampleBody, actor: str) -> dict:
    """Create (once) and confirm the sample issue in Intake while holding the installation-wide sample lock."""
    issue_error = confirmation_error = None
    with repo.sample_ticket_lock(ctx.db) as connection:
        with connection.transaction():
            key, project, issue_error = _sample_issue_key(connection, ctx, body, actor)
        if issue_error:
            with connection.transaction():
                repo.audit_sample(connection, actor, False, "could not create sample issue", project)
        else:
            with connection.transaction():
                if repo.lock_sample_state(connection).get("sample_confirmed_at"):
                    raise SetupError(409, f"Sample ticket {key} was already sent to Intake. "
                                          "Follow it on the ticket screens.")
                confirmation_error = _confirm_sample_issue(connection, ctx, actor, key, project)
    if issue_error:
        raise SetupError(502, "AI Flow could not create the sample ticket in Jira. "
                              "Check the connection and try again.") from issue_error
    if confirmation_error:
        raise SetupError(502, f"Ticket {key} exists, but AI Flow could not confirm it in Intake. "
                              "Check the Jira workflow and try again.") from confirmation_error
    completed = status(ctx)["completed"]
    ctx.events.emit("setup.sample_ticket", ticket=key, project=project, outcome="Intake",
                    account=actor, reason="first-run setup sample confirmed")
    tail = ("Setup is complete; follow its progress on the ticket and Dashboard screens." if completed
            else "Skipped or unfinished steps stay on the Dashboard until you finish them.")
    return {"ok": True, "ticket": key, "status": "Intake", "completed": completed,
            "message": f"Sample ticket {key} was created in Intake. {tail}"}
