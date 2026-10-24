from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiflow.v4.control import ba_chat, notify
from aiflow.v4.control.console_auth import COOKIE_NAME, issue_session
from aiflow.v4.control.ui import mount_ui
from aiflow.v4.core.controlrepo import ControlRepo


BOT_TOKEN = "fake-telegram-bot-token"
CHAT_ID = "77123"


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.incoming = []

    def updates(self, token, offset):
        assert token == BOT_TOKEN
        batch = [update for update in self.incoming if update["update_id"] >= offset]
        self.incoming = []
        return batch

    def send(self, token, chat_id, body):
        assert token == BOT_TOKEN and chat_id == CHAT_ID
        self.sent.append(body)


def channel(ctx):
    notify.save_channel(ctx.db, {"kind": "telegram", "name": "Owner brainstorm", "enabled": True,
                                 "config": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID,
                                            "ba_chat_enabled": True, "project_key": "TESTWP"}})
    return ctx.db.one("SELECT * FROM notification_channels WHERE kind='telegram'")


def message(update_id, text, chat_id=CHAT_ID, chat_type="private", sender_id=None):
    return {"update_id": update_id, "message": {"text": text,
            "chat": {"id": int(chat_id), "type": chat_type},
            "from": {"id": int(sender_id if sender_id is not None else chat_id)}}}


def item(title, description, *, tasks=None):
    result = {"title": title, "description": description,
              "acceptance_criteria": [f"The owner can verify {title.lower()}"],
              "technical_notes": "Use the existing AI Flow services.",
              "definition_of_done": ["The change is tested and documented."]}
    if tasks is not None:
        result["tasks"] = tasks
    return result


def proposal(*, epic="Planning", story="Capture scope", tasks=None):
    return {"summary": "Plan a product change", "epics": [
        {**item(epic, "Organise the planned product change."),
         "stories": [item(story, "As an owner, I can capture the agreed scope.", tasks=tasks or [])]}]}


def run_job(ctx, expected_account="claude-01", node="cc", output=None, error_kind=""):
    job = ba_chat.claim_job(ctx, expected_account, node)
    assert job is not None
    assert ba_chat.complete_job(ctx, job["job_id"], job["claim_token"], expected_account, node,
                               output or {}, error_kind)
    return job


def test_reclaimed_chat_job_rejects_the_previous_claim_result_even_for_the_same_worker(ctx):
    channel(ctx)
    assert ba_chat.accept_update(ctx, ctx.db.one("SELECT * FROM notification_channels WHERE kind='telegram'"),
                                 message(5, "Plan a billing improvement"))
    old_claim = ba_chat.claim_job(ctx, "claude-01", "cc")
    assert old_claim is not None
    ctx.db.run("UPDATE ba_chat_jobs SET claim_until=clock_timestamp()-interval '1 second' WHERE id=%s",
               (old_claim["job_id"],))

    new_claim = ba_chat.claim_job(ctx, "claude-01", "cc")
    assert new_claim is not None and new_claim["job_id"] == old_claim["job_id"]
    assert new_claim["claim_token"] != old_claim["claim_token"]
    stale_proposal = {"mode": "proposal", "proposal": proposal()}
    assert not ba_chat.complete_job(ctx, old_claim["job_id"], old_claim["claim_token"],
                                    "claude-01", "cc", stale_proposal)
    assert ctx.db.one("SELECT status FROM ba_chat_conversations")["status"] == "collecting"
    assert ctx.db.one("SELECT count(*) AS n FROM ba_chat_outbox")["n"] == 0

    assert ba_chat.complete_job(ctx, new_claim["job_id"], new_claim["claim_token"],
                                "claude-01", "cc", stale_proposal)
    assert ctx.db.one("SELECT status FROM ba_chat_conversations")["status"] == "proposal"


def test_private_chat_asks_then_proposes_and_only_approval_creates_template_tickets(ctx):
    channel_row = channel(ctx)
    telegram = FakeTelegram()
    telegram.incoming = [message(1, "Make refund status clearer")]
    assert ba_chat.poll(ctx, telegram) == 1
    run_job(ctx, output={"mode": "question", "reply": "Which refund status should the owner see?"})
    assert not getattr(ctx.jira, "created", [])

    telegram.incoming = [message(2, "Show whether it is pending, paid, or rejected.")]
    assert ba_chat.poll(ctx, telegram) == 1
    run_job(ctx, output={"mode": "proposal", "summary": "Clarify refund outcomes.",
                         "proposal": proposal(tasks=[item("Add status copy", "Write the status labels.")])})
    conversation = ctx.db.one("SELECT status,proposal,history FROM ba_chat_conversations")
    assert conversation["status"] == "proposal"
    assert "What I understood:" in ctx.db.one("SELECT body FROM ba_chat_outbox ORDER BY id DESC LIMIT 1")["body"]
    assert "Epic 1: Planning" in ctx.db.one("SELECT body FROM ba_chat_outbox ORDER BY id DESC LIMIT 1")["body"]
    assert not getattr(ctx.jira, "created", [])  # a proposal is still only a draft

    assert ba_chat.accept_update(ctx, channel_row, message(3, "/approve"))
    assert [issue["issuetype"] for issue in ctx.jira.created] == ["Epic", "Story", "Sub-task"]
    epic, story, task = ctx.jira.created
    assert story["parent"] == epic["key"] and task["parent"] == story["key"]
    for issue in ctx.jira.created:
        description = issue["description"]
        assert "Business requirement:" in description
        assert "Acceptance criteria:\n- AC1:" in description
        assert "Technical notes:" in description and "Definition of done:" in description

    assert ba_chat.poll(ctx, telegram) == 0
    sent = "\n".join(telegram.sent)
    assert "Which refund status should the owner see?" in sent
    assert "Capture scope" in sent and "As an owner, I can capture the agreed scope." in sent
    assert "Add status copy" in sent and "Write the status labels." in sent
    for issue in ctx.jira.created:
        assert issue["key"] in sent and f"{ctx.settings.jira_site}/browse/{issue['key']}" in sent
    assert BOT_TOKEN not in sent


def test_rejection_and_change_request_do_not_create_tickets_and_revision_is_sent(ctx):
    row = channel(ctx)
    assert ba_chat.accept_update(ctx, row, message(10, "Plan a reporting screen"))
    run_job(ctx, output={"mode": "proposal", "summary": "Initial plan", "proposal": proposal()})
    assert not getattr(ctx.jira, "created", [])
    assert ba_chat.accept_update(ctx, row, message(11, "/reject"))
    assert not getattr(ctx.jira, "created", [])
    assert ctx.db.one("SELECT status FROM ba_chat_conversations")["status"] == "rejected"

    assert ba_chat.accept_update(ctx, row, message(12, "Plan a reporting screen"))
    run_job(ctx, output={"mode": "proposal", "summary": "Initial plan", "proposal": proposal()})
    assert ba_chat.accept_update(ctx, row, message(13, "Add a story for downloading a CSV."))
    revised = proposal(epic="Reporting", story="Download a report")
    run_job(ctx, output={"mode": "proposal", "summary": "Plan including CSV export", "proposal": revised})
    convo = ctx.db.one("SELECT status,proposal FROM ba_chat_conversations")
    assert convo["status"] == "proposal" and convo["proposal"]["epics"][0]["title"] == "Reporting"
    latest_reply = ctx.db.one("SELECT body FROM ba_chat_outbox ORDER BY id DESC LIMIT 1")["body"]
    assert "Plan including CSV export" in latest_reply and "Download a report" in latest_reply
    assert not getattr(ctx.jira, "created", [])


def test_jira_refusal_is_reported_and_independent_tickets_are_still_created(ctx):
    row = channel(ctx)
    telegram = FakeTelegram()
    multi = {"summary": "Two independent plans", "epics": [
        {**item("First epic", "First outcome"), "stories": [item("Refused story", "This issue is refused.")]},
        {**item("Second epic", "Second outcome"), "stories": [item("Independent story", "This one can proceed.")]}]}
    assert ba_chat.accept_update(ctx, row, message(20, "Plan two improvements"))
    run_job(ctx, output={"mode": "proposal", "summary": "Two plans", "proposal": multi})
    assert not getattr(ctx.jira, "created", [])

    create_issue = ctx.jira.create_issue

    def refusing_story(project, summary, description, issuetype="Story", **kwargs):
        if summary == "Refused story":
            raise RuntimeError("Jira refused this issue: unsupported issue type")
        return create_issue(project, summary, description, issuetype=issuetype, **kwargs)

    ctx.jira.create_issue = refusing_story
    assert ba_chat.accept_update(ctx, row, message(21, "/approve"))
    assert [issue["summary"] for issue in ctx.jira.created] == ["First epic", "Second epic", "Independent story"]
    assert ba_chat.poll(ctx, telegram) == 0
    sent = "\n".join(telegram.sent)
    assert "Refused story" in sent and "unsupported issue type" in sent
    assert "Independent story" in sent and "TESTWP-902" in sent


def test_private_chat_is_bound_to_configured_owner_and_update_ids_are_deduplicated(ctx):
    row = channel(ctx)
    assert not ba_chat.accept_update(ctx, row, message(30, "not private", chat_type="group"))
    assert not ba_chat.accept_update(ctx, row, message(31, "other user", sender_id="555"))
    assert ba_chat.accept_update(ctx, row, message(32, "accepted"))
    assert not ba_chat.accept_update(ctx, row, message(32, "duplicate"))
    assert ctx.db.one("SELECT count(*) AS n FROM ba_chat_jobs")["n"] == 1


def test_telegram_token_is_not_logged_when_polling_fails(ctx, caplog):
    row = channel(ctx)

    class FailedTelegram(FakeTelegram):
        def updates(self, token, offset):
            raise RuntimeError(f"failed to GET https://api.telegram.org/bot{token}/getUpdates")

    assert ba_chat.poll(ctx, FailedTelegram()) == 0
    assert BOT_TOKEN not in caplog.text


def test_cli_configures_the_same_channel_without_printing_the_token(ctx, monkeypatch, capsys):
    from aiflow.v4 import cli
    import aiflow.v4.control.db as control_db

    class DBProxy:
        def __init__(self, *_args, **_kwargs):
            pass

        def __getattr__(self, name):
            return getattr(ctx.db, name)

        def close(self):
            pass

    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda _cls: ctx.settings))
    monkeypatch.setattr(control_db, "DB", DBProxy)
    monkeypatch.setattr(ControlRepo, "load", staticmethod(lambda _path: ctx.repo()))
    monkeypatch.setattr("sys.stdin", io.StringIO(BOT_TOKEN))

    assert cli.main(["ba-chat", "setup", "--name", "CLI brainstorm", "--chat-id", CHAT_ID,
                     "--project", "TESTWP", "--token-stdin"]) == 0
    assert BOT_TOKEN not in capsys.readouterr().out
    configured = ctx.db.one("SELECT config FROM notification_channels WHERE name='CLI brainstorm'")
    assert configured["config"]["bot_token"] == BOT_TOKEN
    assert configured["config"]["ba_chat_enabled"] is True


def test_console_configures_ba_chat_project_without_returning_the_bot_token(client, ctx):
    ctx.settings.console_session_secret = "ba-chat-console-session-secret-32-bytes"
    token = issue_session("owner@example.test", "owner",
                          int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                          ctx.settings.console_session_secret)
    client.cookies.set(COOKIE_NAME, token)
    mount_ui(client.app, ctx)

    page = client.get("/notifications")
    assert page.status_code == 200
    assert "Enable BA brainstorming chat" in page.text and "Jira project for brainstorms" in page.text
    available = client.get("/v1/notifications")
    assert available.status_code == 200 and {p["key"] for p in available.json()["projects"]} == {"TESTWP"}
    saved = client.put("/v1/notifications/channels", json={"kind": "telegram", "name": "Owner chat",
        "config": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID, "ba_chat_enabled": True, "project_key": "TESTWP"}})
    assert saved.status_code == 200, saved.text
    assert saved.json()["channel"]["config"] == {"chat_id": CHAT_ID, "ba_chat_enabled": True, "project_key": "TESTWP"}
    assert BOT_TOKEN not in saved.text and BOT_TOKEN not in available.text

    invalid = client.put("/v1/notifications/channels", json={"kind": "telegram", "name": "Bad project",
        "config": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID, "ba_chat_enabled": True, "project_key": "OTHER"}})
    assert invalid.status_code == 400
    mixed = client.put("/v1/notifications/channels", json={"kind": " TELEGRAM ", "name": "Mixed case",
        "config": {"bot_token": BOT_TOKEN, "chat_id": CHAT_ID, "ba_chat_enabled": True, "project_key": "OTHER"}})
    assert mixed.status_code == 400


def test_claim_outlives_the_worker_turn_timeout(ctx):
    channel(ctx)
    assert ba_chat.accept_update(ctx, ctx.db.one("SELECT * FROM notification_channels WHERE kind='telegram'"),
                                 message(9, "Plan something"))
    job = ba_chat.claim_job(ctx, "claude-01", "cc")
    seconds = ctx.db.one("SELECT extract(epoch FROM claim_until-clock_timestamp()) AS s FROM ba_chat_jobs "
                         "WHERE id=%s", (job["job_id"],))["s"]
    assert seconds > 600  # the worker allows a turn 600 s
