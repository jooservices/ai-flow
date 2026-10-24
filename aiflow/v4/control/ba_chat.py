"""Channel-neutral BA chat state machine and proposal-to-Jira gateway.

The control plane owns durable conversations, approval, and Jira writes. Channel adapters only receive and send
messages; workers return a question or structured proposal through the fenced node API.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import httpx

from . import notify
from .db import J

log = logging.getLogger("aiflow4.ba_chat")
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
MAX_TEXT = 4000


class TelegramAdapter:
    """Telegram transport. Callers deliberately log only exception types because the token is in the API URL."""

    @staticmethod
    def updates(token: str, offset: int) -> list[dict]:
        response = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                             params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'}, timeout=10)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("ok") is not True or not isinstance(body.get("result"), list):
            raise RuntimeError("Telegram rejected update polling")
        return body["result"]

    @staticmethod
    def send(token: str, chat_id: str, body: str) -> None:
        for chunk in _chunks(body):
            response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat_id, "text": chunk}, timeout=10)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise RuntimeError("Telegram rejected the message")


def _chunks(body: str) -> list[str]:
    text = str(body or "").strip() or "I couldn't prepare a response. Please send your request again."
    chunks = []
    while len(text) > MAX_TEXT:
        split = text.rfind("\n", 0, MAX_TEXT)
        split = split if split > 0 else MAX_TEXT
        chunks.append(text[:split])
        text = text[split:].lstrip()
    chunks.append(text)
    return chunks


def enabled_channels(ctx) -> list[dict]:
    return ctx.db.all("SELECT * FROM notification_channels WHERE kind='telegram' AND enabled=true"
                      " AND COALESCE((config->>'ba_chat_enabled')::boolean,false)=true ORDER BY id")


def _redact(text: str, token: str) -> str:
    return str(text or "").replace(token, "[hidden]") if token else str(text or "")


def _enqueue(ctx, channel_id: int, conversation_id: str | None, body: str, token: str = "") -> None:
    safe = _redact(str(body or "").strip(), token)[:80_000]
    ctx.db.run("INSERT INTO ba_chat_outbox (channel_id, conversation_id, body) VALUES (%s,%s,%s)",
               (channel_id, conversation_id, safe))


def _send_outbox(ctx, channel: dict, transport: TelegramAdapter, limit: int = 20) -> int:
    """Retry queued replies without putting provider URLs or response bodies in logs or database errors."""
    config = channel.get("config") or {}
    token, chat_id = str(config.get("bot_token") or ""), str(config.get("chat_id") or "")
    if not token or not chat_id:
        return 0
    ctx.db.run("UPDATE ba_chat_outbox SET status='pending', next_attempt_at=now()"
               " WHERE channel_id=%s AND status='sending' AND created_at < now()-interval '2 minutes'", (channel["id"],))
    sent = 0
    for _ in range(limit):
        with ctx.db.pool.connection() as connection:
            with connection.transaction():
                row = connection.execute("SELECT id,body FROM ba_chat_outbox WHERE channel_id=%s"
                                         " AND status='pending' AND next_attempt_at<=now()"
                                         " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1", (channel["id"],)).fetchone()
                if not row:
                    break
                connection.execute("UPDATE ba_chat_outbox SET status='sending',attempts=attempts+1 WHERE id=%s",
                                   (row["id"],))
        try:
            transport.send(token, chat_id, _redact(row["body"], token))
        except Exception as exc:  # noqa: BLE001 - provider details may contain the bot URL and token
            error = type(exc).__name__[:80]
            log.warning("BA chat reply failed channel_id=%s error=%s", channel["id"], error)
            ctx.db.run("UPDATE ba_chat_outbox SET status='pending',last_error=%s,next_attempt_at=now() +"
                       " make_interval(secs => LEAST(600, 30 * GREATEST(attempts,1))) WHERE id=%s", (error, row["id"]))
            break
        ctx.db.run("UPDATE ba_chat_outbox SET status='sent',last_error='',sent_at=now() WHERE id=%s", (row["id"],))
        sent += 1
    return sent


def _action(text: str) -> str:
    command = " ".join((text or "").strip().casefold().split()).strip(".! ")
    if command.startswith("/"):
        command = command.split("@", 1)[0]
    if command in {"/approve", "approve", "approved", "yes", "duyệt", "duyet", "đồng ý", "dong y"}:
        return "approve"
    if command in {"/reject", "reject", "rejected", "no", "từ chối", "tu choi", "hủy", "huy"}:
        return "reject"
    return "change"


def accept_message(ctx, channel: dict, update_id: int, external_chat_id: str, text: str,
                   secret_to_redact: str = "") -> bool:
    """Accept a channel adapter's authorized owner message and apply the common BA conversation state machine."""
    config = channel.get("config") or {}
    external_chat_id = str(external_chat_id or "").strip()
    if not external_chat_id or external_chat_id != str(config.get("chat_id") or ""):
        return False
    text = _redact(str(text or "").strip(), secret_to_redact)[:20_000]
    project = str(config.get("project_key") or "").strip().upper()
    if not text or not PROJECT_KEY.fullmatch(project):
        return False

    conversation_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    action = ""
    proposal = None
    with ctx.db.pool.connection() as connection:
        with connection.transaction():
            inserted = connection.execute("INSERT INTO ba_chat_updates (channel_id,update_id) VALUES (%s,%s)"
                                          " ON CONFLICT DO NOTHING RETURNING update_id",
                                          (channel["id"], update_id)).fetchone()
            if not inserted:
                return False
            row = connection.execute("SELECT * FROM ba_chat_conversations WHERE channel_id=%s"
                                     " AND external_chat_id=%s FOR UPDATE", (channel["id"], external_chat_id)).fetchone()
            if not row:
                connection.execute("INSERT INTO ba_chat_conversations"
                                   " (id,channel_id,external_chat_id,project_key) VALUES (%s,%s,%s,%s)",
                                   (conversation_id, channel["id"], external_chat_id, project))
                row = {"id": conversation_id, "status": "collecting", "history": [], "proposal": None,
                       "project_key": project}
            else:
                conversation_id = str(row["id"])
            if row["status"] in ("completed", "rejected"):
                row["status"], row["proposal"], row["history"] = "collecting", None, []
            status = row["status"]
            action = _action(text) if status == "proposal" else "chat"
            history = list(row.get("history") or [])
            history.append({"role": "user", "content": text})
            if action == "approve":
                proposal = row.get("proposal")
                connection.execute("UPDATE ba_chat_conversations SET status='creating',history=%s,updated_at=now()"
                                   " WHERE id=%s AND status='proposal'", (J(history), conversation_id))
            elif action == "reject":
                reply = "Proposal rejected. No Jira tickets were created. Send another message when you want to start a new brainstorm."
                history.append({"role": "assistant", "content": reply})
                connection.execute("UPDATE ba_chat_conversations SET status='rejected',proposal=NULL,history=%s,updated_at=now()"
                                   " WHERE id=%s", (J(history), conversation_id))
                connection.execute("INSERT INTO ba_chat_outbox (channel_id,conversation_id,body) VALUES (%s,%s,%s)",
                                   (channel["id"], conversation_id, _redact(reply, secret_to_redact)))
            else:
                connection.execute("UPDATE ba_chat_conversations SET status='collecting',history=%s,updated_at=now()"
                                   " WHERE id=%s", (J(history), conversation_id))
                connection.execute("INSERT INTO ba_chat_jobs (id,conversation_id,channel_id,update_id)"
                                   " VALUES (%s,%s,%s,%s)", (job_id, conversation_id, channel["id"], update_id))
    if action == "approve":
        result = create_proposal(ctx, proposal or {})
        reply = creation_reply(result, getattr(ctx.jira, "site", "") or ctx.settings.jira_site)
        reply = _redact(reply, secret_to_redact)
        ctx.db.run("UPDATE ba_chat_conversations SET status='completed',proposal=NULL,history=history || %s,updated_at=now()"
                   " WHERE id=%s", (J([{"role": "assistant", "content": reply}]), conversation_id))
        _enqueue(ctx, channel["id"], conversation_id, reply, secret_to_redact)
    return True


def accept_update(ctx, channel: dict, update: dict) -> bool:
    """Validate a Telegram private-chat update before handing its text to the shared chat state machine."""
    try:
        update_id = int(update.get("update_id"))
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id") or "")
        if (chat.get("type") != "private" or not chat_id or chat_id != str(sender.get("id") or "")):
            return False
        secret = str((channel.get("config") or {}).get("bot_token") or "")
        return accept_message(ctx, channel, update_id, chat_id, str(message.get("text") or ""), secret)
    except (AttributeError, TypeError, ValueError):
        return False


def poll(ctx, transport: TelegramAdapter | None = None) -> int:
    """Poll enabled Telegram channels and flush the shared chat outbox."""
    transport = transport or TelegramAdapter()
    updates_seen = 0
    for channel in enabled_channels(ctx):
        config = channel.get("config") or {}
        token = str(config.get("bot_token") or "")
        if not token or not config.get("chat_id") or not config.get("project_key"):
            continue
        _send_outbox(ctx, channel, transport)
        offset = ctx.db.one("SELECT next_update_id FROM ba_chat_offsets WHERE channel_id=%s", (channel["id"],))
        next_id = int(offset["next_update_id"]) if offset else 0
        try:
            updates = transport.updates(token, next_id)
        except Exception as exc:  # noqa: BLE001 - provider URL contains the bot token
            log.warning("BA chat polling failed channel_id=%s error=%s", channel["id"], type(exc).__name__)
            continue
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
            try:
                update_id = int(update["update_id"])
            except (KeyError, TypeError, ValueError):
                continue
            accept_update(ctx, channel, update)
            next_id = max(next_id, update_id + 1)
            ctx.db.run("INSERT INTO ba_chat_offsets (channel_id,next_update_id) VALUES (%s,%s)"
                       " ON CONFLICT (channel_id) DO UPDATE SET next_update_id=GREATEST(ba_chat_offsets.next_update_id,"
                       "EXCLUDED.next_update_id),updated_at=now()", (channel["id"], next_id))
            updates_seen += 1
        _send_outbox(ctx, channel, transport)
    return updates_seen


def claim_job(ctx, account: str, node: str) -> dict | None:
    """Claim the oldest runnable conversation turn for one authenticated worker."""
    repo_account = ctx.repo().account(account)
    if not repo_account or not repo_account.runs_on(node) or repo_account.paused:
        return None
    with ctx.db.pool.connection() as connection:
        with connection.transaction():
            row = connection.execute(
                "SELECT j.id,j.conversation_id,c.project_key,c.status,c.history,c.proposal FROM ba_chat_jobs j"
                " JOIN ba_chat_conversations c ON c.id=j.conversation_id"
                " JOIN notification_channels n ON n.id=j.channel_id"
                " WHERE (j.status='pending' OR (j.status='claimed' AND j.claim_until<now()))"
                " AND n.kind='telegram' AND n.enabled=true"
                " AND COALESCE((n.config->>'ba_chat_enabled')::boolean,false)=true"
                " AND c.status IN ('collecting','proposal') AND NOT EXISTS (SELECT 1 FROM ba_chat_jobs older"
                " WHERE older.conversation_id=j.conversation_id"
                " AND (older.created_at,older.id)<(j.created_at,j.id)"
                " AND older.status IN ('pending','claimed')) ORDER BY j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1"
            ).fetchone()
            if not row:
                return None
            claim_token = str(uuid.uuid4())
            connection.execute("UPDATE ba_chat_jobs SET status='claimed',claimed_by=%s,claimed_node=%s,"
                               "claim_token=%s,claim_until=clock_timestamp()+interval '15 minutes',updated_at=now()"
                               " WHERE id=%s",
                               (account, node, claim_token, row["id"]))
            return {"job_id": str(row["id"]), "conversation_id": str(row["conversation_id"]),
                    "project_key": row["project_key"], "status": row["status"],
                    "history": row["history"] or [], "proposal": row["proposal"] or {},
                    "claim_token": claim_token}


def _clean_line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _normalise_item(item: Any, *, tasks: bool = False) -> dict:
    if not isinstance(item, dict):
        raise ValueError("proposal items must be objects")
    title = _clean_line(item.get("title"), 255)
    description = _clean_line(item.get("description"), 2000)
    if not title or not description:
        raise ValueError("each proposal item needs a title and description")
    criteria = item.get("acceptance_criteria") or []
    if isinstance(criteria, str):
        criteria = [criteria]
    if not isinstance(criteria, list):
        raise ValueError("acceptance criteria must be a list")
    criteria = [_clean_line(value, 500) for value in criteria if _clean_line(value, 500)][:20]
    if not criteria:
        criteria = [description]
    technical = _clean_line(item.get("technical_notes"), 2000) or "No additional technical notes."
    done = item.get("definition_of_done") or []
    if isinstance(done, str):
        done = [done]
    if not isinstance(done, list):
        raise ValueError("definition of done must be a list")
    done = [_clean_line(value, 500) for value in done if _clean_line(value, 500)][:20]
    if not done:
        done = ["The approved work is implemented and verified."]
    normalized = {"title": title, "description": description, "acceptance_criteria": criteria,
                  "technical_notes": technical, "definition_of_done": done}
    if tasks:
        normalized["tasks"] = [_normalise_item(task) for task in (item.get("tasks") or [])]
    return normalized


def normalise_proposal(value: Any, project_key: str = "") -> dict:
    if not isinstance(value, dict):
        raise ValueError("proposal must be an object")
    key = str(project_key or value.get("project_key") or "").strip().upper()
    if not PROJECT_KEY.fullmatch(key):
        raise ValueError("proposal has no valid Jira project")
    epics = value.get("epics")
    if not isinstance(epics, list) or not epics or len(epics) > 20:
        raise ValueError("proposal must contain between one and twenty epics")
    normalized_epics = []
    for raw_epic in epics:
        epic = _normalise_item(raw_epic, tasks=False)
        stories = raw_epic.get("stories") if isinstance(raw_epic, dict) else None
        if not isinstance(stories, list) or not stories or len(stories) > 30:
            raise ValueError("each epic must have between one and thirty stories")
        epic["stories"] = [_normalise_item(story, tasks=True) for story in stories]
        normalized_epics.append(epic)
    return {"project_key": key, "summary": _clean_line(value.get("summary"), 2000), "epics": normalized_epics}


def _ticket_description(item: dict) -> str:
    criteria = "\n".join(f"- AC{index}: {criterion}" for index, criterion in enumerate(item["acceptance_criteria"], 1))
    done = "\n".join(f"- {line}" for line in item["definition_of_done"])
    return (f"Business requirement:\n{item['description']}\n\nAcceptance criteria:\n{criteria}\n\n"
            f"Technical notes:\n{item['technical_notes']}\n\nDefinition of done:\n{done}")


def create_proposal(ctx, proposal: dict) -> dict:
    """Create every approved ticket from one proposal, continuing after a Jira refusal."""
    try:
        proposal = normalise_proposal(proposal)
    except ValueError as exc:
        return {"created": [], "failed": [{"title": "Proposal", "reason": str(exc)}]}
    created: list[dict] = []
    failed: list[dict] = []
    project = proposal["project_key"]
    if not ctx.jira:
        all_items = []
        for epic in proposal["epics"]:
            all_items.append(epic)
            for story in epic["stories"]:
                all_items.append(story)
                all_items.extend(story.get("tasks") or [])
        return {"created": [], "failed": [{"title": item["title"], "reason": "Jira is not connected"}
                                           for item in all_items]}

    def create(item: dict, kind: str, parent: str = "") -> str:
        key = ctx.jira.create_issue(project, item["title"], _ticket_description(item), issuetype=kind, parent=parent)
        created.append({"key": key, "title": item["title"], "type": kind, "parent": parent})
        return key

    for epic in proposal["epics"]:
        try:
            epic_key = create(epic, "Epic")
        except Exception as exc:  # noqa: BLE001 - Jira error text can contain unrelated credentials
            reason = _safe_jira_reason(exc)
            failed.append({"title": epic["title"], "reason": reason})
            for story in epic["stories"]:
                failed.append({"title": story["title"], "reason": f"not created because epic failed: {reason}"})
                failed.extend({"title": task["title"], "reason": f"not created because parent story was unavailable: {reason}"}
                              for task in story.get("tasks") or [])
            continue
        for story in epic["stories"]:
            try:
                story_key = create(story, "Story", epic_key)
            except Exception as exc:  # noqa: BLE001 - continue independent work
                reason = _safe_jira_reason(exc)
                failed.append({"title": story["title"], "reason": reason})
                failed.extend({"title": task["title"], "reason": f"not created because story failed: {reason}"}
                              for task in story.get("tasks") or [])
                continue
            for task in story.get("tasks") or []:
                try:
                    create(task, "Sub-task", story_key)
                except Exception as exc:  # noqa: BLE001 - continue independent work
                    failed.append({"title": task["title"], "reason": _safe_jira_reason(exc)})
    return {"project_key": project, "summary": proposal.get("summary", ""), "created": created, "failed": failed}


def _safe_jira_reason(exc: Exception) -> str:
    text = _clean_line(str(exc), 300)
    # Jira's REST body can be large or include response material; expose only a short explanation.
    return text or f"Jira refused the ticket ({type(exc).__name__})"


def creation_reply(result: dict, site: str = "") -> str:
    lines = ["Jira ticket creation finished."]
    base = str(site or "").rstrip("/")
    if result.get("created"):
        lines.append("Created:")
        for item in result["created"]:
            link = f"{base}/browse/{item['key']}" if base else item["key"]
            lines.append(f"- {item['title']}: {item['key']} ({link})")
    if result.get("failed"):
        lines.append("Failed:")
        lines.extend(f"- {item['title']}: {item['reason']}" for item in result["failed"])
    if not result.get("created") and not result.get("failed"):
        lines.append("No tickets were created.")
    return "\n".join(lines)


def complete_job(ctx, job_id: str, claim_token: str, account: str, node: str, output: dict | None = None,
                 error_kind: str = "") -> bool:
    """Persist only the current, unexpired claim and its reply as one transaction."""
    try:
        claim_token = str(uuid.UUID(claim_token))
    except (TypeError, ValueError, AttributeError):
        return False
    with ctx.db.pool.connection() as connection:
        with connection.transaction():
            job = connection.execute(
                "SELECT j.*,c.history,c.project_key,c.proposal FROM ba_chat_jobs j"
                " JOIN ba_chat_conversations c ON c.id=j.conversation_id"
                " WHERE j.id=%s AND j.status='claimed' AND j.claim_token=%s AND j.claimed_by=%s"
                " AND j.claimed_node=%s AND j.claim_until>clock_timestamp() FOR UPDATE OF j",
                (job_id, claim_token, account, node),
            ).fetchone()
            if not job:
                return False
            channel = connection.execute("SELECT * FROM notification_channels WHERE id=%s",
                                         (job["channel_id"],)).fetchone()
            if not channel:
                connection.execute("UPDATE ba_chat_jobs SET status='failed',last_error='channel removed',updated_at=now()"
                                   " WHERE id=%s", (job_id,))
                return False
            config = channel.get("config") or {}
            token = str(config.get("bot_token") or "")
            reply = ""
            proposal = None
            status, last_error = "done", ""
            mode = str((output or {}).get("mode") or "")
            if error_kind:
                reply = "I couldn't complete that turn just now. Please send the request again in a moment."
                status, last_error = "failed", _clean_line(error_kind, 80)
            elif mode == "question":
                reply = _clean_line((output or {}).get("reply"), 3500) or "What would you like the owner to clarify?"
            elif mode == "proposal":
                try:
                    proposal = normalise_proposal((output or {}).get("proposal"), job["project_key"])
                    summary = _clean_line((output or {}).get("summary") or proposal.get("summary"), 2000)
                    proposal["summary"] = summary
                    reply = render_proposal(summary, proposal)
                except ValueError:
                    reply = "I couldn't prepare a complete proposal yet. Please add more detail or ask me to revise it."
                    status, last_error = "failed", "invalid proposal"
            else:
                reply = "I couldn't prepare a valid response. Please rephrase your request."
                status, last_error = "failed", "invalid model response"

            reply = _redact(reply, token)
            assistant_message = J([{"role": "assistant", "content": reply}])
            if proposal:
                connection.execute("UPDATE ba_chat_conversations SET status='proposal',proposal=%s,"
                                   "history=history || %s,updated_at=now() WHERE id=%s",
                                   (J(proposal), assistant_message, job["conversation_id"]))
            else:
                connection.execute("UPDATE ba_chat_conversations SET status=CASE WHEN proposal IS NOT NULL"
                                   " THEN 'proposal' ELSE 'collecting' END,history=history || %s,updated_at=now()"
                                   " WHERE id=%s", (assistant_message, job["conversation_id"]))
            connection.execute("UPDATE ba_chat_jobs SET status=%s,last_error=%s,updated_at=now() WHERE id=%s",
                               (status, last_error, job_id))
            connection.execute("INSERT INTO ba_chat_outbox (channel_id,conversation_id,body) VALUES (%s,%s,%s)",
                               (job["channel_id"], job["conversation_id"], reply[:80_000]))
    return True


def render_proposal(summary: str, proposal: dict) -> str:
    lines = ["What I understood:", summary or "The request is ready to be planned.", "", "Proposed Jira tickets:"]
    for epic_index, epic in enumerate(proposal["epics"], 1):
        lines.append(f"Epic {epic_index}: {epic['title']} — {epic['description']}")
        for story_index, story in enumerate(epic["stories"], 1):
            lines.append(f"  Story {epic_index}.{story_index}: {story['title']} — {story['description']}")
            for task_index, task in enumerate(story.get("tasks") or [], 1):
                lines.append(f"    Task {epic_index}.{story_index}.{task_index}: {task['title']} — {task['description']}")
    lines.extend(["", "Reply /approve to create these Jira tickets, /reject to discard, or describe the changes you want."])
    return "\n".join(lines)


def process_update_batch(ctx, channel: dict, updates: list[dict], next_id: int) -> int:
    """Test seam for a polled batch; the transport remains an adapter around this state machine."""
    count = 0
    for update in updates:
        if accept_update(ctx, channel, update):
            count += 1
        update_id = int(update.get("update_id", 0))
        next_id = max(next_id, update_id + 1)
        ctx.db.run("INSERT INTO ba_chat_offsets (channel_id,next_update_id) VALUES (%s,%s)"
                   " ON CONFLICT (channel_id) DO UPDATE SET next_update_id=GREATEST(ba_chat_offsets.next_update_id,"
                   "EXCLUDED.next_update_id),updated_at=now()", (channel["id"], next_id))
    return count
