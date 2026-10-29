"""Direct, optional delivery for owner escalation questions.

The sender is deliberately small and replaceable. Delivery is queued in PostgreSQL so all control workers share
one hourly limit and questions raised during that hour can be sent together.
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Protocol
from urllib.request import Request, urlopen

from aiflow.memory.guard import Rejected, scan_secrets

from .repositories import owner_notifications as notification_repository

log = logging.getLogger("aiflow4.owner_notifications")
RATE_LIMIT = timedelta(hours=1)
MAX_GROUP_CHARS = 3500


class Sender(Protocol):
    """One owner delivery channel."""

    def send(self, message: str) -> None: ...


class SmtpSender:
    def __init__(self, settings) -> None:
        self.settings = settings

    def send(self, message: str) -> None:
        settings = self.settings
        _require_tls_for_smtp_auth(settings)
        mail = EmailMessage()
        mail["From"] = settings.escalation_smtp_from or settings.escalation_email
        mail["To"] = settings.escalation_email
        mail["Subject"] = message.splitlines()[0][:180]
        mail.set_content(message)
        mode = settings.escalation_smtp_tls_mode
        smtp_type = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        tls_options = {"context": ssl.create_default_context()} if mode == "ssl" else {}
        with smtp_type(settings.escalation_smtp_host, settings.escalation_smtp_port,
                       timeout=10, **tls_options) as client:
            if mode == "starttls":
                client.starttls(context=ssl.create_default_context())
            if settings.escalation_smtp_username:
                client.login(settings.escalation_smtp_username, settings.escalation_smtp_password)
            client.send_message(mail)


class TelegramSender:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token, self.chat_id = token, chat_id

    def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the owner question")


def sender_for(settings) -> Sender | None:
    """Build a sender only when a complete optional channel is configured."""
    if settings.escalation_telegram_bot_token and settings.escalation_telegram_chat_id:
        return TelegramSender(settings.escalation_telegram_bot_token, settings.escalation_telegram_chat_id)
    email = settings.escalation_email.strip()
    host = settings.escalation_smtp_host.strip()
    sender = settings.escalation_smtp_from.strip() or email
    mode = settings.escalation_smtp_tls_mode
    if (_valid_address(email) and host and _valid_address(sender)
            and mode in ("none", "starttls", "ssl")):
        _require_tls_for_smtp_auth(settings)
        return SmtpSender(settings)
    return None


def _valid_address(value: str) -> bool:
    """A complete mailbox (`name@domain.tld`); anything else leaves the optional email channel unconfigured."""
    return bool(re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", value or ""))


def _require_tls_for_smtp_auth(settings) -> None:
    """Reject authenticated SMTP when the configured transport does not encrypt credentials."""
    if settings.escalation_smtp_username and settings.escalation_smtp_tls_mode not in ("starttls", "ssl"):
        raise ValueError("SMTP authentication requires TLS")


def _sender_or_configuration_failure(settings) -> tuple[Sender | None, dict | None]:
    """Keep an invalid optional channel from interrupting the required Jira hand-off."""
    try:
        return sender_for(settings), None
    except ValueError:
        return None, {"status": "failed", "sent": False}


def ticket_link(site_url: str, ticket: str) -> str:
    return f"{site_url.rstrip('/')}/browse/{ticket}"


def _active_jira_site_url(ctx) -> str:
    """Use the site selected on Integrations; the settings URL is only the legacy fallback."""
    auth = getattr(ctx, "jira_auth", None)
    if auth is None:
        return ctx.settings.jira_site
    return str(auth.status().get("site_url") or "")


def enqueue(ctx, decision_id: int, ticket: str, question: str,
            now: datetime | None = None) -> dict:
    """Persist one ticket's question, then send any due group through the configured channel."""
    sender, failure = _sender_or_configuration_failure(ctx.settings)
    if failure is not None:
        return failure
    if sender is None:
        return {"status": "unconfigured", "sent": False}
    question = str(question or "").strip()[:3000]
    if not question:
        return {"status": "empty", "sent": False}
    try:
        scan_secrets(question)
    except Rejected:  # fail closed: a credential copied into a question never leaves Jira
        log.warning("owner escalation notification for %s withheld: the question looks like it holds a secret", ticket)
        return {"status": "withheld", "sent": False}
    instant = now or datetime.now(timezone.utc)
    notification_repository.enqueue(ctx.db, decision_id, ticket, question,
                                    ticket_link(_active_jira_site_url(ctx), ticket), instant)
    return _send_due(ctx, sender, instant)


def enqueue_for_run(ctx, decision_id: int, run: dict) -> dict | None:
    """Queue the question only when this run's ticket still waits for the decision."""
    ticket = run["ticket_key"]
    pending = notification_repository.pending_question(ctx.db, decision_id, ticket)
    if (not pending or pending["status"] != "asked" or ticket not in pending["waiting_tickets"]):
        return None
    return enqueue(ctx, decision_id, ticket, pending["question"])


def process_pending(ctx, now: datetime | None = None) -> dict:
    """Flush a grouped notice after the hourly window, even when no new escalation arrives."""
    sender, failure = _sender_or_configuration_failure(ctx.settings)
    if failure is not None:
        return failure
    if sender is None:
        return {"status": "unconfigured", "sent": False}
    return _send_due(ctx, sender, now or datetime.now(timezone.utc))


def _group(rows: list[dict]) -> tuple[str, list[int]]:
    parts: list[str] = []
    included: list[int] = []
    size = 0
    for row in rows:
        item = f"{row['question']}\n{row['ticket_url']}"
        extra = len(item) + (2 if parts else 0)
        if parts and size + extra > MAX_GROUP_CHARS:
            break
        parts.append(item)
        included.append(int(row["id"]))
        size += extra
    return "\n\n".join(parts), included


def _send_due(ctx, sender: Sender, now: datetime) -> dict:
    """Use one persistent hourly slot to send the oldest due message group."""
    status, rows = notification_repository.claim_due(ctx.db, now, RATE_LIMIT)
    if status != "ready":
        return {"status": status, "sent": False}
    message, ids = _group(rows)
    try:
        sender.send(message)
    except Exception as exc:  # Provider exceptions may contain a credential or a secret-bearing URL.
        log.warning("owner escalation notification failed error=%s", type(exc).__name__)
        return {"status": "failed", "sent": False}
    notification_repository.mark_sent(ctx.db, ids, now)
    return {"status": "sent", "sent": True, "grouped": len(ids)}
