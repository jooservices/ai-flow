"""Ordered installer groups for project routes.

Route decorators stay with their handlers to preserve the published route contract.
"""
from types import SimpleNamespace
from . import routes as compat


def _install_github_connection_status_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/integrations/github")
    def github_connection_status(request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"})
        gateway = runtime.github_gateway(ctx)
        if not gateway:
            return {"connected": False, "app": None, "account": None, "installation_id": None,
                    "repository_count": 0, "settings_url": ""}
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        account, account_type = compat._github_status_account(ctx, meta, gateway)
        try:
            repositories = gateway.installation_repositories()
            compat._audit_github_repository_changes(runtime, gateway, repositories, user.subject, "integration_refresh")
        except Exception as exc:  # provider response may contain credentials; return only a generic status
            runtime.log.warning("GitHub installation status failed (%s)", type(exc).__name__)
            raise compat.HTTPException(502, "could not load GitHub installation status") from exc
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        return {"connected": True,
                "app": {"id": str(meta.get("id") or ""), "name": str(meta.get("name") or "GitHub App"),
                        "slug": str(meta.get("slug") or "")},
                "account": {"login": account, "type": account_type},
                "installation_id": int(installation_id) if installation_id else None,
                "repository_count": len(repositories),
                "settings_url": compat._github_installation_settings(account, account_type, int(installation_id))
                if installation_id else ""}
    return github_connection_status

def _install_github_connections_status_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/integrations/github/connections")
    def github_connections_status(request: compat.Request) -> dict[str, compat.Any]:
        console_user(request, {"integrations.manage"})
        return {"connections": [compat._connection_view(runtime, c) for c in runtime.github_connection_list(ctx)],
                "recommendation": "Use a GitHub App; an access token is broader, has no bot identity and cannot "
                                  "be limited to one repository per run."}
    return github_connections_status

def _install_github_add_token_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.post("/v1/integrations/github/connections/token")
    def github_add_token(body: compat.GitHubConnectionTokenBody, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="add_token_connection")
        account = body.account.strip()
        if account and not compat.re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", account):
            audit("github", "add_token_connection", "", "", None, user.subject, False, "invalid account")
            raise compat.HTTPException(422, "enter a valid GitHub account or organization login")
        try:
            gateway = compat.verify_token(body.token.strip(), account)
        except Exception as exc:
            runtime.log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "add_token_connection", "", "", None, user.subject, False, "token could not be verified")
            raise compat.HTTPException(422, "GitHub did not accept this token for that account") from exc
        label = body.label.strip() or f"{gateway.org} access token"
        connection_id = runtime.github_connections(ctx).add_token(label, body.token.strip(), gateway.org,
                                                           gateway.meta.get("account_type", ""))
        compat.publish_for(ctx, compat.GitHubTokenConnectionAdded(actor=user.subject, connection=connection_id,
                                                    account=gateway.org))
        ctx.events.emit("github.connected", account=gateway.org, outcome="connected", reason="token")
        return compat._connection_view(runtime, compat._find_connection(runtime, connection_id))
    return github_add_token

def _install_github_test_connection_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.post("/v1/integrations/github/connections/{connection_id}/test")
    def github_test_connection(connection_id: str, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="test_connection")
        view = compat._connection_view(runtime, compat._find_connection(runtime, connection_id))
        audit("github", "test_connection", "", "", None, user.subject, view["status"] == "connected",
              view["status"], {"connection": connection_id, "repositories": len(view["repositories"])})
        return view
    return github_test_connection

def _install_github_replace_token_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.put("/v1/integrations/github/connections/{connection_id}/token")
    def github_replace_token(connection_id: str, body: compat.GitHubConnectionTokenBody, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="replace_token_connection")
        conn = compat._find_connection(runtime, connection_id)
        if conn.kind != "token":
            raise compat.HTTPException(409, "only an access token connection takes a new token; reconnect the App instead")
        try:
            gateway = compat.verify_token(body.token.strip(), conn.account)
        except Exception as exc:
            runtime.log.warning("GitHub token could not be verified (%s)", type(exc).__name__)
            audit("github", "replace_token_connection", "", "", None, user.subject, False,
                  "token could not be verified", {"connection": connection_id})
            raise compat.HTTPException(422, "GitHub did not accept this token for that account") from exc
        runtime.github_connections(ctx).replace_token(connection_id, body.token.strip(), gateway.org,
                                              gateway.meta.get("account_type", ""))
        compat.publish_for(ctx, compat.GitHubTokenConnectionReplaced(actor=user.subject, connection=connection_id,
                                                       account=gateway.org))
        return compat._connection_view(runtime, compat._find_connection(runtime, connection_id))
    return github_replace_token

def _install_github_remove_connection_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.delete("/v1/integrations/github/connections/{connection_id}")
    def github_remove_connection(connection_id: str, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="remove_connection")
        compat._find_connection(runtime, connection_id)
        registry = runtime.github_connections(ctx)
        using = sorted(repo for repo, assigned in registry.assignments().items() if assigned == connection_id)
        if using:
            audit("github", "remove_connection", "", "", None, user.subject, False,
                  "connection is assigned to repositories", {"connection": connection_id, "repositories": using})
            raise compat.HTTPException(409, "reassign these repositories first: " + ", ".join(using))
        if connection_id == compat.DEFAULT_ID:
            for name in ("app.json", "app.pem"):
                (compat.Path(ctx.settings.github_app_dir) / name).unlink(missing_ok=True)
            ctx.github = None
        else:
            registry.remove(connection_id)
        compat.publish_for(ctx, compat.GitHubConnectionRemoved(actor=user.subject, connection=connection_id))
        return {"removed": connection_id}
    return github_remove_connection

def _install_github_manifest_start_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.get("/v1/integrations/github/manifest/start", response_class=compat.HTMLResponse)
    def github_manifest_start(organization: str, request: compat.Request, connection: str = "default") -> compat.HTMLResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="manifest_start")
        if not compat.re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", organization):
            audit("github", "manifest_start", "", "", None, user.subject, False, "invalid GitHub organization")
            raise compat.HTTPException(422, "enter a valid GitHub organization login")
        base = compat._github_public_base(runtime)
        store = compat._github_secret_store(runtime)
        state = store.new_id()
        store.create_pending(state, {"phase": "manifest", "actor": user.subject, "connection": compat._target_connection(runtime, connection),
                                     "expected_owner": organization, "source": "manifest"})
        compat.publish_for(ctx, compat.GitHubManifestStarted(actor=user.subject, organization=organization))
        callback = base + "/v1/integrations/github/manifest/callback"
        setup = callback.removesuffix("/manifest/callback") + "/setup?state=" + state
        manifest = {"name": f"AI Flow {organization}", "url": base + "/cc", "description": "AI Flow GitHub integration",
                    "redirect_url": callback, "setup_url": setup, "setup_on_update": False, "public": False,
                    "default_events": [],
                    "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read",
                                            "actions": "read",
                                            "checks": "read", "statuses": "read", "workflows": "write",
                                            "administration": "write"}}
        action = f"https://github.com/organizations/{compat.quote(organization, safe='-')}/settings/apps/new?state={state}"
        encoded = compat.html.escape(compat.json.dumps(manifest, separators=(",", ":")), quote=True)
        action_attr = compat.html.escape(action, quote=True)
        body = ("<!doctype html><html><head><meta charset='utf-8'><title>Connect GitHub</title></head><body>"
                "<p>Continue to GitHub to create the AI Flow App.</p>"
                f"<form id='manifest' action='{action_attr}' method='post'>"
                f"<input type='hidden' name='manifest' value='{encoded}'>"
                "<button type='submit'>Continue to GitHub</button></form>"
                "<script>document.getElementById('manifest').submit()</script></body></html>")
        return compat.HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return github_manifest_start

def _install_github_manifest_callback_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    @app.get("/v1/integrations/github/manifest/callback")
    def github_manifest_callback(code: str, state: str) -> compat.RedirectResponse:
        store = compat._github_secret_store(runtime)
        try:
            record, _, _ = store.read_pending(state)
            if record.get("phase") != "manifest":
                raise ValueError("GitHub manifest state is invalid")
            converted = compat._convert_github_manifest(code, str(record.get("expected_owner") or ""))
            pem = converted.get("pem")
            record["phase"] = "manifest_setup"
            meta = compat.app_metadata(converted)
            store.update_pending(state, record, meta, pem)
            actor = str(record.get("actor") or "github-manifest")
            compat.publish_for(ctx, compat.GitHubManifestCreated(actor=actor, app_id=str(meta.get("id") or ""),
                                                   organization=record.get("expected_owner", "")))
            return compat.RedirectResponse(compat._github_install_url(str(meta["slug"])), status_code=303)
        except Exception as exc:  # no provider response or callback credential is copied into logs
            runtime.log.warning("GitHub manifest callback failed (%s)", type(exc).__name__)
            actor = compat._delete_failed_manifest_state(store, state)
            audit("github", "manifest_created", "", "", None, actor, False, "GitHub manifest setup failed")
            return compat.RedirectResponse("/integrations?github=error", status_code=303)
    return github_manifest_callback

def _install_github_install_setup_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    @app.get("/v1/integrations/github/setup")
    def github_install_setup(installation_id: int, state: str, setup_action: str = "install") -> compat.RedirectResponse:
        store = compat._github_secret_store(runtime)
        actor = "github-setup"
        try:
            record, _, _ = store.read_pending(state)
            actor = str(record.get("actor") or actor)
            if record.get("phase") != "manifest_setup":
                raise ValueError("GitHub setup state is invalid")
            compat._complete_github_connection(runtime, store, state, installation_id, actor, "manifest")
            return compat.RedirectResponse("/integrations?github=connected", status_code=303)
        except Exception as exc:
            runtime.log.warning("GitHub installation callback failed (%s)", type(exc).__name__)
            try:
                store.delete_pending(state)
            except Exception:
                pass
            audit("github", "connect", "", "", None, actor, False, "GitHub installation could not be verified",
                  {"installation_id": installation_id, "source": "manifest", "setup_action": setup_action})
            return compat.RedirectResponse("/integrations?github=error", status_code=303)
    return github_install_setup

def _install_github_import_app_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.post("/v1/integrations/github/imports")
    async def github_import_app(request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="import_app")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 25_000:
                audit("github", "import_app", "", "", None, user.subject, False, "private key upload too large")
                raise compat.HTTPException(413, "private key file is too large")
        payload, refusal = compat._decode_github_import(raw)
        if refusal:
            audit("github", "import_app", "", "", None, user.subject, False, "invalid App import request")
            raise compat.HTTPException(422, "select a GitHub App ID and private key file")
        app_id, pem = payload.get("app_id"), payload.get("private_key")
        if type(app_id) is not int or app_id < 1:
            audit("github", "import_app", "", "", None, user.subject, False, "invalid GitHub App id")
            raise compat.HTTPException(422, "enter a valid GitHub App ID")
        if not compat._github_private_key_is_valid(pem):
            audit("github", "import_app", "", "", None, user.subject, False, "invalid private key file",
                  {"app_id": app_id})
            raise compat.HTTPException(422, "select a valid GitHub App private key file")
        store = compat._github_secret_store(runtime)
        pending_id = store.new_id()
        record = {"phase": "import", "actor": user.subject, "source": "import", "app": {"id": app_id},
                  "connection": compat._target_connection(runtime, payload.get("connection"))}
        store.create_pending(pending_id, record, pem)
        try:
            details = compat._verify_imported_github_app(runtime, store, pending_id, app_id, pem, record)
            compat.publish_for(ctx, compat.GitHubAppImportVerified(actor=user.subject, app_id=app_id))
            return compat._github_import_result(pending_id, details)
        except Exception as exc:
            store.delete_pending(pending_id)
            runtime.log.warning("GitHub App import could not be verified (%s)", type(exc).__name__)
            audit("github", "import_app", "", "", None, user.subject, False, "GitHub App credentials could not be verified",
                  {"app_id": app_id})
            raise compat.HTTPException(422, "could not verify this GitHub App and private key") from exc
    return github_import_app

def _install_github_import_installations_route(app, ctx, deps, runtime, support):
    console_user = runtime.console_user
    @app.get("/v1/integrations/github/imports/{pending_id}/installations")
    def github_import_installations(pending_id: str, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="list_import_installations")
        store = compat._github_secret_store(runtime)
        stage = "load pending App"
        try:
            record, meta, _ = store.read_pending(pending_id)
            if record.get("phase") != "import":
                raise ValueError("GitHub App import state is invalid")
            stage = "query GitHub installations"
            gateway = compat._pending_gateway(runtime, store, pending_id, meta)
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
                    "install_url": compat._github_install_url(str(meta.get("slug") or ""))}
        except Exception as exc:
            runtime.log.warning("GitHub App installations could not be listed (%s, stage=%s)", type(exc).__name__, stage)
            raise compat.HTTPException(502, "could not load installations for this GitHub App") from exc
    return github_import_installations

def _install_github_import_install_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.get("/v1/integrations/github/imports/{pending_id}/install")
    def github_import_install(pending_id: str, request: compat.Request) -> compat.RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_app_install")
        try:
            record, meta, _ = compat._github_secret_store(runtime).read_pending(pending_id)
            if record.get("phase") != "import" or not meta.get("slug"):
                raise ValueError("GitHub App import state is invalid")
        except Exception as exc:
            audit("github", "open_app_install", "", "", None, user.subject, False, "GitHub App import expired")
            raise compat.HTTPException(404, "GitHub App import expired; start again") from exc
        compat.publish_for(ctx, compat.GitHubAppInstallOpened(actor=user.subject, app_id=str(meta.get("id") or "")))
        return compat.RedirectResponse(compat._github_install_url(str(meta["slug"])), status_code=303)
    return github_import_install

def _install_github_import_connect_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.post("/v1/integrations/github/imports/{pending_id}/connect")
    def github_import_connect(pending_id: str, body: compat.GitHubInstallationBody, request: compat.Request) -> dict[str, compat.Any]:
        user = console_user(request, {"integrations.manage"}, target="github", action="connect_imported_app")
        try:
            result = compat._complete_github_connection(runtime, compat._github_secret_store(runtime), pending_id, body.installation_id,
                                               user.subject, "import")
            return {"connected": True, **result}
        except Exception as exc:
            runtime.log.warning("GitHub imported App connection failed (%s)", type(exc).__name__)
            audit("github", "connect", "", "", None, user.subject, False,
                  "GitHub installation could not be verified",
                  {"installation_id": body.installation_id, "source": "import"})
            raise compat.HTTPException(422, "could not connect this GitHub installation") from exc
    return github_import_connect

def _install_github_open_installation_settings_route(app, ctx, deps, runtime, support):
    audit = runtime.audit
    console_user = runtime.console_user
    @app.get("/v1/integrations/github/installation-settings")
    def github_open_installation_settings(request: compat.Request) -> compat.RedirectResponse:
        user = console_user(request, {"integrations.manage"}, target="github", action="open_installation_settings")
        gateway = runtime.github_gateway(ctx)
        if not gateway:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub App is not connected")
            raise compat.HTTPException(503, "GitHub App is not connected")
        meta = getattr(gateway, "meta", {}) or {}
        installation_id = meta.get("installation_id") or getattr(gateway, "_inst", None)
        if not installation_id:
            audit("github", "open_installation_settings", "", "", None, user.subject, False,
                  "GitHub installation is not configured")
            raise compat.HTTPException(503, "GitHub installation is not configured")
        account = str(meta.get("account_login") or getattr(gateway, "org", "") or ctx.settings.github_org)
        account_type = str(meta.get("account_type") or "Organization")
        url = compat._github_installation_settings(account, account_type, int(installation_id))
        compat.publish_for(ctx, compat.GitHubInstallationSettingsOpened(actor=user.subject,
                                                          app_id=str(meta.get("id") or ""),
                                                          installation_id=int(installation_id), account=account))
        return compat.RedirectResponse(url, status_code=303)
    return github_open_installation_settings


def install_integrations_routes(app, ctx, deps, runtime, support) -> SimpleNamespace:
    github_connection_status = _install_github_connection_status_route(app, ctx, deps, runtime, support)
    github_connections_status = _install_github_connections_status_route(app, ctx, deps, runtime, support)
    github_add_token = _install_github_add_token_route(app, ctx, deps, runtime, support)
    github_test_connection = _install_github_test_connection_route(app, ctx, deps, runtime, support)
    github_replace_token = _install_github_replace_token_route(app, ctx, deps, runtime, support)
    github_remove_connection = _install_github_remove_connection_route(app, ctx, deps, runtime, support)
    github_manifest_start = _install_github_manifest_start_route(app, ctx, deps, runtime, support)
    github_manifest_callback = _install_github_manifest_callback_route(app, ctx, deps, runtime, support)
    github_install_setup = _install_github_install_setup_route(app, ctx, deps, runtime, support)
    _install_github_import_app_route(app, ctx, deps, runtime, support)
    github_import_installations = _install_github_import_installations_route(app, ctx, deps, runtime, support)
    github_import_install = _install_github_import_install_route(app, ctx, deps, runtime, support)
    github_import_connect = _install_github_import_connect_route(app, ctx, deps, runtime, support)
    github_open_installation_settings = _install_github_open_installation_settings_route(app, ctx, deps, runtime, support)
    return SimpleNamespace(
        github_connection_status=github_connection_status,
        github_connections_status=github_connections_status,
        github_add_token=github_add_token,
        github_test_connection=github_test_connection,
        github_replace_token=github_replace_token,
        github_remove_connection=github_remove_connection,
        github_manifest_start=github_manifest_start,
        github_manifest_callback=github_manifest_callback,
        github_install_setup=github_install_setup,
        github_import_installations=github_import_installations,
        github_import_install=github_import_install,
        github_import_connect=github_import_connect,
        github_open_installation_settings=github_open_installation_settings,
    )
