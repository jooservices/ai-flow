"""Stored alert channels, routing, delivery and scheduled summaries for the control plane.

Channel settings and delivery state live in the shared registry. Provider credentials are read only by this module;
the API uses :func:`public_settings` and never returns them. Delivery failures are recorded and retried by the
control loop, and the public ``send`` entry point intentionally never raises into a control loop.
"""
from __future__ import annotations

import logging
import re
import smtplib
from datetime import date, datetime, time as wall_time, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import httpx

from .db import J

log = logging.getLogger("aiflow4.notify")
CHANNEL_KINDS = ("email", "telegram", "slack", "teams", "webhook")
ALERT_KINDS = ("critical_incident", "ticket_needs_person", "review_before_merge", "daily_summary", "weekly_report")
ALERT_LABELS = {
    "critical_incident": "Critical incidents",
    "ticket_needs_person": "Ticket needs a person",
    "review_before_merge": "Review before merge",
    "daily_summary": "Daily summary",
    "weekly_report": "Weekly report",
}
MAX_ATTEMPTS = 4
RETRY_DELAYS_S = (30, 120, 600)
SENDING_RECOVERY_AFTER_S = 60
TICKET_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
SECRET_FIELDS = {
    "email": ("password",),
    "telegram": ("bot_token",),
    "slack": ("webhook_url",),
    "teams": ("webhook_url",),
    "webhook": ("url", "auth_token"),
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _url(value: str, *, required: bool = True) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
        _ = parsed.port
    except ValueError:
        valid = False
    if required and not valid:
        raise ValueError("enter a valid HTTP or HTTPS URL")
    return candidate if valid else ""


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if not low <= result <= high:
        raise ValueError(f"enter a value from {low} to {high}")
    return result


def _channel_config(kind: str, incoming: dict, previous: dict | None = None) -> dict:
    """Validate and normalize a configuration without ever including it in an error message."""
    previous = previous or {}
    source = {**previous, **{k: v for k, v in incoming.items() if v not in (None, "")}}
    if kind == "email":
        host = _text(source.get("host"), 253)
        if not host:
            raise ValueError("email server host is required")
        sender = _text(source.get("from"), 254)
        recipient = _text(source.get("to"), 254)
        if not sender or "@" not in sender or not recipient or "@" not in recipient:
            raise ValueError("enter a sender and recipient email address")
        tls_mode = source.get("tls_mode", "starttls")
        if tls_mode not in ("none", "starttls", "ssl"):
            raise ValueError("choose a valid email security mode")
        return {"mode": "smtp", "host": host, "port": _int(source.get("port"), 587, 1, 65535),
                "username": _text(source.get("username"), 254), "password": str(source.get("password") or ""),
                "from": sender, "to": recipient, "tls_mode": tls_mode}
    if kind == "telegram":
        token = str(source.get("bot_token") or "").strip()
        chat_id = _text(source.get("chat_id"), 128)
        if not token or not chat_id:
            raise ValueError("Telegram bot token and chat ID are required")
        ba_chat_enabled = bool(source.get("ba_chat_enabled", False))
        project_key = _text(source.get("project_key"), 10).upper()
        if ba_chat_enabled and not PROJECT_KEY.fullmatch(project_key):
            raise ValueError("choose a Jira project key to enable BA chat")
        return {"bot_token": token, "chat_id": chat_id, "ba_chat_enabled": ba_chat_enabled,
                "project_key": project_key}
    if kind in ("slack", "teams"):
        webhook = _url(source.get("webhook_url", ""))
        return {"webhook_url": webhook}
    if kind == "webhook":
        webhook = _url(source.get("url", ""))
        auth_token = str(source.get("auth_token") or "").strip()
        return {"url": webhook, "auth_token": auth_token}
    raise ValueError("unknown notification channel kind")


def save_channel(db, payload: dict) -> dict:
    kind = str(payload.get("kind") or "").strip().lower()
    name = _text(payload.get("name"), 100)
    if kind not in CHANNEL_KINDS:
        raise ValueError("choose a supported notification channel")
    if not name:
        raise ValueError("channel name is required")
    channel_id = payload.get("id")
    previous = None
    if channel_id is not None:
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            raise ValueError("channel was not found") from None
        previous = db.one("SELECT id, kind, config FROM notification_channels WHERE id=%s", (channel_id,))
        if not previous:
            raise ValueError("channel was not found")
        if previous["kind"] != kind:
            raise ValueError("a saved channel kind cannot be changed")
    config = _channel_config(kind, payload.get("config") if isinstance(payload.get("config"), dict) else {},
                             previous.get("config") if previous else {})
    enabled = bool(payload.get("enabled", True))
    if previous:
        row = db.one("UPDATE notification_channels SET name=%s, enabled=%s, config=%s, updated_at=now()"
                     " WHERE id=%s RETURNING id", (name, enabled, J(config), channel_id))
    else:
        row = db.one("INSERT INTO notification_channels (name, kind, enabled, config) VALUES (%s,%s,%s,%s)"
                     " RETURNING id", (name, kind, enabled, J(config)))
    return public_channel(db.one("SELECT * FROM notification_channels WHERE id=%s", (row["id"],)))


def _public_config(kind: str, config: dict) -> dict:
    if kind == "email":
        return {key: config.get(key, "") for key in ("mode", "host", "port", "username", "from", "to", "tls_mode")}
    if kind == "telegram":
        return {"chat_id": config.get("chat_id", ""),
                "ba_chat_enabled": bool(config.get("ba_chat_enabled", False)),
                "project_key": config.get("project_key", "")}
    return {}


def public_channel(row: dict | None) -> dict:
    if not row:
        return {}
    config = row.get("config") or {}
    kind = row["kind"]
    secrets_present = {f"has_{field}": bool(config.get(field)) for field in SECRET_FIELDS[kind]}
    return {"id": int(row["id"]), "name": row["name"], "kind": kind, "enabled": bool(row["enabled"]),
            "config": _public_config(kind, config), "secrets": secrets_present,
            "last_delivery": {"status": row.get("last_delivery_status") or "never",
                              "at": _iso(row.get("last_delivery_at")),
                              "error": row.get("last_delivery_error") or ""},
            "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at"))}


def save_preferences(db, payload: dict) -> dict:
    routes = payload.get("routes") if isinstance(payload.get("routes"), dict) else {}
    channel_ids = {int(row["id"]) for row in db.all("SELECT id FROM notification_channels")}
    cleaned: dict[str, list[int]] = {}
    for kind in ALERT_KINDS:
        values = routes.get(kind, [])
        if not isinstance(values, list):
            raise ValueError("each alert route must be a list of channels")
        try:
            selected = {int(value) for value in values}
        except (TypeError, ValueError):
            raise ValueError("alert routes contain an invalid channel") from None
        if not selected.issubset(channel_ids):
            raise ValueError("alert routes contain an unknown channel")
        cleaned[kind] = sorted(selected)
    start = str(payload.get("quiet_start") or "").strip()
    end = str(payload.get("quiet_end") or "").strip()
    for value in (start, end):
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("quiet hours must use UTC HH:MM times")
    if bool(start) != bool(end):
        raise ValueError("set both quiet hours or leave both empty")
    db.run("INSERT INTO notification_preferences (id, routes, quiet_start, quiet_end) VALUES (true,%s,%s,%s)"
           " ON CONFLICT (id) DO UPDATE SET routes=EXCLUDED.routes, quiet_start=EXCLUDED.quiet_start,"
           " quiet_end=EXCLUDED.quiet_end, updated_at=now()", (J(cleaned), start, end))
    return public_preferences(db)


def public_preferences(db) -> dict:
    row = db.one("SELECT routes, quiet_start, quiet_end, updated_at FROM notification_preferences WHERE id=true")
    if not row:
        return {"routes": {kind: [] for kind in ALERT_KINDS}, "quiet_start": "", "quiet_end": "", "timezone": "UTC"}
    routes = row["routes"] or {}
    return {"routes": {kind: [int(v) for v in routes.get(kind, [])] for kind in ALERT_KINDS},
            "quiet_start": row["quiet_start"], "quiet_end": row["quiet_end"], "timezone": "UTC",
            "updated_at": _iso(row.get("updated_at"))}


def public_settings(db, projects: list[dict] | None = None) -> dict:
    channels = db.all("SELECT * FROM notification_channels ORDER BY id")
    deliveries = db.all("SELECT id, channel_id, channel_name, channel_kind, alert_kind, title, link, project, ticket,"
                        " needed, status, attempts, max_attempts, last_error, is_test, created_at, updated_at"
                        " FROM notification_deliveries ORDER BY id DESC LIMIT 50")
    incidents = db.all("SELECT id, opened_at, severity, title, detail, subject FROM incidents"
                        " WHERE kind='notification_delivery' AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 50")
    result = {"kinds": list(CHANNEL_KINDS), "alert_kinds": ALERT_LABELS,
            "channels": [public_channel(row) for row in channels], "preferences": public_preferences(db),
            "deliveries": [{**row, "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"])}
                           for row in deliveries],
            "incidents": [{**row, "opened_at": _iso(row["opened_at"])} for row in incidents]}
    if projects is not None:
        result["projects"] = projects
    return result


def configured_channels(ctx, alert_kind: str = "ticket_needs_person") -> list[str]:
    """Return enabled, routed channel kinds without exposing their credentials or destinations."""
    if alert_kind not in ALERT_KINDS:
        return []
    preferences = public_preferences(ctx.db)
    selected = set(preferences["routes"].get(alert_kind, []))
    rows = ctx.db.all("SELECT id, kind FROM notification_channels WHERE enabled=true ORDER BY id")
    return list(dict.fromkeys(row["kind"] for row in rows if int(row["id"]) in selected))


def delete_channel(db, channel_id: int) -> bool:
    row = db.one("DELETE FROM notification_channels WHERE id=%s RETURNING id", (channel_id,))
    if not row:
        return False
    prefs = public_preferences(db)
    prefs["routes"] = {kind: [value for value in ids if value != channel_id]
                       for kind, ids in prefs["routes"].items()}
    save_preferences(db, prefs)
    return True


def _quiet_until(preferences: dict, now: datetime) -> datetime | None:
    start, end = preferences.get("quiet_start", ""), preferences.get("quiet_end", "")
    if not start or not end or start == end:
        return None
    minute = now.hour * 60 + now.minute
    a = int(start[:2]) * 60 + int(start[3:])
    b = int(end[:2]) * 60 + int(end[3:])
    quiet = a <= minute < b if a < b else minute >= a or minute < b
    if not quiet:
        return None
    until = now.replace(hour=int(end[:2]), minute=int(end[3:]), second=0, microsecond=0)
    if a > b and minute >= a:
        until += timedelta(days=1)
    return until


def _absolute_link(ctx, link: str, alert_kind: str) -> str:
    if link:
        if link.startswith("http://") or link.startswith("https://"):
            return link
        return ctx.settings.public_url.rstrip("/") + "/" + link.lstrip("/")
    path = {"critical_incident": "/cc", "ticket_needs_person": "/business", "review_before_merge": "/review",
            "daily_summary": "/delivery", "weekly_report": "/delivery"}.get(alert_kind, "/cc")
    return ctx.settings.public_url.rstrip("/") + path


def _envelope(ctx, title: str, detail: str, link: str, alert_kind: str, project: str, ticket: str,
              needed: str) -> tuple[str, str, str, str, str]:
    sample = "\n".join((title, detail, ticket))
    if not ticket:
        match = TICKET_KEY.search(sample)
        ticket = match.group(0).upper() if match else "System"
    ticket_row = ctx.db.one("SELECT project, summary FROM tickets WHERE key=%s", (ticket,)) if ticket != "System" else None
    if ticket_row:
        project = project or ticket_row["project"]
        ticket_label = f"{ticket} — {ticket_row['summary']}" if ticket_row.get("summary") else ticket
    else:
        ticket_label = ticket
    project = _text(project or (ticket.split("-")[0] if ticket != "System" else "AI Flow"), 160)
    need = _text(needed or detail or title, 500)
    action_link = _absolute_link(ctx, link, alert_kind)
    body = f"Project: {project}\nTicket: {ticket_label}\nNeeded: {need}"
    if detail:
        body += f"\n\nDetails:\n{detail[:3000]}"
    body += f"\n\nAct here: {action_link}"
    return project, ticket_label, need, action_link, body


def _dispatch(db, channel: dict, title: str, body: str, alert_kind: str, project: str, ticket: str, needed: str,
              link: str, is_test: bool, recipient: str = "", delay_until: datetime | None = None) -> tuple[dict, bool]:
    row = db.one("INSERT INTO notification_deliveries (channel_id, channel_name, channel_kind, alert_kind, title,"
                 " body, link, project, ticket, needed, status, attempts, max_attempts, next_attempt_at, is_test)"
                 " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s) RETURNING id",
                 (channel["id"], channel["name"], channel["kind"], alert_kind, title, body, link, project, ticket,
                  needed, "pending" if delay_until else "sending", MAX_ATTEMPTS, delay_until, is_test))
    delivery = db.one("SELECT * FROM notification_deliveries WHERE id=%s", (row["id"],))
    if delay_until:
        _record_result(db, channel["id"], row["id"], "quiet hours", "", 0, "pending", delay_until)
        return delivery, False
    _attempt(db, channel, delivery, recipient)
    return delivery, _delivery_success(db, row["id"])


def _record_result(db, channel_id: int, delivery_id: int, status: str, error: str, attempts: int,
                   delivery_status: str, next_attempt: datetime | None = None) -> None:
    db.run("UPDATE notification_deliveries SET status=%s, attempts=%s, last_error=%s, next_attempt_at=%s,"
           " updated_at=now(), delivered_at=CASE WHEN %s='sent' THEN now() ELSE delivered_at END WHERE id=%s",
           (delivery_status, attempts, error, next_attempt, delivery_status, delivery_id))
    db.run("UPDATE notification_channels SET last_delivery_status=%s, last_delivery_error=%s,"
           " last_delivery_at=now(), updated_at=now() WHERE id=%s",
           (status, error, channel_id))


def _deliver_channel(channel: dict, title: str, body: str, recipient: str = "") -> None:
    kind, config = channel["kind"], channel.get("config") or {}
    if kind == "email":
        msg = EmailMessage()
        msg["From"] = config["from"]
        msg["To"] = config["to"]
        if recipient and recipient.casefold() != str(config["to"]).casefold():
            msg["Cc"] = recipient
        msg["Subject"] = title
        msg.set_content(body)
        port = int(config.get("port") or 587)
        mode = config.get("tls_mode", "starttls")
        smtp_type = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
        with smtp_type(config["host"], port, timeout=10) as connection:
            if mode == "starttls":
                connection.starttls()
            if config.get("username"):
                connection.login(config["username"], config.get("password", ""))
            connection.send_message(msg)
        return
    if kind == "telegram":
        response = httpx.post(f"https://api.telegram.org/bot{config['bot_token']}/sendMessage",
                              json={"chat_id": config["chat_id"], "text": f"{title}\n{body}"[:4000]}, timeout=10)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the message")
        return
    elif kind in ("slack", "teams"):
        response = httpx.post(config["webhook_url"], json={"text": f"{title}\n{body}"[:4000]}, timeout=10)
    elif kind == "webhook":
        headers = {"Authorization": f"Bearer {config['auth_token']}"} if config.get("auth_token") else {}
        response = httpx.post(config["url"], headers=headers,
                              json={"title": title, "text": body, "project": channel.get("project", ""),
                                    "ticket": channel.get("ticket", ""), "link": channel.get("link", "")},
                              timeout=10)
    else:
        raise ValueError("unsupported channel")
    response.raise_for_status()


def _attempt(db, channel: dict, delivery: dict, recipient: str = "") -> dict:
    attempts = int(delivery["attempts"]) + 1
    # Event-specific context is put on a shallow copy so transport adapters never need database access.
    outgoing = {**channel, "project": delivery["project"], "ticket": delivery["ticket"], "link": delivery["link"]}
    try:
        _deliver_channel(outgoing, delivery["title"], delivery["body"], recipient)
    except Exception as exc:  # Credentials and provider URLs are deliberately absent from logs and stored errors.
        error = type(exc).__name__[:80]
        log.warning("notification delivery failed channel_id=%s error=%s", channel["id"], error)
        _record_attempt_failure(db, channel, delivery, error, attempts)
        return delivery
    _record_result(db, channel["id"], delivery["id"], "sent", "", attempts, "sent")
    _close_delivery_incident(db, delivery)
    return delivery


def _record_attempt_failure(db, channel: dict, delivery: dict, error: str, attempts: int) -> None:
    """Persist retry state or open the incident once this delivery exhausts its attempts."""
    if attempts >= int(delivery["max_attempts"]):
        _record_result(db, channel["id"], delivery["id"], "failed", error, attempts, "failed")
        _open_delivery_incident(db, channel, delivery, error)
        return
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAYS_S[min(attempts - 1,
                                                                               len(RETRY_DELAYS_S) - 1)])
    _record_result(db, channel["id"], delivery["id"], f"retrying ({attempts}/{delivery['max_attempts']})",
                   error, attempts, "pending", retry_at)


def _delivery_success(db, delivery_id: int) -> bool:
    row = db.one("SELECT status FROM notification_deliveries WHERE id=%s", (delivery_id,))
    return bool(row and row["status"] == "sent")


def _open_delivery_incident(db, channel: dict, delivery: dict, error: str) -> None:
    subject = f"{channel['id']}:{delivery['id']}"
    row = db.one("INSERT INTO incidents (kind, subject, severity, title, detail)"
                 " VALUES ('notification_delivery',%s,'critical',%s,%s)"
                 " ON CONFLICT (kind, subject) WHERE closed_at IS NULL DO NOTHING RETURNING id",
                 (subject, f"Alert delivery failed: {channel['name']}",
                  f"{delivery['alert_kind']} alert for {delivery['ticket']} failed after {delivery['max_attempts']} attempts ({error})."))
    if row:
        db.run("UPDATE notification_deliveries SET incident_id=%s WHERE id=%s", (row["id"], delivery["id"]))


def _close_delivery_incident(db, delivery: dict) -> None:
    if not delivery.get("incident_id"):
        return
    db.run("UPDATE incidents SET closed_at=now() WHERE id=%s AND closed_at IS NULL", (delivery["incident_id"],))


def _get_channel(db, channel_id: int) -> dict | None:
    return db.one("SELECT * FROM notification_channels WHERE id=%s", (channel_id,))


def send(severity: str, title: str, detail: str = "", link: str = "", recipient: str = "", *, ctx=None,
         alert_kind: str = "", project: str = "", ticket: str = "", needed: str = "", is_test: bool = False,
         channel_id: int | None = None, channels: list[str] | None = None, queued_ok: bool = False) -> list[str]:
    """Send through stored routes and swallow delivery failures so control loops continue running."""
    if ctx is None:
        return []
    kind = alert_kind or ("critical_incident" if severity == "critical" else
                          "ticket_needs_person" if severity == "warning" else "daily_summary")
    if kind not in ALERT_KINDS:
        return []
    try:
        available_channels = (ctx.db.all("SELECT * FROM notification_channels WHERE id=%s", (channel_id,))
                              if channel_id is not None else
                              ctx.db.all("SELECT * FROM notification_channels WHERE enabled=true ORDER BY id"))
        preferences = public_preferences(ctx.db)
        if channel_id is None and not is_test:
            selected = set(preferences["routes"].get(kind, []))
            available_channels = [channel for channel in available_channels if int(channel["id"]) in selected]
        if channel_id is None and channels is not None:
            allowed = set(channels)
            available_channels = [channel for channel in available_channels if channel["kind"] in allowed]
        project, ticket_label, need, action_link, body = _envelope(ctx, title, detail, link, kind, project, ticket, needed)
        subject_prefix = f"[{severity.upper()}] AI Flow:"
        subject = title if title.startswith(subject_prefix) else f"{subject_prefix} {title}"
        quiet_until = None if is_test or kind == "critical_incident" else _quiet_until(preferences, datetime.now(timezone.utc))
        sent = []
        for channel in available_channels:
            row = {"id": int(channel["id"]), "name": channel["name"], "kind": channel["kind"],
                   "config": channel["config"]}
            delivery, success = _dispatch(ctx.db, row, subject, body, kind, project, ticket_label, need, action_link,
                                          is_test, recipient, quiet_until)
            if success or (queued_ok and delivery):
                sent.append(row["kind"])
        return sent
    except Exception as exc:  # noqa: BLE001
        log.warning("notification dispatch failed error=%s", type(exc).__name__)
        return []


def send_test(ctx, channel_id: int) -> dict:
    """Deliver a channel-specific test, bypassing routing and quiet hours."""
    channel = _get_channel(ctx.db, channel_id)
    if not channel:
        raise ValueError("channel was not found")
    previous = ctx.db.one("SELECT coalesce(max(id),0) AS id FROM notification_deliveries WHERE channel_id=%s AND is_test=true",
                          (channel_id,))
    send("info", "AI Flow notification test", "Confirm that this channel receives AI Flow alerts.",
         "/notifications", ctx=ctx, alert_kind="daily_summary", project="AI Flow", ticket="Notification test",
         needed="Confirm this channel receives alerts.", is_test=True, channel_id=channel_id)
    latest = ctx.db.one("SELECT status, attempts, max_attempts, last_error, updated_at FROM notification_deliveries"
                        " WHERE channel_id=%s AND is_test=true AND id>%s ORDER BY id DESC LIMIT 1",
                        (channel_id, previous["id"] if previous else 0))
    return {"channel": public_channel(_get_channel(ctx.db, channel_id)),
            "result": {"status": latest["status"] if latest else "failed",
                       "attempts": int(latest["attempts"]) if latest else 0,
                       "max_attempts": int(latest["max_attempts"]) if latest else MAX_ATTEMPTS,
                       "error": latest["last_error"] if latest else "delivery could not be queued",
                       "at": _iso(latest["updated_at"]) if latest else None}}


def process_retries(ctx, limit: int = 25) -> int:
    """Retry due deliveries and reclaim sends interrupted for at least one minute."""
    limit = max(1, min(limit, 100))
    # Claim stale sending rows by refreshing updated_at. This makes recovery single-owner even if two control
    # loops briefly overlap, while leaving enough time for a normal provider request to finish.
    interrupted = ctx.db.all(
        "WITH stale AS (SELECT id FROM notification_deliveries WHERE status='sending'"
        " AND updated_at<=now()-(%s * interval '1 second') ORDER BY updated_at,id"
        " FOR UPDATE SKIP LOCKED LIMIT %s)"
        " UPDATE notification_deliveries AS delivery SET updated_at=now() FROM stale"
        " WHERE delivery.id=stale.id RETURNING delivery.*",
        (SENDING_RECOVERY_AFTER_S, limit))
    rows = ctx.db.all("SELECT * FROM notification_deliveries WHERE status='pending' AND next_attempt_at<=now()"
                      " ORDER BY next_attempt_at,id LIMIT %s", (max(0, limit - len(interrupted)),))
    processed = 0
    for delivery in interrupted:
        channel = _get_channel(ctx.db, int(delivery["channel_id"])) if delivery.get("channel_id") else None
        if channel:
            error = "DeliveryInterrupted"
            attempts = int(delivery["attempts"]) + 1
            log.warning("notification delivery interrupted channel_id=%s", channel["id"])
            _record_attempt_failure(ctx.db, channel, delivery, error, attempts)
        else:
            missing_channel = {"id": int(delivery.get("channel_id") or 0), "name": delivery["channel_name"]}
            error = "DeliveryInterrupted"
            attempts = int(delivery["attempts"]) + 1
            _record_attempt_failure(ctx.db, missing_channel, delivery, error, attempts)
        processed += 1
    for delivery in rows:
        channel = _get_channel(ctx.db, int(delivery["channel_id"])) if delivery.get("channel_id") else None
        if not channel:
            ctx.db.run("UPDATE notification_deliveries SET status='failed',last_error='channel removed',updated_at=now()"
                       " WHERE id=%s", (delivery["id"],))
            continue
        _attempt(ctx.db, channel, delivery)
        processed += 1
    return processed


def _summary(ctx, start: datetime, end: datetime, period: str, label: str) -> tuple[str, str]:
    rows = ctx.db.all("SELECT ticket_key, project, outcome, summary, cost_usd FROM runs WHERE ended_at >= %s"
                      " AND ended_at < %s ORDER BY ended_at DESC", (start, end))
    finished = [row for row in rows if row["outcome"] in ("done", "merged")]
    failed = [row for row in rows if row["outcome"] in ("failed", "timeout", "lost")]
    waiting = ctx.db.all("SELECT key, project, summary, status FROM tickets WHERE status IN ('Needs owner',"
                         " 'Human review') ORDER BY jira_updated DESC NULLS LAST LIMIT 10")
    cost = sum(float(row["cost_usd"] or 0) for row in rows)

    def items(values: list[dict]) -> str:
        if not values:
            return "none"
        shown = [f"{row.get('project') or 'AI Flow'}/{row.get('ticket_key') or row.get('key')}: "
                 f"{_text(row.get('summary'), 100)}" for row in values[:5]]
        if len(values) > 5:
            shown.append(f"and {len(values) - 5} more")
        return "; ".join(shown)

    title = f"{period} summary for {label}"
    detail = (f"Finished: {items(finished)}\nNeeds a person: {items(waiting)}\nFailed: {items(failed)}\n"
              f"Spend: ${cost:.2f} USD")
    return title, detail


def summary_content(ctx, day: date | None = None) -> str:
    """Build the daily summary text: completed work, tickets needing a person, failures and spend."""
    day = day or (datetime.now(timezone.utc).date() - timedelta(days=1))
    start = datetime.combine(day, wall_time.min, tzinfo=timezone.utc)
    title, detail = _summary(ctx, start, start + timedelta(days=1), "Daily", day.isoformat())
    return f"Project: Multiple projects\nTicket: Daily summary {day.isoformat()}\nNeeded: Review work and items awaiting a person.\n{detail}"


def send_scheduled_summaries(ctx, now: datetime | None = None) -> list[str]:
    """Send yesterday's daily summary and last week's report once, at 00:05 UTC or later."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.hour == 0 and now.minute < 5:
        return []
    today = now.date()
    daily_for = today - timedelta(days=1)
    daily = ctx.db.one("UPDATE notification_schedules SET last_daily_date=%s WHERE id=true"
                       " AND (last_daily_date IS NULL OR last_daily_date < %s) RETURNING id", (daily_for, daily_for))
    sent: list[str] = []
    if daily:
        start = datetime.combine(daily_for, wall_time.min, tzinfo=timezone.utc)
        title, detail = _summary(ctx, start, start + timedelta(days=1), "Daily", daily_for.isoformat())
        sent.extend(send("info", title, detail, "/delivery", ctx=ctx, alert_kind="daily_summary",
                         project="Multiple projects", ticket=f"Daily summary {daily_for.isoformat()}",
                         needed="Review work and items awaiting a person."))
    if today.weekday() == 0:
        week_for = today - timedelta(days=7)
        weekly = ctx.db.one("UPDATE notification_schedules SET last_weekly_monday=%s WHERE id=true"
                            " AND (last_weekly_monday IS NULL OR last_weekly_monday < %s) RETURNING id",
                            (week_for, week_for))
        if weekly:
            start = datetime.combine(week_for, wall_time.min, tzinfo=timezone.utc)
            title, detail = _summary(ctx, start, start + timedelta(days=7), "Weekly", week_for.isoformat())
            sent.extend(send("info", title, detail, "/delivery", ctx=ctx, alert_kind="weekly_report",
                             project="Multiple projects", ticket=f"Weekly report {week_for.isoformat()}",
                             needed="Review the week's completed work, blockers, failures and spend."))
    return sent
