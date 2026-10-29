"""Environment-only owner notification channels and persistent hourly grouping."""
from __future__ import annotations

import json
import ssl
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aiflow.v4.control import escalations
from aiflow.v4.control import owner_notifications
from aiflow.v4.core.settings import Settings


def _pending_decision(ctx, ticket: str, question: str, decision_id: int | None = None) -> int:
    if decision_id is None:
        decision = ctx.db.one(
            "INSERT INTO owner_decisions (topic,status,title,question,source,ticket_key,waiting_tickets)"
            " VALUES (%s,'asked',%s,%s,'ask',%s,%s) RETURNING id",
            (f"notification-{ticket.lower()}", question, question, ticket, [ticket]))
        decision_id = decision["id"]
    else:
        ctx.db.run("UPDATE owner_decisions SET waiting_tickets=array_append(waiting_tickets,%s) WHERE id=%s",
                   (ticket, decision_id))
    escalations.record(ctx.db, ticket, "TESTWP", "triage", f"notice-{ticket}", escalations.BUSINESS,
                       question, "pending notification test", "open", decision_id)
    return decision_id


def test_no_channel_is_a_noop(ctx):
    assert owner_notifications.sender_for(ctx.settings) is None
    result = owner_notifications.enqueue(ctx, 1, "TESTWP-1", "Should the button be blue?")
    assert result == {"status": "unconfigured", "sent": False}
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0


def test_notice_link_uses_the_active_jira_connections_site_url(ctx, monkeypatch):
    messages = []

    class CaptureSender:
        def send(self, message):
            messages.append(message)

    ctx.settings.jira_site = "https://settings-site.invalid"
    ctx.jira_auth = SimpleNamespace(status=lambda: {"site_url": "https://connected-site.atlassian.net"})
    monkeypatch.setattr(owner_notifications, "sender_for", lambda _settings: CaptureSender())

    question = "Should we keep this behavior?"
    decision_id = _pending_decision(ctx, "TESTWP-42", question)
    escalations.record(ctx.db, "TESTWP-42", "TESTWP", "triage", "notice-retry-TESTWP-42",
                       escalations.BUSINESS, question, "duplicate active row", "open", decision_id)
    result = owner_notifications.enqueue(ctx, decision_id, "TESTWP-42", question)

    expected_link = "https://connected-site.atlassian.net/browse/TESTWP-42"
    notice = ctx.db.one("SELECT ticket_url FROM owner_escalation_notices WHERE ticket_key='TESTWP-42'")
    assert result["sent"] is True and result["grouped"] == 1
    assert notice["ticket_url"] == expected_link
    assert messages == [f"Should we keep this behavior?\n{expected_link}"]


def test_notice_is_rejected_when_the_ticket_is_not_waiting_for_the_decision(ctx):
    question = "Should CSV or JSON be the default?"
    asked = escalations.ask(ctx, "TESTWP-2", "TESTWP", question, "The choice affects users.",
                            ["CSV", "JSON"], "CSV", "Export format", ["export", "format"])
    escalations.settle(ctx.db, "TESTWP-2", "TESTWP", "triage", "run-2", escalations.BUSINESS, question,
                       "The choice affects users.", "person", asked["decision"]["id"], reached_person=True)

    result = owner_notifications.enqueue_for_run(
        ctx, asked["decision"]["id"], {"ticket_key": "TESTWP-1"})

    assert result is None
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0


def test_settings_load_optional_destinations_and_secret_from_env(monkeypatch):
    monkeypatch.delenv("AIFLOW4_SECRET_FILES", raising=False)
    monkeypatch.setenv("AIFLOW4_ESCALATION_EMAIL", "owner@")
    monkeypatch.setenv("AIFLOW4_ESCALATION_SMTP_HOST", "configured-smtp")
    monkeypatch.setenv("AIFLOW4_ESCALATION_SMTP_PASSWORD", "smtp-test-secret")
    monkeypatch.setenv("AIFLOW4_ESCALATION_TELEGRAM_CHAT_ID", "-10042")
    monkeypatch.setenv("AIFLOW4_ESCALATION_TELEGRAM_BOT_TOKEN", "telegram-test-secret")

    settings = Settings.from_env()

    assert settings.escalation_email == "owner@"
    assert settings.escalation_smtp_host == "configured-smtp"
    assert settings.escalation_smtp_password == "smtp-test-secret"
    assert settings.escalation_telegram_chat_id == "-10042"
    assert settings.escalation_telegram_bot_token == "telegram-test-secret"


def test_smtp_sender_delivers_only_the_question_and_link(monkeypatch):
    settings = Settings(escalation_email="owner@example.test", escalation_smtp_host="configured-smtp",
                        escalation_smtp_from="flow@example.test", escalation_smtp_username="flow-user",
                        escalation_smtp_password="smtp-test-secret")
    messages: list[EmailMessage] = []

    class SMTP:
        def __init__(self, host, port, timeout, **_kwargs):
            assert (host, port, timeout) == ("configured-smtp", 587, 10)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, **_kwargs):
            pass

        def login(self, username, password):
            assert (username, password) == ("flow-user", "smtp-test-secret")

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr(owner_notifications.smtplib, "SMTP", SMTP)
    message_body = "Should we keep the old response?\n" + owner_notifications.ticket_link(settings.jira_site, "JAIF-1")
    owner_notifications.sender_for(settings).send(message_body)

    assert len(messages) == 1
    assert messages[0]["Subject"] == "Should we keep the old response?"
    assert messages[0].get_content().strip() == message_body
    assert "smtp-test-secret" not in messages[0].as_string()


class SmtpTlsValidationTests(unittest.TestCase):
    def test_tls_modes_use_default_context_with_certificate_and_hostname_validation(self):
        real_create_default_context = ssl.create_default_context
        for mode in ("starttls", "ssl"):
            with self.subTest(mode=mode):
                contexts = {}
                settings = Settings(escalation_email="owner@example.test", escalation_smtp_host="configured-smtp",
                                    escalation_smtp_tls_mode=mode)

                class SMTP:
                    def __init__(self, _host, _port, timeout, **kwargs):
                        self.assert_timeout = timeout
                        contexts["constructor"] = kwargs.get("context")

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def starttls(self, *, context):
                        contexts["starttls"] = context

                    def send_message(self, _message):
                        pass

                with (patch.object(owner_notifications.smtplib, "SMTP", SMTP),
                      patch.object(owner_notifications.smtplib, "SMTP_SSL", SMTP),
                      patch.object(owner_notifications.ssl, "create_default_context",
                                   wraps=real_create_default_context) as create_context):
                    owner_notifications.SmtpSender(settings).send("Should we proceed?\nhttps://jira.invalid/browse/JAIF-1")

                context = contexts["constructor"] if mode == "ssl" else contexts["starttls"]
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                create_context.assert_called_once_with()


def test_smtp_authentication_is_rejected_without_tls_before_connecting(monkeypatch):
    settings = Settings(escalation_email="owner@example.test", escalation_smtp_host="configured-smtp",
                        escalation_smtp_username="flow-user", escalation_smtp_password="smtp-test-secret",
                        escalation_smtp_tls_mode="none")

    def connect(*_args, **_kwargs):
        raise AssertionError("SMTP must reject the configuration before connecting")

    monkeypatch.setattr(owner_notifications.smtplib, "SMTP", connect)

    with pytest.raises(ValueError, match="SMTP authentication requires TLS"):
        owner_notifications.sender_for(settings)

    with pytest.raises(ValueError, match="SMTP authentication requires TLS"):
        owner_notifications.SmtpSender(settings).send("Should we proceed?\nhttps://jira.invalid/browse/JAIF-1")


def test_insecure_smtp_configuration_is_a_sanitized_noop_for_notification_calls(ctx, caplog):
    ctx.settings.escalation_email = "owner@example.test"
    ctx.settings.escalation_smtp_host = "configured-smtp"
    ctx.settings.escalation_smtp_username = "flow-user"
    ctx.settings.escalation_smtp_password = "smtp-test-secret"
    ctx.settings.escalation_smtp_tls_mode = "none"

    enqueued = owner_notifications.enqueue(ctx, 19, "TESTWP-19", "Should we keep this choice?")
    flushed = owner_notifications.process_pending(ctx)

    assert enqueued == flushed == {"status": "failed", "sent": False}
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices")["n"] == 0
    assert "smtp-test-secret" not in caplog.text


def test_telegram_sender_keeps_credentials_out_of_the_message(monkeypatch):
    settings = Settings(escalation_telegram_chat_id="-10042", escalation_telegram_bot_token="telegram-test-secret")
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(owner_notifications, "urlopen", urlopen)
    message_body = "Should we ship this?\n" + owner_notifications.ticket_link(settings.jira_site, "JAIF-2")
    owner_notifications.sender_for(settings).send(message_body)

    request, timeout = requests[0]
    payload = json.loads(request.data)
    assert timeout == 10 and payload == {"chat_id": "-10042", "text": message_body}
    assert "telegram-test-secret" not in payload["text"]


def test_pending_questions_are_grouped_and_sent_at_most_once_per_hour(ctx, monkeypatch):
    messages = []

    class CaptureSender:
        def send(self, message):
            messages.append(message)

    monkeypatch.setattr(owner_notifications, "sender_for", lambda _settings: CaptureSender())
    start = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    first_question = "Should the default be CSV?"
    second_question = "Should the default be JSON?"
    third_question = "Should the default be YAML?"
    first_id = _pending_decision(ctx, "TESTWP-1", first_question)
    second_id = _pending_decision(ctx, "TESTWP-2", second_question)
    third_id = _pending_decision(ctx, "TESTWP-3", third_question)
    first = owner_notifications.enqueue(ctx, first_id, "TESTWP-1", first_question, now=start)
    second = owner_notifications.enqueue(ctx, second_id, "TESTWP-2", second_question,
                                         now=start + timedelta(minutes=10))
    third = owner_notifications.enqueue(ctx, third_id, "TESTWP-3", third_question,
                                        now=start + timedelta(minutes=20))
    early = owner_notifications.process_pending(ctx, now=start + timedelta(minutes=59))
    later = owner_notifications.process_pending(ctx, now=start + timedelta(hours=1))

    assert first["status"] == "sent" and first["grouped"] == 1
    assert second["status"] == third["status"] == early["status"] == "queued"
    assert later["status"] == "sent" and later["grouped"] == 2
    assert len(messages) == 2
    assert messages[0] == "Should the default be CSV?\n" + owner_notifications.ticket_link(
        ctx.settings.jira_site, "TESTWP-1")
    assert messages[1] == ("Should the default be JSON?\n"
                           + owner_notifications.ticket_link(ctx.settings.jira_site, "TESTWP-2")
                           + "\n\nShould the default be YAML?\n"
                           + owner_notifications.ticket_link(ctx.settings.jira_site, "TESTWP-3"))
    assert ctx.db.one("SELECT count(*) AS n FROM owner_escalation_notices WHERE sent_at IS NOT NULL")["n"] == 3


def test_hourly_flush_skips_notices_for_answered_grouped_decisions(ctx, monkeypatch):
    messages = []

    class CaptureSender:
        def send(self, message):
            messages.append(message)

    monkeypatch.setattr(owner_notifications, "sender_for", lambda _settings: CaptureSender())
    start = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
    seed_question = "Should the seed notice be sent?"
    seed_id = _pending_decision(ctx, "TESTWP-10", seed_question)
    seed = owner_notifications.enqueue(ctx, seed_id, "TESTWP-10", seed_question, now=start)
    question = "Should a grouped notice still be sent after resolution?"
    first_id = _pending_decision(ctx, "TESTWP-11", question)
    second_id = _pending_decision(ctx, "TESTWP-12", question, first_id)
    assert second_id == first_id
    first = owner_notifications.enqueue(ctx, first_id, "TESTWP-11", question,
                                       now=start + timedelta(minutes=5))
    second = owner_notifications.enqueue(ctx, second_id, "TESTWP-12", question,
                                         now=start + timedelta(minutes=10))

    assert seed["status"] == "sent"
    assert first["status"] == second["status"] == "queued"
    escalations._store_owner_answer(ctx.db, first_id, "Use option A", "owner@example.test")
    assert ctx.db.one("SELECT status FROM owner_decisions WHERE id=%s", (first_id,))["status"] == "decided"
    assert {row["resolution"] for row in ctx.db.all(
        "SELECT resolution FROM escalations WHERE decision_id=%s", (first_id,))} == {"resuming"}

    flushed = owner_notifications.process_pending(ctx, now=start + timedelta(hours=1))

    assert flushed == {"status": "empty", "sent": False}
    assert messages == [seed_question + "\n" + owner_notifications.ticket_link(ctx.settings.jira_site, "TESTWP-10")]
    pending = ctx.db.all("SELECT sent_at FROM owner_escalation_notices WHERE decision_id=%s", (first_id,))
    assert len(pending) == 2 and all(row["sent_at"] is None for row in pending)


def test_failed_delivery_stays_queued_and_recovers_after_the_rate_limit(ctx, monkeypatch):
    messages = []

    class RetrySender:
        def send(self, message):
            messages.append(message)
            if len(messages) == 1:
                raise RuntimeError("temporary delivery failure")

    monkeypatch.setattr(owner_notifications, "sender_for", lambda _settings: RetrySender())
    start = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)

    question = "Should we keep this choice?"
    decision_id = _pending_decision(ctx, "TESTWP-7", question)
    failed = owner_notifications.enqueue(ctx, decision_id, "TESTWP-7", question, now=start)
    queued = owner_notifications.process_pending(ctx, now=start + timedelta(minutes=59))
    recovered = owner_notifications.process_pending(ctx, now=start + timedelta(hours=1))

    notice = ctx.db.one("SELECT sent_at FROM owner_escalation_notices WHERE decision_id=%s AND ticket_key='TESTWP-7'",
                        (decision_id,))
    assert failed == {"status": "failed", "sent": False}
    assert queued == {"status": "queued", "sent": False}
    assert recovered == {"status": "sent", "sent": True, "grouped": 1}
    assert messages == [question + "\n" + owner_notifications.ticket_link(
        ctx.settings.jira_site, "TESTWP-7")] * 2
    assert notice["sent_at"] is not None


def test_incomplete_email_addresses_leave_the_email_channel_unconfigured():
    for owner, sender in (("owner@", "flow@example.test"), ("owner@example.test", "flow@"), ("owner", "flow@example.test"),
                          ("owner@example", "flow@example.test")):
        settings = Settings(escalation_email=owner, escalation_smtp_host="configured-smtp", escalation_smtp_from=sender)
        assert owner_notifications.sender_for(settings) is None


def test_a_question_that_holds_a_credential_is_withheld_from_both_channels(ctx, monkeypatch):
    sent: list[str] = []

    class Channel:
        def send(self, message):
            sent.append(message)

    monkeypatch.setattr(owner_notifications, "sender_for", lambda settings: Channel())
    secret_question = "Which database should we use? password=" + "hunter2hunter2"
    result = owner_notifications.enqueue(ctx, 1, "TESTWP-1", secret_question)
    assert result == {"status": "withheld", "sent": False} and sent == []
    ctx.settings.escalation_telegram_bot_token, ctx.settings.escalation_telegram_chat_id = "t", "1"
    assert owner_notifications.enqueue(ctx, 1, "TESTWP-1", secret_question)["status"] == "withheld" and sent == []
