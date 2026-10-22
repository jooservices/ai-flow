from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from aiflow.v4.control import notify
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import mount_ui
from tests.v4.conftest import add_ticket


SECRET = "notifications-test-session-secret-32-bytes"


def sign_in(client, ctx, role="owner"):
    ctx.settings.console_session_secret = SECRET
    token = issue_session("admin@example.test", role, int(datetime.now(timezone.utc).timestamp()) + 3600, SECRET)
    client.cookies.set(COOKIE_NAME, token)


def add_channel(client, kind, config, name=None):
    response = client.put("/v1/notifications/channels", json={"name": name or kind.title(), "kind": kind,
                                                               "config": config})
    assert response.status_code == 200, response.text
    return response.json()["channel"]


def fake_http(monkeypatch, posted):
    class Response:
        def raise_for_status(self):
            return None

    def post(url, **kwargs):
        posted.append((url, kwargs))
        return Response()

    monkeypatch.setattr(notify.httpx, "post", post)


def test_admin_screen_channel_test_results_and_hidden_credentials(client, ctx, monkeypatch):
    sign_in(client, ctx)
    mount_ui(client.app, ctx)
    page = client.get("/notifications")
    assert page.status_code == 200 and "Notifications" in page.text

    posted, mail = [], []
    fake_http(monkeypatch, posted)

    class SMTP:
        def __init__(self, host, port, timeout):
            mail.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            return None

        def login(self, username, password):
            mail.append((username, password))

        def send_message(self, message):
            mail.append((message["To"], message.get_content()))

    monkeypatch.setattr(notify.smtplib, "SMTP", SMTP)
    specs = [
        ("email", {"host": "smtp.example.test", "port": 2525, "username": "mailer", "password": "smtp-secret",
                   "from": "flow@example.test", "to": "owner@example.test", "tls_mode": "starttls"}),
        ("telegram", {"bot_token": "telegram-secret", "chat_id": "-10042"}),
        ("slack", {"webhook_url": "https://hooks.slack.test/secret-path"}),
        ("teams", {"webhook_url": "https://teams.test/secret-path"}),
        ("webhook", {"url": "https://hooks.example.test/secret?key=hidden", "auth_token": "webhook-secret"}),
    ]
    channels = [add_channel(client, kind, config) for kind, config in specs]
    for channel in channels:
        response = client.post(f"/v1/notifications/channels/{channel['id']}/test")
        assert response.status_code == 200, response.text
        assert response.json()["result"]["status"] == "sent"
        assert response.json()["channel"]["last_delivery"]["status"] == "sent"

    assert mail[0] == ("smtp.example.test", 2525)
    assert mail[1] == ("mailer", "smtp-secret")
    assert len(posted) == 4
    assert "telegram-secret" in posted[0][0]
    assert posted[1][0] == "https://hooks.slack.test/secret-path"
    assert posted[2][0] == "https://teams.test/secret-path"
    assert posted[3][0] == "https://hooks.example.test/secret?key=hidden"
    assert posted[3][1]["headers"]["Authorization"] == "Bearer webhook-secret"

    response = client.get("/v1/notifications")
    assert response.status_code == 200
    encoded = response.text
    for secret in ("smtp-secret", "telegram-secret", "secret-path", "key=hidden", "webhook-secret"):
        assert secret not in encoded
    result = response.json()
    assert {channel["kind"] for channel in result["channels"]} == set(notify.CHANNEL_KINDS)
    assert all(channel["last_delivery"]["status"] == "sent" for channel in result["channels"])

    sign_in(client, ctx, "lead")
    assert client.get("/v1/notifications").status_code == 403
    assert client.get("/notifications").status_code == 403


def test_route_selection_for_each_alert_kind_and_quiet_hours(client, ctx, monkeypatch):
    sign_in(client, ctx)
    posted = []
    fake_http(monkeypatch, posted)
    channel_rows = [add_channel(client, "webhook", {"url": f"https://notify-{i}.example.test/hook"}, f"route-{i}")
                    for i in range(len(notify.ALERT_KINDS))]
    routes = {kind: [channel_rows[index]["id"]] for index, kind in enumerate(notify.ALERT_KINDS)}
    response = client.put("/v1/notifications/preferences", json={"routes": routes, "quiet_start": "", "quiet_end": ""})
    assert response.status_code == 200

    for kind in notify.ALERT_KINDS:
        notify.send("critical" if kind == "critical_incident" else "warning", f"Alert {kind}", "Please review",
                    ctx=ctx, alert_kind=kind)
    assert [item[0] for item in posted] == [f"https://notify-{i}.example.test/hook" for i in range(5)]

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(notify, "datetime", FixedClock)
    response = client.put("/v1/notifications/preferences", json={"routes": routes,
                          "quiet_start": "11:00", "quiet_end": "13:00"})
    assert response.status_code == 200
    before = len(posted)
    notify.send("warning", "Quiet warning", "Needs attention", ctx=ctx, alert_kind="ticket_needs_person")
    assert len(posted) == before
    assert ctx.db.one("SELECT status,attempts,last_error FROM notification_deliveries ORDER BY id DESC LIMIT 1") == {
        "status": "pending", "attempts": 0, "last_error": ""}
    # Critical notifications pass through quiet hours.
    notify.send("critical", "Urgent incident", "Act now", ctx=ctx, alert_kind="critical_incident")
    assert len(posted) == before + 1


def test_repeated_delivery_failure_retries_and_opens_screen_incident(client, ctx, monkeypatch, caplog):
    sign_in(client, ctx)
    channel = add_channel(client, "webhook", {"url": "https://hidden.example.test/token-path"}, "broken webhook")
    client.put("/v1/notifications/preferences", json={"routes": {"critical_incident": [channel["id"]]}})

    def fail(url, **kwargs):
        raise httpx.ConnectError("failed https://hidden.example.test/token-path")

    monkeypatch.setattr(notify.httpx, "post", fail)
    assert notify.send("critical", "TESTWP-91 broken", "Fix the connection", ctx=ctx,
                       alert_kind="critical_incident") == []
    for _ in range(3):
        ctx.db.run("UPDATE notification_deliveries SET next_attempt_at=now() WHERE status='pending'")
        assert notify.process_retries(ctx) == 1
    delivery = ctx.db.one("SELECT status, attempts, last_error FROM notification_deliveries ORDER BY id DESC LIMIT 1")
    assert delivery == {"status": "failed", "attempts": 4, "last_error": "ConnectError"}
    assert ctx.db.one("SELECT kind, severity FROM incidents WHERE kind='notification_delivery' AND closed_at IS NULL") == {
        "kind": "notification_delivery", "severity": "critical"}
    assert "token-path" not in caplog.text
    screen = client.get("/v1/notifications").json()
    assert screen["incidents"] and screen["incidents"][0]["title"] == "Alert delivery failed: broken webhook"


def test_alert_content_has_project_ticket_needed_and_exact_action_link(ctx, monkeypatch):
    add_ticket(ctx.db, "TESTWP-92")
    channel_id = notify.save_channel(ctx.db, {"name": "content hook", "kind": "webhook",
                                              "config": {"url": "https://content.example.test/hook"}})["id"]
    notify.save_preferences(ctx.db, {"routes": {"ticket_needs_person": [channel_id]}})
    posted = []
    fake_http(monkeypatch, posted)
    notify.send("warning", "TESTWP-92 needs owner decision", "Choose whether to proceed.",
                "https://jira.example.test/browse/TESTWP-92", ctx=ctx, alert_kind="ticket_needs_person",
                needed="Choose whether to proceed.")
    data = posted[0][1]["json"]
    assert "Project: TESTWP" in data["text"]
    assert "Ticket: TESTWP-92 — Summary of TESTWP-92" in data["text"]
    assert "Needed: Choose whether to proceed." in data["text"]
    assert data["link"] == "https://jira.example.test/browse/TESTWP-92"
    assert "https://jira.example.test/browse/TESTWP-92" in data["text"]


def test_daily_summary_has_finished_waiting_failed_and_spend(ctx):
    today = datetime.now(timezone.utc).date()
    add_ticket(ctx.db, "TESTWP-93", status="Human review")
    for key, outcome, summary, spend in (("TESTWP-94", "done", "Finished task", 1.25),
                                         ("TESTWP-95", "failed", "Failed task", 0.75)):
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,node,fence,status,outcome,started_at,"
                   "ended_at,cost_usd,summary) VALUES (%s,%s,'TESTWP','dev','claude-01','cc',1,'finished',%s,"
                   "now(),now(),%s,%s)", (uuid.uuid4(), key, outcome, spend, summary))
        add_ticket(ctx.db, key)
    summary = notify.summary_content(ctx, today)
    assert "Finished: TESTWP/TESTWP-94: Finished task" in summary
    assert "Needs a person: TESTWP/TESTWP-93: Summary of TESTWP-93" in summary
    assert "Failed: TESTWP/TESTWP-95: Failed task" in summary
    assert "Spend: $2.00 USD" in summary
    assert "Project: Multiple projects" in summary and f"Ticket: Daily summary {today.isoformat()}" in summary


def test_notify_and_deploy_scripts_have_no_legacy_environment_or_local_mail_path():
    root = Path(__file__).resolve().parents[2]
    files = [root / "aiflow/v4/control/notify.py", root / "deploy/v4/bin/aiflow4-autodeploy",
             root / "deploy/v4/bin/aiflow4-autodeploy-develop", root / "deploy/v4/control/compose.yaml"]
    legacy = ("AIFLOW4_SMTP_HOST", "AIFLOW4_SMTP_PORT", "AIFLOW4_SMTP_FROM", "AIFLOW4_ALERT_EMAIL",
              "AIFLOW4_TELEGRAM_BOT_TOKEN", "AIFLOW4_TELEGRAM_CHAT_ID", "AIFLOW4_SLACK_WEBHOOK",
              "AIFLOW4_TEAMS_WEBHOOK", "owner@aiflow4.local", "11025")
    for path in files:
        contents = path.read_text(encoding="utf-8")
        assert not any(value in contents for value in legacy), path

