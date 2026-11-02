"""Security hardening of the control API (docs/v4/security.md).

Installed once by `create_app` after the console login (login.py), it adds:
- rate limits (AC3): a per-client-IP budget on every endpoint, and a tighter per-path budget on public endpoints
  (the ones that need no sign-in: login, install, join, provider callbacks, health) and on anonymous changes; a request
  over budget gets 429 with `Retry-After`. Sign-in guessing has its own protection in login.py;
- cross-site request protection (AC4): a state-changing request that rides a console session cookie must carry the
  `X-CSRF-Token` of that session (the console script copies it from the `aiflow4_csrf` cookie). Machine callers that
  send an `Authorization` header are not affected: a browser cannot add that header to a cross-site request;
- security headers, including a content security policy that blocks scripts from other origins;
- the audit trail (AC7): state-changing console requests are recorded in the transaction; credential OAuth callbacks
  and owner-token Jira changes are recorded too;
- the security screen's API under `/v1/security/*`: two-factor setup and recovery codes, session length, forced
  sign-out, people and the read-only audit log (`GET /v1/security/audit`; there is no route that writes to it).
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from . import mfa, notify
from .security_guard import CSP, CSRF_EXEMPT, CSRF_HEADER, SAFE_METHODS, SecurityKit  # noqa: F401 - re-exported
from .security_mfa import CodeBody, install_security_mfa  # noqa: F401 - re-exported
from .security_middleware import install_audit_changes, install_csrf, install_limits_and_headers
from .security_people import install_security_audit, install_security_people, install_security_sessions
from .security_settings import SettingsBody, install_security_settings  # noqa: F401 - re-exported

log = logging.getLogger(__name__)


def migrate_legacy_secrets(ctx) -> None:
    settings = ctx.settings
    mfa.migrate_plaintext_secrets(ctx.db, settings.console_session_secret)
    # The API must not serve while a notification credential remains plaintext or cannot be decrypted.
    notify.migrate_plaintext_credentials(ctx.db)
    # Same for the Jira and GitHub secret files: legacy plaintext is encrypted now, or startup fails without the key.
    from .github_connect import GitHubSecretStore
    from .jira_connect import SecretStore as JiraSecretStore
    JiraSecretStore(settings.jira_secret_dir).migrate_plaintext()
    GitHubSecretStore(settings.github_app_dir).migrate_plaintext()


def install_security(app: FastAPI, ctx) -> None:
    migrate_legacy_secrets(ctx)
    kit = SecurityKit(app, ctx)
    # middleware: the last one registered is the outermost
    install_audit_changes(kit)
    install_csrf(kit)
    install_limits_and_headers(kit)
    # routes, in the order the security screen lists them
    install_security_settings(kit)
    install_security_sessions(kit)
    install_security_people(kit)
    install_security_mfa(kit)
    install_security_audit(kit)
