"""Routes of the projects feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
import html
import json
import httpx
from pathlib import Path
from types import SimpleNamespace

from .bodies import EnvironmentRemovalBody, GitHubConnectionTokenBody, GitHubInstallationBody, ProjectEnvironmentBody, ProjectPauseBody, ProjectPreflightBody, ProjectRepositoriesBody, ProjectServicesBody
from .events import (EnvironmentRemovalsRecorded, GitHubAppConnected, GitHubAppImportVerified,
                     GitHubAppInstallOpened, GitHubConnectionAssigned, GitHubConnectionRemoved,
                     GitHubInstallationSettingsOpened, GitHubManifestCreated, GitHubManifestStarted,
                     GitHubRepositoryAccessChanged, GitHubRepositorySnapshotRecorded,
                     GitHubTokenConnectionAdded, GitHubTokenConnectionReplaced, ProjectAdded,
                     ProjectEnvironmentSaved, ProjectEnvironmentScanned, ProjectPauseChanged,
                     ProjectRemoved, ProjectRepositoriesSaved, ProjectServicesChanged)
from typing import Any
from urllib.parse import quote, urlsplit
import yaml
from fastapi import Depends, HTTPException, Request
from fastapi.responses import (HTMLResponse, RedirectResponse)
from ....core import work_limits
from ....core.services import normalize_services
from ....core.toolchains import (CONTAINER_RUNTIME_TOOL, STACK_TOOLS, missing_tools, toolchain_gaps,
                               tools_for_stack)
from ....core.environment import KINDS as ENV_KINDS, RUN_STEPS as ENV_RUN_STEPS, parse_items as parse_env_items
from ... import (licensing, system_check, system_check_api, tooling)
from ... import environment as environment_reports
from ...platform.events.wiring import publish_for
from ...github import GitHubApp
from ...github_connect import GitHubSecretStore, app_metadata
from ...github_connections import DEFAULT_ID, Connection, verify_token
from ... import inventory


def install(app, ctx, deps) -> SimpleNamespace:
    from ...api import ProjectCreateBody, log  # noqa: PLC0415 - api imports this module
    from ...gateway_helpers import declare_project_services, github_connection_list, github_connections, github_control_gateway, github_gateway  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    console_user = deps.console_user
    fenced = deps.fenced
    node_auth = deps.node_auth
    optional_console_user = deps.optional_console_user
    require_capacity = deps.require_capacity
    # --- Owner-managed GitHub App connection ----------------------------------------------------------------
    def github_secret_store() -> GitHubSecretStore:
        return GitHubSecretStore(ctx.settings.github_app_dir)

    def audit_github_repository_changes(gateway: GitHubApp, repositories: list[dict[str, Any]], actor: str,
                                       source: str) -> None:
        store = github_secret_store()
        if not hasattr(gateway, "meta") or not store.active_exists:
            return
        names = sorted({str(item.get("full_name") or "") for item in repositories if item.get("full_name")})
        previous = store.update_repository_snapshot(names)
        if hasattr(gateway, "_refresh_config"):
            gateway._refresh_config()
        if previous is None:
            publish_for(ctx, GitHubRepositorySnapshotRecorded(actor=actor, count=len(names), source=source))
            return
        old_by_key = {name.lower(): name for name in previous}
        new_by_key = {name.lower(): name for name in names}
        added = sorted(new_by_key[key] for key in new_by_key.keys() - old_by_key.keys())
        removed = sorted(old_by_key[key] for key in old_by_key.keys() - new_by_key.keys())
        if added or removed:
            publish_for(ctx, GitHubRepositoryAccessChanged(actor=actor, added=added, removed=removed, source=source))
            ctx.events.emit("github.repositories_changed", account=getattr(gateway, "org", ""),
                            outcome="updated", reason=source, data={"added": added, "removed": removed})

    def github_install_url(slug: str) -> str:
        return f"https://github.com/apps/{quote(slug, safe='-')}/installations/new"

    def github_installation_settings(account_login: str, account_type: str, installation_id: int) -> str:
        if account_type.lower() == "organization":
            return (f"https://github.com/organizations/{quote(account_login, safe='-')}/settings/"
                    f"installations/{installation_id}")
        return f"https://github.com/settings/installations/{installation_id}"

    def github_public_base() -> str:
        value = str(ctx.settings.public_url or "").rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise HTTPException(503, "set AIFLOW4_PUBLIC_URL to the HTTPS URL owners use to open AI Flow")
        return value

    def pending_gateway(store: GitHubSecretStore, pending_id: str, meta: dict[str, Any]) -> GitHubApp:
        directory = store._pending_dir(pending_id)
        return GitHubApp(str(directory), str(meta.get("account_login") or ctx.settings.github_org))

    def complete_github_connection(store: GitHubSecretStore, pending_id: str, installation_id: int,
                                   actor: str, source: str) -> dict[str, Any]:
        record, meta, private_key = store.read_pending(pending_id)
        if not private_key or record.get("phase") not in {"manifest_setup", "import"}:
            raise ValueError("GitHub App setup is incomplete")
        gateway = pending_gateway(store, pending_id, meta)
        system_check_api.validate_github_app_permissions(gateway.app_details())
        installation = gateway.installation_details(installation_id)
        if int(installation.get("id") or 0) != installation_id:
            raise ValueError("GitHub installation could not be verified")
        account = installation.get("account") or {}
        account_login = str(account.get("login") or "")
        account_type = str(account.get("type") or "")
        if not account_login:
            raise ValueError("GitHub installation account is missing")
        expected = str(record.get("expected_owner") or "")
        if expected and account_login.lower() != expected.lower():
            raise ValueError("GitHub installation account does not match the selected organization")
        if installation.get("suspended_at"):
            raise ValueError("GitHub App installation is suspended")

        # Validate the installation token and selected repository list before activating the key.
        active_meta = app_metadata(meta, account_login=account_login, account_type=account_type,
                                   installation_id=installation_id)
        store.update_pending(pending_id, record, active_meta, private_key)
        gateway = pending_gateway(store, pending_id, active_meta)
        repositories = gateway.installation_repositories()
        active_meta["repository_names"] = sorted({str(item["full_name"]) for item in repositories
                                                  if item.get("full_name")})
        target = str(record.get("connection") or "default")
        if target == "default" or (target == "new" and not store.active_exists):
            connection_id = DEFAULT_ID
            store.save_active(active_meta, private_key)
            ctx.github = GitHubApp(ctx.settings.github_app_dir, account_login)
            ctx.settings.github_org = account_login
        else:
            connection_id = github_connections(ctx).save_app(None if target == "new" else target, "",
                                                             active_meta, private_key)
        store.delete_pending(pending_id)
        publish_for(ctx, GitHubAppConnected(actor=actor, app_id=str(active_meta.get("id") or ""),
                                            installation_id=installation_id, account=account_login,
                                            repositories=len(repositories), source=source, connection=connection_id))
        ctx.events.emit("github.connected", account=account_login, outcome="connected", reason=source)
        return {"account": account_login, "account_type": account_type, "installation_id": installation_id,
                "connection": connection_id,
                "repositories": len(repositories), "settings_url": github_installation_settings(
                    account_login, account_type, installation_id)}

    @app.get("/v1/integrations/github")
    def github_connection_status(request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"})
        gateway = github_gateway(ctx)
        if not gateway:
            return {"connected": False, "app": None, "account": None, "installation_id": None,
                    "repository_count": 0, "settings_url": ""}
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
        account_type = str(meta.get("account_type") or "Organization")
        try:
            repositories = gateway.installation_repositories()
            audit_github_repository_changes(gateway, repositories, user.subject, "integration_refresh")
        except Exception as exc:  # provider response may contain credentials; return only a generic status
            log.warning("GitHub installation status failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load GitHub installation status") from exc
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        return {"connected": True,
                "app": {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                        "slug": str(meta.get("slug") or "")},
                "account": {"login": account, "type": account_type},
                "installation_id": int(installation_id) if installation_id else None,
                "repository_count": len(repositories),
                "settings_url": github_installation_settings(account, account_type, int(installation_id))
                if installation_id else ""}

    def target_connection(value: Any) -> str:
        """Where a GitHub App being connected goes: the default one, a new connection, or an existing id."""
        text = str(value or "default")
        if text in ("default", "new") or (text != DEFAULT_ID and github_connections(ctx).meta(text)):
            return text
        raise HTTPException(422, "unknown GitHub connection")

    def connection_view(conn: Connection) -> dict[str, Any]:
        """One row of the Integrations list: account, credential type, status and reachable repositories."""
        meta = getattr(conn.gateway, "meta", {}) or {}
        view: dict[str, Any] = {**conn.summary(), "status": "connected", "repositories": [], "warning": "",
                                "app": None, "settings_url": ""}
        if conn.kind == "token":
            view["warning"] = ("An access token reaches more than a GitHub App, has no bot identity and cannot "
                               "be limited per run: it stays in Command Center and is never given to a node. "
                               "Prefer a GitHub App.")
        else:
            view["app"] = {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                           "slug": str(meta.get("slug") or "")}
            installation = meta.get("installation_id") or getattr(conn.gateway, "_inst", None)
            if installation and conn.account:
                view["settings_url"] = github_installation_settings(conn.account, conn.account_type or "Organization",
                                                                    int(installation))
        try:
            view["repositories"] = sorted(
                str(r.get("full_name")) for r in conn.gateway.installation_repositories()
                if str(r.get("full_name") or "").split("/", 1)[0].lower() == conn.account.lower())
        except Exception as exc:  # provider text may carry credentials; show only a generic status
            log.warning("GitHub connection %s status failed (%s)", conn.id, type(exc).__name__)
            view["status"] = "error"
        return view

    def find_connection(connection_id: str) -> Connection:
        conn = next((c for c in github_connection_list(ctx) if c.id == connection_id), None)
        if conn is None:
            raise HTTPException(404, "unknown GitHub connection")
        return conn

    @app.get("/v1/integrations/github/connections")
    def github_connections_status(request: Request) -> dict[str, Any]:
        console_user(request, {"integrations.manage"})
        return {"connections": [connection_view(c) for c in github_connection_list(ctx)],
                "recommendation": "Use a GitHub App; an access token is broader, has no bot identity and cannot "
                                  "be limited to one repository per run."}

    @app.post("/v1/integrations/github/connections/token")
    def github_add_token(body: GitHubConnectionTokenBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="add_token_connection")
        account = body.account.strip()
        if account and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", account):
            audit("github", "add_token_connection", "", "", None, user.subject, False, "invalid account")
            raise HTTPException(422, "enter a valid GitHub account or organization login")
        try:
            gateway = verify_token(body.token.strip(), account)
        except Exception as exc:
            log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "add_token_connection", "", "", None, user.subject, False, "token could not be verified")
            raise HTTPException(422, "GitHub did not accept this token for that account") from exc
        label = body.label.strip() or f"{gateway.org} access token"
        connection_id = github_connections(ctx).add_token(label, body.token.strip(), gateway.org,
                                                           gateway.meta.get("account_type", ""))
        publish_for(ctx, GitHubTokenConnectionAdded(actor=user.subject, connection=connection_id,
                                                    account=gateway.org))
        ctx.events.emit("github.connected", account=gateway.org, outcome="connected", reason="token")
        return connection_view(find_connection(connection_id))

    @app.post("/v1/integrations/github/connections/{connection_id}/test")
    def github_test_connection(connection_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="test_connection")
        view = connection_view(find_connection(connection_id))
        audit("github", "test_connection", "", "", None, user.subject, view["status"] == "connected",
              view["status"], {"connection": connection_id, "repositories": len(view["repositories"])})
        return view

    @app.put("/v1/integrations/github/connections/{connection_id}/token")
    def github_replace_token(connection_id: str, body: GitHubConnectionTokenBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="replace_token_connection")
        conn = find_connection(connection_id)
        if conn.kind != "token":
            raise HTTPException(409, "only an access token connection takes a new token; reconnect the App instead")
        try:
            gateway = verify_token(body.token.strip(), conn.account)
        except Exception as exc:
            log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "replace_token_connection", "", "", None, user.subject, False,
                  "token could not be verified", {"connection": connection_id})
            raise HTTPException(422, "GitHub did not accept this token for that account") from exc
        github_connections(ctx).replace_token(connection_id, body.token.strip(), gateway.org,
                                              gateway.meta.get("account_type", ""))
        publish_for(ctx, GitHubTokenConnectionReplaced(actor=user.subject, connection=connection_id,
                                                       account=gateway.org))
        return connection_view(find_connection(connection_id))

    @app.delete("/v1/integrations/github/connections/{connection_id}")
    def github_remove_connection(connection_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="remove_connection")
        find_connection(connection_id)
        registry = github_connections(ctx)
        using = sorted(repo for repo, assigned in registry.assignments().items() if assigned == connection_id)
        if using:
            audit("github", "remove_connection", "", "", None, user.subject, False,
                  "connection is assigned to repositories", {"connection": connection_id, "repositories": using})
            raise HTTPException(409, "reassign these repositories first: " + ", ".join(using))
        if connection_id == DEFAULT_ID:
            for name in ("app.json", "app.pem"):
                (Path(ctx.settings.github_app_dir) / name).unlink(missing_ok=True)
            ctx.github = None
        else:
            registry.remove(connection_id)
        publish_for(ctx, GitHubConnectionRemoved(actor=user.subject, connection=connection_id))
        return {"removed": connection_id}

    @app.get("/v1/integrations/github/manifest/start", response_class=HTMLResponse)
    def github_manifest_start(organization: str, request: Request, connection: str = "default") -> HTMLResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="manifest_start")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", organization):
            audit("github", "manifest_start", "", "", None, user.subject, False, "invalid GitHub organization")
            raise HTTPException(422, "enter a valid GitHub organization login")
        base = github_public_base()
        store = github_secret_store()
        state = store.new_id()
        store.create_pending(state, {"phase": "manifest", "actor": user.subject, "connection": target_connection(connection),
                                     "expected_owner": organization, "source": "manifest"})
        publish_for(ctx, GitHubManifestStarted(actor=user.subject, organization=organization))
        callback = base + "/v1/integrations/github/manifest/callback"
        setup = callback.removesuffix("/manifest/callback") + "/setup?state=" + state
        manifest = {"name": f"AI Flow {organization}", "url": base + "/cc", "description": "AI Flow GitHub integration",
                    "redirect_url": callback, "setup_url": setup, "setup_on_update": False, "public": False,
                    "default_events": [],
                    "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read",
                                            "actions": "read",
                                            "checks": "read", "statuses": "read", "workflows": "write",
                                            "administration": "write"}}
        action = f"https://github.com/organizations/{quote(organization, safe='-')}/settings/apps/new?state={state}"
        encoded = html.escape(json.dumps(manifest, separators=(",", ":")), quote=True)
        action_attr = html.escape(action, quote=True)
        body = ("<!doctype html><html><head><meta charset='utf-8'><title>Connect GitHub</title></head><body>"
                "<p>Continue to GitHub to create the AI Flow App.</p>"
                f"<form id='manifest' action='{action_attr}' method='post'>"
                f"<input type='hidden' name='manifest' value='{encoded}'>"
                "<button type='submit'>Continue to GitHub</button></form>"
                "<script>document.getElementById('manifest').submit()</script></body></html>")
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/v1/integrations/github/manifest/callback")
    def github_manifest_callback(code: str, state: str) -> RedirectResponse:
        store = github_secret_store()
        try:
            record, _, _ = store.read_pending(state)
            if record.get("phase") != "manifest":
                raise ValueError("GitHub manifest state is invalid")
            if not re.fullmatch(r"[A-Za-z0-9]+", code):
                raise ValueError("GitHub manifest response is invalid")
            with httpx.Client(base_url="https://api.github.com", timeout=30,
                              headers={"Accept": "application/vnd.github+json",
                                       "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aiflow4"}) as client:
                response = client.post(f"/app-manifests/{quote(code, safe='')}/conversions")
            if response.status_code not in (200, 201):
                raise ValueError("GitHub manifest could not be completed")
            converted = response.json()
            pem = converted.get("pem")
            if not converted.get("id") or not converted.get("slug") or not isinstance(pem, str) or not pem.startswith("-----BEGIN "):
                raise ValueError("GitHub manifest response is incomplete")
            app_owner = converted.get("owner") or {}
            if (str(app_owner.get("login") or "").lower() != str(record.get("expected_owner") or "").lower()
                    or str(app_owner.get("type") or "").lower() != "organization"):
                raise ValueError("GitHub App owner does not match the selected organization")
            record["phase"] = "manifest_setup"
            meta = app_metadata(converted)
            store.update_pending(state, record, meta, pem)
            actor = str(record.get("actor") or "github-manifest")
            publish_for(ctx, GitHubManifestCreated(actor=actor, app_id=str(meta.get("id") or ""),
                                                   organization=record.get("expected_owner", "")))
            return RedirectResponse(github_install_url(str(meta["slug"])), status_code=303)
        except Exception as exc:  # no provider response or callback credential is copied into logs
            log.warning("GitHub manifest callback failed (%s)", type(exc).__name__)
            actor = "github-manifest"
            try:
                record, _, _ = store.read_pending(state)
                actor = str(record.get("actor") or actor)
                store.delete_pending(state)
            except Exception:
                pass
            audit("github", "manifest_created", "", "", None, actor, False, "GitHub manifest setup failed")
            return RedirectResponse("/integrations?github=error", status_code=303)

    @app.get("/v1/integrations/github/setup")
    def github_install_setup(installation_id: int, state: str, setup_action: str = "install") -> RedirectResponse:
        store = github_secret_store()
        actor = "github-setup"
        try:
            record, _, _ = store.read_pending(state)
            actor = str(record.get("actor") or actor)
            if record.get("phase") != "manifest_setup":
                raise ValueError("GitHub setup state is invalid")
            complete_github_connection(store, state, installation_id, actor, "manifest")
            return RedirectResponse("/integrations?github=connected", status_code=303)
        except Exception as exc:
            log.warning("GitHub installation callback failed (%s)", type(exc).__name__)
            try:
                store.delete_pending(state)
            except Exception:
                pass
            audit("github", "connect", "", "", None, actor, False, "GitHub installation could not be verified",
                  {"installation_id": installation_id, "source": "manifest", "setup_action": setup_action})
            return RedirectResponse("/integrations?github=error", status_code=303)

    @app.post("/v1/integrations/github/imports")
    async def github_import_app(request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="import_app")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 25_000:
                audit("github", "import_app", "", "", None, user.subject, False, "private key upload too large")
                raise HTTPException(413, "private key file is too large")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid App import request")
            raise HTTPException(422, "select a GitHub App ID and private key file")
        if not isinstance(payload, dict):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid App import request")
            raise HTTPException(422, "select a GitHub App ID and private key file")
        app_id, pem = payload.get("app_id"), payload.get("private_key")
        if type(app_id) is not int or app_id < 1:
            audit("github", "import_app", "", "", None, user.subject, False, "invalid GitHub App id")
            raise HTTPException(422, "enter a valid GitHub App ID")
        if not isinstance(pem, str) or (len(pem) < 100 or len(pem) > 20_000 or "\x00" in pem or not pem.startswith("-----BEGIN ")
                or "PRIVATE KEY-----" not in pem or not pem.rstrip().endswith("-----END PRIVATE KEY-----")
                and not pem.rstrip().endswith("-----END RSA PRIVATE KEY-----")):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid private key file",
                  {"app_id": app_id})
            raise HTTPException(422, "select a valid GitHub App private key file")
        store = github_secret_store()
        pending_id = store.new_id()
        record = {"phase": "import", "actor": user.subject, "source": "import", "app": {"id": app_id},
                  "connection": target_connection(payload.get("connection"))}
        store.create_pending(pending_id, record, pem)
        try:
            _, meta, _ = store.read_pending(pending_id)
            gateway = pending_gateway(store, pending_id, meta)
            details = gateway.app_details()
            if int(details.get("id") or 0) != app_id:
                raise ValueError("GitHub App id did not match")
            system_check_api.validate_github_app_permissions(details)
            if not details.get("slug"):
                raise ValueError("GitHub App slug is missing")
            record["app"] = app_metadata(details)
            store.update_pending(pending_id, record, app_metadata(details), pem)
            publish_for(ctx, GitHubAppImportVerified(actor=user.subject, app_id=app_id))
            return {"pending_id": pending_id,
                    "app": {"id": str(details.get("id") or ""), "name": str(details.get("name") or "GitHub App"),
                            "slug": str(details.get("slug") or "")},
                    "install_url": github_install_url(str(details.get("slug") or ""))}
        except Exception as exc:
            store.delete_pending(pending_id)
            log.warning("GitHub App import could not be verified (%s)", type(exc).__name__)
            audit("github", "import_app", "", "", None, user.subject, False, "GitHub App credentials could not be verified",
                  {"app_id": app_id})
            raise HTTPException(422, "could not verify this GitHub App and private key") from exc

    @app.get("/v1/integrations/github/imports/{pending_id}/installations")
    def github_import_installations(pending_id: str, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="list_import_installations")
        store = github_secret_store()
        stage = "load pending App"
        try:
            record, meta, _ = store.read_pending(pending_id)
            if record.get("phase") != "import":
                raise ValueError("GitHub App import state is invalid")
            stage = "query GitHub installations"
            gateway = pending_gateway(store, pending_id, meta)
            installations = gateway.app_installations()
            stage = "format GitHub installations"
            accounts = []
            for item in installations:
                account = item.get("account") or {}
                if account.get("login"):
                    accounts.append({"id": int(item["id"]), "login": str(account["login"]),
                                     "type": str(account.get("type") or ""),
                                     "repository_selection": str(item.get("repository_selection") or "selected")})
            return {"app": {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                            "slug": str(meta.get("slug") or "")}, "installations": accounts,
                    "install_url": github_install_url(str(meta.get("slug") or ""))}
        except Exception as exc:
            log.warning("GitHub App installations could not be listed (%s, stage=%s)", type(exc).__name__, stage)
            raise HTTPException(502, "could not load installations for this GitHub App") from exc

    @app.get("/v1/integrations/github/imports/{pending_id}/install")
    def github_import_install(pending_id: str, request: Request) -> RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_app_install")
        try:
            record, meta, _ = github_secret_store().read_pending(pending_id)
            if record.get("phase") != "import" or not meta.get("slug"):
                raise ValueError("GitHub App import state is invalid")
        except Exception as exc:
            audit("github", "open_app_install", "", "", None, user.subject, False, "GitHub App import expired")
            raise HTTPException(404, "GitHub App import expired; start again") from exc
        publish_for(ctx, GitHubAppInstallOpened(actor=user.subject, app_id=str(meta.get("id") or "")))
        return RedirectResponse(github_install_url(str(meta["slug"])), status_code=303)

    @app.post("/v1/integrations/github/imports/{pending_id}/connect")
    def github_import_connect(pending_id: str, body: GitHubInstallationBody, request: Request) -> dict[str, Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="connect_imported_app")
        try:
            result = complete_github_connection(github_secret_store(), pending_id, body.installation_id,
                                               user.subject, "import")
            return {"connected": True, **result}
        except Exception as exc:
            log.warning("GitHub imported App connection failed (%s)", type(exc).__name__)
            audit("github", "connect", "", "", None, user.subject, False,
                  "GitHub installation could not be verified",
                  {"installation_id": body.installation_id, "source": "import"})
            raise HTTPException(422, "could not connect this GitHub installation") from exc

    @app.get("/v1/integrations/github/installation-settings")
    def github_open_installation_settings(request: Request) -> RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_installation_settings")
        gateway = github_gateway(ctx)
        if not gateway:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub App is not connected")
            raise HTTPException(503, "GitHub App is not connected")
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        if not installation_id:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub installation is not configured")
            raise HTTPException(503, "GitHub installation is not configured")
        account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
        account_type = str(meta.get("account_type") or "Organization")
        url = github_installation_settings(account, account_type, int(installation_id))
        publish_for(ctx, GitHubInstallationSettingsOpened(actor=user.subject,
                                                          app_id=str(meta.get("id") or ""),
                                                          installation_id=int(installation_id), account=account))
        return RedirectResponse(url, status_code=303)

    # --- Projects screen: owner-managed project definitions in aiflow-control -------------------------------
    required_project_statuses = list(system_check.REQUIRED_PROJECT_STATUSES)

    def jira_site() -> str:
        return str(getattr(ctx.jira, "site", "") or ctx.settings.jira_site).rstrip("/")

    def project_key_from_input(value: str, site: str) -> str:
        """Accept a Jira project key or a URL for the selected connected site."""
        raw = value.strip()
        if raw.startswith(("http://", "https://")):
            parsed = urlsplit(raw)
            selected = urlsplit(site)
            if not parsed.hostname or parsed.hostname.lower() != (selected.hostname or "").lower():
                raise HTTPException(422, "Jira project URL must belong to the selected connected site")
            parts = [part for part in parsed.path.split("/") if part]
            candidate = ""
            for index, part in enumerate(parts[:-1]):
                if part in {"projects", "browse"}:
                    candidate = parts[index + 1]
                    break
            if not candidate:
                raise HTTPException(422, "Jira project URL does not contain a project key")
            raw = candidate.split("-", 1)[0]
        key = raw.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key):
            raise HTTPException(422, "Jira project must be a project key or project URL")
        return key

    def github_catalog(actor: str, source: str) -> list[dict[str, Any]]:
        """Repositories of every connection, each with the connections that reach it and the one it uses."""
        connections = github_connection_list(ctx)
        assigned = github_connections(ctx).assignments()
        repos: dict[str, dict[str, Any]] = {}
        failures: list[Exception] = []
        for conn in connections:
            try:
                rows = conn.gateway.installation_repositories()
                if conn.id == DEFAULT_ID:
                    audit_github_repository_changes(conn.gateway, rows, actor, source)
            except Exception as exc:  # one broken connection must not hide the others
                log.warning("GitHub connection %s repositories failed (%s)", conn.id, type(exc).__name__)
                failures.append(exc)
                continue
            for item in rows:
                name = str(item.get("full_name") or "")
                if name.split("/", 1)[0].lower() != conn.account.lower():
                    continue
                repos.setdefault(name.lower(), {**item, "connections": []})["connections"].append(conn.id)
        if connections and len(failures) == len(connections):
            raise failures[0]
        for key, item in repos.items():
            assigned_connection = assigned.get(key, "")
            item["assigned_connection"] = assigned_connection
            item["connection"] = assigned_connection if assigned_connection in item["connections"] else (
                "" if assigned_connection else item["connections"][0])
        return sorted(repos.values(), key=lambda row: str(row["full_name"]).lower())

    def connection_summaries() -> list[dict[str, str]]:
        return [conn.summary() for conn in github_connection_list(ctx)]

    def project_preflight(body: ProjectPreflightBody, actor: str = "owner") -> dict[str, Any]:
        if not ctx.jira:
            raise HTTPException(503, "Jira is not connected")
        if not github_connection_list(ctx):
            raise HTTPException(503, "GitHub is not connected")
        site = jira_site()
        if body.site.rstrip("/") != site:
            raise HTTPException(422, "selected Jira site is not connected")
        key = project_key_from_input(body.project, site)
        if body.stack and body.stack not in STACK_TOOLS:
            raise HTTPException(422, "unsupported project stack")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", body.base) or ".." in body.base:
            raise HTTPException(422, "invalid base branch")
        org, slash, repo_name = body.repo.partition("/")
        if not slash or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo_name) or not re.fullmatch(r"[A-Za-z0-9_.-]+", org):
            raise HTTPException(422, "select a repository accessible to a connected GitHub account")

        try:
            jira_statuses = ctx.jira.project_statuses(key)
            catalog = github_catalog(actor, "project_preflight")
            entry = next((item for item in catalog if str(item["full_name"]).lower() == body.repo.lower()), None)
            if entry is None or not entry["connection"]:
                raise HTTPException(422, "repository is not accessible to any connected GitHub account")
            github = github_gateway(ctx, body.repo)
            branch_exists = bool(github.branch_exists(repo_name, body.base))
        except HTTPException:
            raise
        except Exception as exc:  # provider details can contain request metadata; keep them out of the UI
            log.warning("project preflight failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not complete Jira and GitHub project checks") from exc

        missing_statuses = [status for status in required_project_statuses if status not in jira_statuses]
        repo = ctx.repo()
        node_rows = {row["name"]: row for row in ctx.db.all("SELECT name,status,tools FROM nodes")}
        node_names = sorted(set(repo.nodes) | set(node_rows))
        required_tools = tools_for_stack(body.stack)
        if body.containers and CONTAINER_RUNTIME_TOOL not in required_tools:
            required_tools.append(CONTAINER_RUNTIME_TOOL)
        node_results = []
        for name in node_names:
            row = node_rows.get(name) or {}
            tools = row.get("tools") or {}
            gaps = toolchain_gaps(tools, required_tools, repo.versions, [body.stack] if body.stack else (),
                                  require_pins=bool(body.stack))
            absent = missing_tools(tools, required_tools)
            gaps = list(dict.fromkeys(absent + gaps))
            node_results.append({"name": name, "status": row.get("status", "unknown"),
                                 "ready": not gaps, "missing_tools": gaps})
        return {"project": key, "site": site, "missing_statuses": missing_statuses,
                "branch_exists": branch_exists, "nodes": node_results, "required_tools": required_tools,
                "ok": not missing_statuses and branch_exists}

    def refresh_projects() -> bool:
        if not ctx.refresh_repo:
            return True
        if ctx.refresh_repo:
            try:
                ctx.refresh_repo()
            except Exception as exc:  # RepoCache will retry its pull on a later refresh
                log.warning("project control repo refresh failed (%s)", type(exc).__name__)
                return False
        return True

    def project_audit(action: str, key: str, actor: str, accepted: bool, result: str,
                      payload: dict[str, Any] | None = None) -> None:
        audit("aiflow-control", action, key, "", None, actor, accepted, result,
              payload or {"project": key})

    def ticket_epic(ticket: dict[str, Any]) -> str:
        """Resolve a ticket's parent chain to its Jira epic key."""
        if ticket.get("type") == "Epic":
            return str(ticket.get("key") or "")
        parent = str(ticket.get("parent_key") or "")
        seen: set[str] = set()
        while parent and parent not in seen:
            seen.add(parent)
            ancestor = ctx.db.one("SELECT key,type,parent_key FROM tickets WHERE key=%s", (parent,))
            if not ancestor:
                return parent
            if ancestor["type"] == "Epic":
                return parent
            parent = str(ancestor.get("parent_key") or "")
        return ""

    @app.get("/v1/projects")
    def managed_projects(request: Request) -> dict:
        user = (console_user(request, "projects.manage") if ctx.settings.console_login
                else optional_console_user(request))
        repo = ctx.repo()
        readiness_by_project = system_check_api.projects_readiness(ctx, repo)
        counts = {row["project"]: int(row["count"]) for row in
                  ctx.db.all("SELECT project,count(*) AS count FROM tickets GROUP BY project")}
        probes = tooling.probe_report(ctx)
        projects = [{"key": project.key, "repo": project.repo,
                              "repositories": project.configured_repositories,
                              "status": "paused" if project.paused else "active",
                              "priority": project.priority, "ticket_count": counts.get(project.key, 0),
                              "base": project.base, "stack": project.stack,
                              "node_readiness": readiness_by_project.get(project.key, []),
                              "services": project.required_services,
                              "checks": probes.get(project.key, {})}
                    for project in sorted(repo.projects.values(), key=lambda item: item.key)]
        if user and user.project_keys is not None:
            projects = [project for project in projects if project["key"] in user.project_keys]
        invalid_projects = [{"file": f"{name}.yaml", "reason": reason}
                            for name, reason in sorted(repo.invalid_projects.items())
                            if not user or user.project_keys is None or name in user.project_keys]
        return {"projects": projects, "invalid_projects": invalid_projects}

    @app.get("/v1/projects/catalog")
    def project_catalog(request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_catalog")
        if not ctx.jira or not github_connection_list(ctx):
            raise HTTPException(503, "Jira and GitHub must be connected before adding a project")
        try:
            jira_projects = ctx.jira.projects()
            github_repos = github_catalog(user.subject, "project_catalog")
        except Exception as exc:
            log.warning("project catalog could not be loaded (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load connected Jira projects and GitHub repositories") from exc
        return {"jira_sites": [jira_site()], "jira_projects": jira_projects,
                "github_repos": github_repos, "connections": connection_summaries(),
                "stacks": sorted(STACK_TOOLS), "required_statuses": required_project_statuses}

    def jira_project_epics(key: str) -> list[dict[str, str]]:
        """Read the current Jira epics for the project instead of relying on the periodic ticket mirror."""
        issues = ctx.jira.search(f'project = "{key}" AND issuetype = Epic ORDER BY key')
        out = []
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_type = (fields.get("issuetype") or {}).get("name") or ""
            if issue_type and issue_type.lower() != "epic":
                continue
            issue_key = str(issue.get("key") or "")
            if issue_key:
                out.append({"key": issue_key, "summary": str(fields.get("summary") or issue_key),
                            "status": str((fields.get("status") or {}).get("name") or "")})
        return sorted(out, key=lambda item: (int(re.search(r"(\d+)$", item["key"]).group(1))
                                             if re.search(r"(\d+)$", item["key"]) else 0, item["key"]))

    def repository_settings_payload(project) -> list[dict[str, Any]]:
        configured = {str(item.get("repo") or "").lower(): item for item in project.repositories
                      if isinstance(item, dict) and item.get("repo")}
        names = project.configured_repositories
        return [{"repo": name, "base": str(configured.get(name.lower(), {}).get("base") or ""),
                 "checks": list(configured.get(name.lower(), {}).get("checks") or []),
                 "stack": str(configured.get(name.lower(), {}).get("stack") or ""),
                 "containers": configured.get(name.lower(), {}).get("containers"),
                 "effective": project.settings_for_repo(name)} for name in names]

    @app.get("/v1/projects/{key}/repositories")
    def project_repositories(key: str, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, "project is not configured")
        if not ctx.jira or not github_connection_list(ctx):
            raise HTTPException(503, "Jira and GitHub must be connected to configure repositories")
        try:
            epics = jira_project_epics(key)
            available = github_catalog(user.subject, "project_repositories")
        except Exception as exc:
            log.warning("project repositories could not be loaded (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        by_name = {str(item["full_name"]).lower(): item for item in available}
        assignments = github_connections(ctx).assignments()
        repositories = []
        for row in repository_settings_payload(project):
            catalog_row = by_name.get(row["repo"].lower()) or {}
            reachable = catalog_row.get("connections", [])
            assigned_connection = assignments.get(row["repo"].lower(), catalog_row.get("assigned_connection", ""))
            stale_assignment = bool(assigned_connection and assigned_connection not in reachable)
            selected_connection = catalog_row.get("connection", "")
            if stale_assignment:
                selected_connection = reachable[0] if reachable else ""
            repositories.append({**row, "connection": selected_connection,
                                 "assigned_connection": assigned_connection,
                                 "connection_stale": stale_assignment,
                                 "connections": reachable})
        return {"project": key, "default_repo": project.repo, "connections": connection_summaries(),
                "project_defaults": {"base": project.base, "checks": project.checks, "stack": project.stack,
                                     "containers": project.containers},
                "repositories": repositories,
                "epic_repositories": project.epic_repositories or {},
                "epics": epics, "github_repos": available, "stacks": sorted(STACK_TOOLS)}

    @app.put("/v1/projects/{key}/repositories")
    def save_project_repositories(key: str, body: ProjectRepositoriesBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="save_project_repositories", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            project_audit("save_project_repositories", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        if not ctx.jira or not github_connection_list(ctx):
            project_audit("save_project_repositories", key, user.subject, False,
                          "Jira and GitHub must be connected")
            raise HTTPException(503, "Jira and GitHub must be connected")
        try:
            epics = jira_project_epics(key)
            available_repositories = github_catalog(user.subject, "project_repository_save")
        except Exception as exc:
            project_audit("save_project_repositories", key, user.subject, False, "could not load providers")
            log.warning("project repository save preflight failed (%s)", type(exc).__name__)
            raise HTTPException(502, "could not load Jira epics and GitHub repositories") from exc
        available = {str(item["full_name"]).lower(): item for item in available_repositories}
        gateways = {conn.id: conn.gateway for conn in github_connection_list(ctx)}
        chosen: dict[str, str] = {}  # repository -> connection the owner picked on this save
        epic_keys = {item["key"] for item in epics}
        repos: dict[str, dict[str, Any]] = {}
        error = ""
        for item in body.repositories:
            name = item.repo.strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name) or name.lower() not in available:
                error = f"repository {name} is not visible to any connected GitHub account"
                break
            if item.connection:
                if item.connection not in available[name.lower()]["connections"]:
                    error = f"GitHub connection {item.connection} cannot reach {name}"
                    break
                chosen[name.lower()] = item.connection
            if name.lower() in repos:
                error = f"repository {name} is listed more than once"
                break
            base = item.base.strip()
            effective_base = base or project.base
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}", effective_base) or ".." in effective_base:
                error = f"invalid base branch for {name}"
                break
            if item.stack and item.stack not in STACK_TOOLS:
                error = f"unsupported stack for {name}"
                break
            repos[name.lower()] = {"repo": name, "base": base,
                                   "checks": [check.strip() for check in item.checks if check.strip()],
                                   "stack": item.stack.strip(),
                                   **({"containers": item.containers} if item.containers is not None else {})}
        mappings = {str(epic).strip(): str(name).strip() for epic, name in body.epic_repositories.items()
                    if str(name).strip()}
        if not error:
            for epic, name in mappings.items():
                if epic not in epic_keys:
                    error = f"{epic} is not an epic in Jira project {key}"
                    break
                if name.lower() not in available:
                    error = f"repository {name} is not visible to any connected GitHub account"
                    break
                if name.lower() not in repos:
                    repos[name.lower()] = {"repo": name, "base": "", "checks": [], "stack": ""}
        if not error and project.repo.lower() not in repos:
            repos[project.repo.lower()] = {"repo": project.repo, "base": "", "checks": [], "stack": ""}
        if not error:
            for item in repos.values():
                lowered = item["repo"].lower()
                connection = chosen.get(lowered) or (available.get(lowered) or {}).get("connection", "")
                if not connection or connection not in gateways:
                    error = f"no GitHub connection can reach {item['repo']}"
                    break
                branch = item["base"] or project.base
                if not gateways[connection].branch_exists(item["repo"].split("/", 1)[-1], branch):
                    error = f"base branch {branch} is missing from {item['repo']}"
                    break
        if error:
            project_audit("save_project_repositories", key, user.subject, False, error,
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise HTTPException(422, error)

        config_path = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            normalized = sorted(repos.values(), key=lambda item: item["repo"].lower())
            config["repositories"] = normalized
            config["epic_repositories"] = mappings
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = Path(ctx.settings.control_repo).name
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                       f"projects: update repositories for {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project repository update failed (%s)", type(exc).__name__)
            project_audit("save_project_repositories", key, user.subject, False, "control repo commit failed",
                          {"epic_repositories": mappings, "repositories": list(repos.values())})
            raise HTTPException(502, "could not save repository settings to aiflow-control") from exc
        for lowered, connection in chosen.items():
            github_connections(ctx).assign(repos[lowered]["repo"], connection)
            publish_for(ctx, GitHubConnectionAssigned(actor=user.subject, repo=repos[lowered]["repo"],
                                                      connection=connection, project=key))
        refresh_projects()
        publish_for(ctx, ProjectRepositoriesSaved(project=key, actor=user.subject,
                                                  result=commit or "bot commit created",
                                                  details={"epic_repositories": mappings,
                                                           "repositories": normalized}))
        ctx.events.emit("project.repositories_changed", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}",
                        data={"epic_count": len(mappings), "repository_count": len(normalized)})
        return {"saved": True, "project": key, "commit": commit,
                "epic_repositories": mappings, "repositories": normalized}

    # --- environment of the runs: configuration, missing items, scan (docs/v4/environment.md) ----------------------
    @app.get("/v1/projects/{key}/environment")
    def project_environment(key: str, request: Request) -> dict:
        console_user(request, {"projects.manage"}, target="aiflow-control", action="read", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            raise HTTPException(404, "project is not configured")
        items, problems = parse_env_items(project.environment)
        return {"project": key, "items": [item.public() for item in items], "problems": problems,
                "kinds": list(ENV_KINDS), "runs": list(ENV_RUN_STEPS),
                "missing": [r for r in environment_reports.reports(ctx) if r["project"] == key],
                "post_merge": environment_reports.post_merge_report(ctx, key)}

    @app.put("/v1/projects/{key}/environment")
    def save_project_environment(key: str, body: ProjectEnvironmentBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control",
                            action="save_project_environment", node=key)
        project = ctx.repo().projects.get(key)
        if not project:
            project_audit("save_project_environment", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        items, problems = parse_env_items(body.items)
        if problems or len(items) != len(body.items):
            project_audit("save_project_environment", key, user.subject, False, "; ".join(problems)[:300],
                          {"project": key, "names": [str(i.get("name", ""))[:64] for i in body.items]})
            raise HTTPException(422, "; ".join(problems) or "every environment item needs a name and a kind")
        config_path = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            config["environment"] = [{k: v for k, v in (
                ("name", i.name), ("kind", i.kind), ("value", i.value), ("runs", list(i.runs)),
                ("description", i.description)) if v not in ("", None)} for i in items]
            if not items:
                config.pop("environment")
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = Path(ctx.settings.control_repo).name
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                       f"projects: update the environment of {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project environment update failed (%s)", type(exc).__name__)
            project_audit("save_project_environment", key, user.subject, False, "control repo commit failed")
            raise HTTPException(502, "could not save the environment to aiflow-control") from exc
        refresh_projects()
        # names only in the audit: the values are configuration, the secrets are never in the file
        publish_for(ctx, ProjectEnvironmentSaved(project=key, actor=user.subject,
                                                 result=commit or "bot commit created",
                                                 details={"project": key, "names": [i.name for i in items]}))
        ctx.events.emit("project.environment_changed", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}", data={"items": len(items)})
        return {"saved": True, "project": key, "commit": commit, "items": [i.public() for i in items]}

    @app.get("/v1/environment/missing")
    def environment_missing_items(request: Request) -> dict:
        user = (console_user(request, "projects.manage") if ctx.settings.console_login
                else optional_console_user(request))
        rows = environment_reports.reports(ctx)
        if user and user.project_keys is not None:
            rows = [r for r in rows if r["project"] in user.project_keys]
        return {"missing": rows}

    @app.get("/v1/environment/scan")
    def environment_scan_report(request: Request) -> dict:
        console_user(request, {"projects.manage"}, target="aiflow-control", action="read")
        return {"scan": environment_reports.last_scan(ctx)}

    @app.post("/v1/environment/scan")
    def environment_scan_run(request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="environment_scan")
        report = environment_reports.scan(ctx, actor=user.subject)
        publish_for(ctx, ProjectEnvironmentScanned(project="", actor=user.subject,
                                                   result=f"{len(report['offenders'])} of {report['scanned']} open tickets",
                                                   details={"scan": report["id"]}))
        return {"scan": report}

    @app.post("/v1/environment/removals")
    def environment_removals(body: EnvironmentRemovalBody, node: str = Depends(node_auth)) -> dict:
        """The intake AI's record of the wording it removed and the environment needs behind it (fenced)."""
        run = fenced(body.run_id, body.fence, "environment", "removals", {"removed": len(body.removed)})
        if run["step"] != "intake":
            raise HTTPException(403, "only an intake run records removed wording")
        environment_reports.record_removal(ctx, run, body.removed, body.needs)
        tickets = environment_reports.open_need_tickets(ctx, run, body.needs)
        publish_for(ctx, EnvironmentRemovalsRecorded(run_id=body.run_id, fence=body.fence,
                                                     ticket=run["ticket_key"], actor=run["account_id"],
                                                     removed=len(body.removed), tickets=len(tickets),
                                                     needs=len(body.needs)))
        return {"ok": True, "environment_tickets": tickets}

    @app.post("/v1/projects/preflight")
    def project_preflight_endpoint(body: ProjectPreflightBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_preflight")
        return project_preflight(body, user.subject)

    @app.post("/v1/projects")
    def add_project(body: ProjectCreateBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="add_project",
                            node=body.project[:32])
        unknown = [service for service in body.services if service.strip() and not normalize_services(service)]
        if unknown:
            raise HTTPException(422, "unknown test services: " + ", ".join(unknown) + ". Use postgres or redis.")
        with ctx.db.pool.connection() as connection:
            with connection.transaction():
                # Keep the shared admission lock through preflight, the external commit, and repo refresh. A
                # competing request then counts the project just committed before it can take the final slot.
                licensing.lock_resource_admission(connection)
                if not refresh_projects():
                    project_audit("add_project", body.project[:32], user.subject, False,
                                  "control repo refresh failed before capacity check", {"repo": body.repo})
                    raise HTTPException(503, "could not refresh the control repo before checking project capacity")
                require_capacity("projects", len(ctx.repo().projects), connection=connection)
                try:
                    result = project_preflight(body, user.subject)
                except HTTPException as exc:
                    project_audit("add_project", body.project[:32], user.subject, False, str(exc.detail),
                                  {"project_input": body.project, "repo": body.repo})
                    raise
                key = result["project"]
                if key in ctx.repo().projects:
                    project_audit("add_project", key, user.subject, False, "project is already configured",
                                  {"repo": body.repo})
                    raise HTTPException(409, "project is already configured")
                if not result["ok"]:
                    reasons = []
                    if result["missing_statuses"]:
                        reasons.append("missing Jira statuses: " + ", ".join(result["missing_statuses"]))
                    if not result["branch_exists"]:
                        reasons.append(f"base branch {body.base} is missing from {body.repo}")
                    detail = {"message": "; ".join(reasons) or "project checks failed",
                              "missing_statuses": result["missing_statuses"],
                              "branch_exists": result["branch_exists"], "nodes": result["nodes"]}
                    project_audit("add_project", key, user.subject, False, "; ".join(reasons), detail)
                    raise HTTPException(422, detail)

                worker_nodes = {row["name"] for row in ctx.db.all(
                    "SELECT name FROM nodes WHERE role='worker' AND removed_at IS NULL")}
                checks = [check.strip() for check in body.checks if check.strip()]
                config = {"key": key, "repo": body.repo, "base": body.base, "stack": body.stack,
                          "services": normalize_services(body.services), "checks": checks, "containers": body.containers, "merge": {"method": body.merge_method},
                          "limits": {"parallel": body.parallel_limit,
                                     "work_minutes": dict(work_limits.DEFAULTS)},
                          "priority": body.priority, "paused": False}
                content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
                inventory.assign_project(connection, key, sorted(worker_nodes))
                control_name = Path(ctx.settings.control_repo).name
                try:
                    github = github_control_gateway(ctx)
                    branch = github.default_branch(control_name)
                    commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                                f"projects: add {key} via AI Flow console")
                except HTTPException:
                    raise
                except Exception as exc:
                    log.warning("project add commit failed (%s)", type(exc).__name__)
                    project_audit("add_project", key, user.subject, False, "control repo commit failed", config)
                    raise HTTPException(502, "could not save the project to aiflow-control") from exc
                # Make the committed project visible to the next admission before releasing the lock.
                refresh_projects()
        publish_for(ctx, ProjectAdded(project=key, actor=user.subject,
                                      result=commit or "bot commit created", details=config))
        ctx.events.emit("project.added", project=key, account=user.subject, outcome="saved",
                        reason=f"control repo commit {commit or 'created'}")
        system_check.refresh_all(ctx, "project_added")
        return {"saved": True, "project": key, "commit": commit, "preflight": result}

    @app.post("/v1/projects/{key}/services")
    def save_project_services(key: str, body: ProjectServicesBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="project_services", node=key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            project_audit("project_services", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        unknown = [s for s in body.services if s.strip() and not normalize_services(s)]
        if unknown:
            raise HTTPException(422, "unknown test services: " + ", ".join(unknown) + ". Use postgres or redis.")
        try:
            names, commit = declare_project_services(ctx, key, body.services, f"projects: test services of {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project services update failed (%s)", type(exc).__name__)
            project_audit("project_services", key, user.subject, False, "control repo commit failed", {"services": body.services})
            raise HTTPException(502, "could not update the project in aiflow-control") from exc
        publish_for(ctx, ProjectServicesChanged(project=key, actor=user.subject,
                                                result=commit or "bot commit created", details={"services": names}))
        ctx.events.emit("project.services_saved", project=key, account=user.subject, outcome="saved",
                        reason=", ".join(names) or "none")
        return {"saved": True, "project": key, "services": names, "commit": commit}

    @app.post("/v1/projects/{key}/pause")
    def pause_project(key: str, body: ProjectPauseBody, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="pause_project", node=key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            project_audit("pause_project", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        project_file = ctx.repo().root / "projects" / f"{key}.yaml"
        try:
            config = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
            config["paused"] = body.paused
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            control_name = Path(ctx.settings.control_repo).name
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.commit_file(control_name, branch, f"projects/{key}.yaml", content,
                                            f"projects: {'pause' if body.paused else 'resume'} {key}")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project pause update failed (%s)", type(exc).__name__)
            project_audit("pause_project", key, user.subject, False, "control repo commit failed",
                          {"paused": body.paused})
            raise HTTPException(502, "could not update the project in aiflow-control") from exc
        refresh_projects()
        action = "paused" if body.paused else "resumed"
        publish_for(ctx, ProjectPauseChanged(project=key, actor=user.subject,
                                             result=commit or f"{action} by bot commit",
                                             details={"project": key, "paused": body.paused}))
        ctx.events.emit(f"project.{action}", project=key, account=user.subject, outcome=action,
                        reason=f"control repo commit {commit or 'created'}")
        return {"project": key, "status": "paused" if body.paused else "active", "commit": commit}

    @app.delete("/v1/projects/{key}")
    def remove_project(key: str, request: Request) -> dict:
        user = console_user(request, {"projects.manage"}, target="aiflow-control", action="remove_project", node=key)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) or key not in ctx.repo().projects:
            project_audit("remove_project", key[:32], user.subject, False, "project is not configured")
            raise HTTPException(404, "project is not configured")
        control_name = Path(ctx.settings.control_repo).name
        try:
            github = github_control_gateway(ctx)
            branch = github.default_branch(control_name)
            commit = github.delete_file(control_name, branch, f"projects/{key}.yaml",
                                            f"projects: remove {key} via AI Flow console")
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("project remove commit failed (%s)", type(exc).__name__)
            project_audit("remove_project", key, user.subject, False, "control repo commit failed")
            raise HTTPException(502, "could not remove the project from aiflow-control") from exc
        refresh_projects()
        publish_for(ctx, ProjectRemoved(project=key, actor=user.subject,
                                        result=commit or "bot commit created", details={"project": key}))
        ctx.events.emit("project.removed", project=key, account=user.subject, outcome="removed",
                        reason=f"control repo commit {commit or 'created'}")
        return {"removed": True, "project": key, "commit": commit}

    return SimpleNamespace(github_secret_store=github_secret_store, audit_github_repository_changes=audit_github_repository_changes, github_install_url=github_install_url, github_installation_settings=github_installation_settings, github_public_base=github_public_base, pending_gateway=pending_gateway, complete_github_connection=complete_github_connection, github_connection_status=github_connection_status, target_connection=target_connection, connection_view=connection_view, find_connection=find_connection, github_connections_status=github_connections_status, github_add_token=github_add_token, github_test_connection=github_test_connection, github_replace_token=github_replace_token, github_remove_connection=github_remove_connection, github_manifest_start=github_manifest_start, github_manifest_callback=github_manifest_callback, github_install_setup=github_install_setup, github_import_installations=github_import_installations, github_import_install=github_import_install, github_import_connect=github_import_connect, github_open_installation_settings=github_open_installation_settings, required_project_statuses=required_project_statuses, jira_site=jira_site, project_key_from_input=project_key_from_input, github_catalog=github_catalog, connection_summaries=connection_summaries, project_preflight=project_preflight, refresh_projects=refresh_projects, project_audit=project_audit, ticket_epic=ticket_epic, managed_projects=managed_projects, project_catalog=project_catalog, jira_project_epics=jira_project_epics, repository_settings_payload=repository_settings_payload, project_repositories=project_repositories, save_project_repositories=save_project_repositories, project_environment=project_environment, save_project_environment=save_project_environment, environment_missing_items=environment_missing_items, environment_scan_report=environment_scan_report, environment_scan_run=environment_scan_run, environment_removals=environment_removals, project_preflight_endpoint=project_preflight_endpoint, add_project=add_project, save_project_services=save_project_services, pause_project=pause_project, remove_project=remove_project)
