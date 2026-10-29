"""Master AI rescue run (JAIF-92): performs the `rescue` incidents of the operator queue (rules: `master_rescue.py`).

    rescue incident -> Claude available?  yes: Handover status, Taken by = Claude, label master-rescue, audit comment   (one per ticket)
                                          no (quota / offline): the owner at once
                    -> owner-only work or no budget left: the owner at once
    An unsuccessful rescue ends early or uses its budget -> the flow doctor records the cause and returns
    the ticket to development with its findings; it is not escalated to a person.

The rescue record `rescue:<KEY>` (Redis, kept) holds the state, the start, Claude's budget and the model; it is also the
"one rescue per ticket" guard together with the label."""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from . import agents, events, master_rescue
from .incidents import Incident
from .jira import JiraError
from .lock import RedisError
from .memory.guard import Rejected, scan_secrets
from .project_database import redact as redact_line

DAY = 86400
DOCTOR_TARGET = "To Do"  # development
SUCCESS_STATUSES = {"done", "closed", "resolved"}
COMPLETION_STATUSES = SUCCESS_STATUSES | {"in review", "review passed", "in qa", "human review", "ready to merge"}
ACTIVE_RESCUE_STATUSES = {master_rescue.HANDOVER.casefold(), "in progress"}


def safe_run_findings(text: str, verdict: tuple[str, str] | None) -> str:
    """Summarize this run's finding without copying credentials or an unbounded model response."""
    source = str(verdict[1]) if verdict and verdict[1].strip() else str(text or "")
    lines = [redact_line(re.sub(r"[\x00-\x1f\x7f]", " ", line)) for line in source.splitlines() if line.strip()]
    summary = " ".join(lines)[:600]
    if not summary:
        return "Claude produced no final findings."
    try:
        scan_secrets(summary)
    except Rejected:
        return "Claude's run findings contained sensitive-looking text; raw output was omitted."
    return summary


class Rescue:
    def __init__(self, operator: Any) -> None:
        self.op, self.sup = operator, getattr(operator, "sup", operator)
        self.redis, self.jira, self.cfg = operator.redis, operator.jira, operator.cfg

    def pending(self, key: str) -> bool:
        """Fail closed while an active rescue or doctor recovery could supersede ordinary incidents."""
        try:
            record = json.loads(self.redis.get(f"rescue:{key}") or "{}")
        except RedisError:
            return True
        except (TypeError, ValueError):
            return False
        return isinstance(record, dict) and record.get("state") in master_rescue.PENDING_RESCUE_STATES

    @property
    def settings(self) -> master_rescue.MasterSettings:
        return self.cfg.registry.master

    def claude_unavailable(self) -> str:
        """'offline' when no machine has Claude, 'quota' when it cools down after a usage limit, '' when it can work."""
        if master_rescue.RESCUE_AGENT not in self.sup.online_pool():
            return "offline"
        return "quota" if self.sup.cooling(master_rescue.RESCUE_AGENT) else ""

    def model(self) -> tuple[str, str]:
        """The strongest configured model for Claude: `[master] model`, else the model configured for Claude in agent_models."""
        if self.settings.model:
            _, model, effort = agents.parse_spec(self.settings.model)
            return model, effort
        return self.cfg.agent_models.get(master_rescue.RESCUE_AGENT, ("", ""))

    # ---- the incident ----------------------------------------------------------------------------------------------
    def handle(self, incident: Incident) -> bool:
        facts = incident.facts
        reason = ""
        if facts.get("mode") == "owner":
            if facts.get("topic") == master_rescue.CONTEXT_UNAVAILABLE:
                reason = "required Jira description or recent comments could not be read"
            else:
                reason = f"this work is owner-only ({facts.get('topic')})"
        elif unavailable := self.claude_unavailable():
            reason = f"Claude is unavailable ({unavailable})"
        if reason:
            return self.to_owner(incident, reason)
        return self.hand_over(incident)

    def to_owner(self, incident: Incident, reason: str) -> bool:
        question = f"{incident.ticket} qualifies for a master rescue ({incident.facts.get('trigger')}) but goes to the owner: {reason}."
        self.op.escalate(incident, question, "Decide the next step and move the ticket to To Do", "Review the ticket and decide")
        self.op.audit(incident, "master", f"ESCALATE: {reason}", [], [], self.lines(incident))
        self.redis.set(f"operator:cool:{incident.id}", "1", DAY)
        events.emit("operator.rescue", ticket=incident.ticket, outcome="owner", reason=reason[:200])
        return True

    def lines(self, incident: Incident) -> list[str]:
        """Where the ticket is stuck, what was tried, what is left of the budget (the audit comment of the handover)."""
        facts = incident.facts
        return [f"Stuck: {self.stuck_summary(facts)}",
                f"Tried: {self.tried_summary(facts)}",
                f"Work time: {facts.get('worked_minutes', 0)} min in total, {facts.get('stretch_minutes', 0)} min in the current stretch",
                f"Remaining budget: {facts.get('remaining_minutes', 0)} min (master_minutes {self.settings.master_minutes}, "
                f"total_cap_minutes {self.settings.total_cap_minutes})"]

    @staticmethod
    def stuck_summary(facts: dict[str, Any]) -> str:
        times = facts.get("times")
        minutes = times.get("minutes_in_status", 0) if isinstance(times, dict) else 0
        stuck = f"{facts.get('status', 'unknown')} for {minutes} min"
        if facts.get("blocked_kind"):
            stuck += f" ({facts['blocked_kind']})"
        return stuck

    @staticmethod
    def tried_summary(facts: dict[str, Any]) -> str:
        tried = []
        attempts = facts.get("attempts")
        if isinstance(attempts, dict):
            for role in ("developer", "reviewer", "qa", "pr"):
                count = attempts.get(role)
                if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    tried.append(f"{count} failed {role} run(s) recorded")
        if not tried:
            tried.append("no prior failed AI runs recorded")
        if facts.get("blocked_kind") == "Rework" or facts.get("rework_rounds"):
            tried.append(f"{facts.get('rework_rounds', 0)} rework round(s) recorded")
        comments = facts.get("last_comments", [])
        if isinstance(comments, list) and comments:
            tried.append(f"{len(comments)} recent comment(s) reviewed; raw excerpts omitted")
        return "; ".join(tried)

    def hand_over(self, incident: Incident) -> bool:
        key = incident.ticket
        model, effort = self.model()
        try:
            self.jira.set_field(key, self.settings.taken_by_field, {"value": master_rescue.RESCUE_AGENT})
            if model:
                self.jira.set_field(key, self.cfg.model_field, {"value": f"{master_rescue.RESCUE_AGENT}:{model}"})
        except JiraError as error:
            self.log(f"master rescue of {key} could not set the handover fields: {error}")
            self.rollback_handover(key, incident.id, bool(model))
            return True
        started = int(time.time())
        record = self.handover_record(incident, model, effort, started)
        try:
            self.finish(key, record, "handover_pending")
        except RedisError as error:
            self.log(f"master rescue of {key} could not persist its pending handover: {error}")
            self.rollback_handover(key, incident.id, bool(model))
            return True
        self.redis.delete(f"lease:{key}")  # the pending record fences any worker after a restart
        try:
            moved = self.jira.transition(key, master_rescue.HANDOVER)
        except JiraError as error:
            self.log(f"master rescue of {key} got an uncertain Handover response: {error}")
            moved = False
        if not moved:
            status = self.current_status(key)
            if status is None:
                return True
            if status.casefold() != master_rescue.HANDOVER.casefold():
                self.rollback_handover(key, incident.id, bool(model))
                return True
        self.activate_handover(key, record)
        return True

    def handover_record(self, incident: Incident, model: str, effort: str, started: int) -> dict[str, Any]:
        key, facts = incident.ticket, incident.facts
        findings = self.lines(incident)
        source_times = facts.get("times")
        minutes = source_times.get("minutes_in_status", 0) if isinstance(source_times, dict) else 0
        incident_facts = dict(status=facts.get("status", "unknown"), times={"minutes_in_status": minutes},
                              trigger=facts.get("trigger", ""), blocked_kind=facts.get("blocked_kind", ""),
                              worked_minutes=facts.get("worked_minutes", 0), stretch_minutes=facts.get("stretch_minutes", 0),
                              remaining_minutes=facts.get("remaining_minutes", 0), attempts=facts.get("attempts", {}))
        extra = [f"Master rescue handover audit: rescue:{key}", *findings,
                 f"Taken by: {master_rescue.RESCUE_AGENT}, model {model or 'default'}" +
                 (f" (effort {effort})" if effort else "")]
        return dict(state="handover_pending", started=started, budget=facts.get("remaining_minutes", 0),
                    model=f"{model}:{effort}".strip(":"), stuck=facts.get("trigger", ""), findings=findings,
                    summary=incident.summary, incident_facts=incident_facts,
                    label_pending=True, handover_audit_pending=True, handover_audit_attempts=0,
                    handover_audit=dict(summary=incident.summary, extra=extra,
                                        done=["handover", f"label {master_rescue.RESCUE_LABEL}"]))

    def activate_handover(self, key: str, record: dict[str, Any]) -> None:
        record["state"] = "active"
        self.finish(key, record, "active")
        self.ensure_label(key, record)
        self.ensure_handover_audit(key, record)
        events.emit("operator.rescue", ticket=key, outcome="handover", budget=record["budget"], model=record["model"])

    def defer_unstarted_to_owner(self, key: str, reason: str) -> bool:
        """Keep global Claude unavailability durable for the operator; a local capacity miss is not a handoff."""
        record = self.load_record(f"rescue:{key}")
        if record is None or record.get("state") != "active" or record.get("worker_started"):
            return False
        record["owner_reason"] = f"Claude is unavailable ({reason})"
        self.finish(key, record, "owner_pending")
        return True

    def mark_worker_started(self, key: str) -> bool:
        """Record the first successful Claude reservation so later outages do not reclassify active work."""
        record = self.load_record(f"rescue:{key}")
        if record is None or record.get("state") != "active":
            return False
        record["worker_started"] = True
        self.finish(key, record, "active")
        return True

    def rollback_handover(self, key: str, incident_id: str, model: bool) -> None:
        if not self.clear_handover_fields(key, model):
            self.queue_handover_cleanup(key, model)
        self.redis.delete(f"rescue:{key}")
        self.redis.set(f"operator:cool:{incident_id}", "1", 600)

    def queue_handover_cleanup(self, key: str, model: bool) -> None:
        self.redis.set_persistent(f"operator:rescue-cleanup:{key}", json.dumps({"model": model}))

    def current_status(self, key: str) -> str | None:
        """Read Jira's status; None means the transition outcome is still uncertain."""
        try:
            issue = self.jira.view(key)
        except (JiraError, ValueError, TypeError, OSError, subprocess.TimeoutExpired):
            return None
        status = issue.get("status") if isinstance(issue, dict) else None
        return status if isinstance(status, str) and status else None

    def reconcile_handover(self, key: str, record: dict[str, Any]) -> bool:
        status = self.current_status(key)
        if status is None:
            return False
        if status.casefold() != master_rescue.HANDOVER.casefold():
            self.rollback_handover(key, f"rescue:{key}", bool(record.get("model")))
            return False
        record["state"] = "active"
        self.finish(key, record, "active")
        return True

    def ensure_label(self, key: str, record: dict[str, Any]) -> bool:
        """Retry the idempotent label write after Jira accepted Handover but rejected the label update."""
        if not record.get("label_pending", True):
            return True
        try:
            self.jira.add_label(key, master_rescue.RESCUE_LABEL)
        except JiraError as error:
            self.log(f"master rescue of {key} moved to Handover but could not add its label: {error}")
            return False
        record["label_pending"] = False
        self.finish(key, record, record.get("state", "active"))
        return True

    def ensure_handover_audit(self, key: str, record: dict[str, Any]) -> bool:
        """Post the saved handover audit once; keep it pending across Jira comment failures."""
        if not record.get("handover_audit_pending"):
            return True
        payload = self.handover_audit_payload(record)
        if payload is None:
            return False
        audit, extra, done, marker, attempts = payload
        if attempts:
            existing = self.find_handover_audit(key, marker)
            if existing is None:
                return False
            if existing:
                self.complete_handover_audit(key, record)
                return True
        record["handover_audit_attempts"] = attempts + 1
        self.finish(key, record, record.get("state", "active"))
        incident = Incident(f"rescue:{key}", "rescue", key, str(audit.get("summary", "")), {})
        outcome = f"HANDOVER to {master_rescue.RESCUE_AGENT}: status {master_rescue.HANDOVER}, label {master_rescue.RESCUE_LABEL}"
        try:
            self.op.audit(incident, "master", outcome, done, [], extra)
        except JiraError:
            self.log(f"master rescue handover audit for {key} is pending a retry")
            return False
        self.complete_handover_audit(key, record)
        return True

    def handover_audit_payload(self, record: dict[str, Any]) -> tuple[dict[str, Any], list[Any], list[Any], str, int] | None:
        audit = record.get("handover_audit")
        if not isinstance(audit, dict):
            return None
        extra, done = audit.get("extra"), audit.get("done")
        if not isinstance(extra, list) or not isinstance(done, list):
            return None
        marker = next((line for line in extra if isinstance(line, str)
                       and line.startswith("Master rescue handover audit:")), "")
        if not marker:
            return None
        try:
            attempts = max(0, int(record.get("handover_audit_attempts", 0) or 0))
        except (TypeError, ValueError):
            attempts = 0
        return audit, extra, done, marker, attempts

    def find_handover_audit(self, key: str, marker: str) -> bool | None:
        """True if Jira already has the audit, False if safe to retry, None if comments could not be checked."""
        try:
            comments = self.jira.comments(key, 10)
        except JiraError:
            return None
        if not isinstance(comments, list):
            return None
        return any(isinstance(comment, dict) and marker in str(comment.get("body", ""))
                   for comment in comments)

    def complete_handover_audit(self, key: str, record: dict[str, Any]) -> None:
        record["handover_audit_pending"] = False
        record.pop("handover_audit", None)
        self.finish(key, record, record.get("state", "active"))

    def clear_taken_by(self, key: str) -> bool:
        """Clear Claude as the temporary owner after a refused Handover or unsuccessful rescue."""
        try:
            self.jira.set_field(key, self.settings.taken_by_field, None)
        except JiraError as error:
            self.log(f"master rescue could not clear Taken by on {key}: {error}")
            return False
        return True

    def clear_model(self, key: str) -> bool:
        """Remove the rescue model field when Jira refuses the Handover transition."""
        try:
            self.jira.set_field(key, self.cfg.model_field, None)
        except JiraError as error:
            self.log(f"master rescue could not clear the model field on {key}: {error}")
            return False
        return True

    def clear_handover_fields(self, key: str, model: bool) -> bool:
        taken_by_cleared = self.clear_taken_by(key)
        model_cleared = self.clear_model(key) if model else True
        return taken_by_cleared and model_cleared

    def check_cleanup(self) -> None:
        """Retry temporary-field cleanup after Jira refuses a Handover transition."""
        for name in self.redis.scan("operator:rescue-cleanup:*"):
            key = name.split(":", 2)[2]
            try:
                record = json.loads(self.redis.get(name) or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(record, dict) and self.clear_handover_fields(key, bool(record.get("model"))):
                self.redis.delete(name)

    # ---- the end of a rescue -----------------------------------------------------------------------------------------
    def check_rescues(self) -> None:
        """Reconcile labels and route every unconfirmed rescue outcome through the flow doctor."""
        self.check_cleanup()
        for name in self.redis.scan("rescue:*"):
            key = name.split(":", 1)[1]
            record = self.load_record(name)
            if record is not None:
                self.check_rescue(key, record)

    def load_record(self, name: str) -> dict[str, Any] | None:
        try:
            record = json.loads(self.redis.get(name) or "{}")
        except (TypeError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def check_rescue(self, key: str, record: dict[str, Any]) -> None:
        if self.reconcile_rescue_state(key, record):
            return
        self.reconcile_handover_artifacts(key, record)
        if self.route_pending_owner(key, record):
            return
        if record.get("state") == "doctor_pending":
            self.flow_doctor(key, record, record.get("doctor_cause", "the rescue ended without a result"),
                             record.get("doctor_findings", ""))
            return
        if record.get("state") != "active":
            return
        self.check_active_rescue(key, record)

    def reconcile_rescue_state(self, key: str, record: dict[str, Any]) -> bool:
        """Resolve states that must be checked before ordinary active-rescue handling."""
        if record.get("state") == "completing" and self.check_completion(key, record):
            return True
        return record.get("state") == "handover_pending" and not self.reconcile_handover(key, record)

    def reconcile_handover_artifacts(self, key: str, record: dict[str, Any]) -> None:
        if record.get("handover_audit_pending"):
            self.ensure_handover_audit(key, record)
        if record.get("label_pending", True):
            self.ensure_label(key, record)

    def route_pending_owner(self, key: str, record: dict[str, Any]) -> bool:
        if record.get("state") != "owner_pending":
            return False
        model = bool(record.get("model"))
        if not self.clear_handover_fields(key, model):
            self.queue_handover_cleanup(key, model)
        facts = record.get("incident_facts")
        incident = Incident(f"rescue:{key}", "rescue", key, str(record.get("summary", "")),
                            facts if isinstance(facts, dict) else {})
        try:
            self.to_owner(incident, str(record.get("owner_reason", "Claude is unavailable")))
        except JiraError as error:
            self.log(f"master rescue of {key} could not route the unavailable Claude case to the owner: {error}")
            return True
        self.finish(key, record, "owner")
        return True

    def route_unavailable_unstarted(self, key: str, record: dict[str, Any]) -> bool:
        if record.get("worker_started"):
            return False
        unavailable = self.claude_unavailable()
        if not unavailable or not self.defer_unstarted_to_owner(key, unavailable):
            return False
        pending = self.load_record(f"rescue:{key}")
        return self.route_pending_owner(key, pending) if pending else True

    def check_active_rescue(self, key: str, record: dict[str, Any]) -> None:
        if self.route_unavailable_unstarted(key, record):
            return
        status = self.rescue_status(key)
        if not status:
            return
        if status.casefold() in SUCCESS_STATUSES:
            self.finish(key, record, "finished")
            return
        expired = time.time() - record.get("started", 0) >= record.get("budget", 0) * 60
        if status.casefold() not in ACTIVE_RESCUE_STATUSES:
            self.end(key, ok=False, cause=f"the rescue ended early in {status} without a confirmed result",
                     findings=self.recent_findings(key, record))
        elif expired:
            self.end(key, ok=False, cause=f"the rescue budget of {record.get('budget', 0)} min ran out without a result",
                     findings=self.recent_findings(key, record))

    def begin_completion(self, key: str) -> bool:
        record = self.load_record(f"rescue:{key}")
        if record is None or record.get("state") != "active":
            return False
        self.finish(key, record, "completing")
        return True

    def check_completion(self, key: str, record: dict[str, Any]) -> bool:
        """Reconcile a successful worker handoff without racing it into the failure doctor."""
        status = self.rescue_status(key)
        if not status:
            return True
        if status.casefold() in COMPLETION_STATUSES:
            self.finish(key, record, "finished")
        elif status.casefold() == master_rescue.HANDOVER.casefold() and self.redis.get(f"lease:{key}"):
            return True
        elif status.casefold() == master_rescue.HANDOVER.casefold():
            record["state"] = "active"
            self.finish(key, record, "active")
            return False
        else:
            self.end(key, ok=False, cause=f"Claude's completion handoff ended in {status} without a confirmed result",
                     findings=self.recent_findings(key, record))
        return True

    def reconcile_failed_completion(self, key: str) -> bool:
        """Return True when a failed worker response is confirmed complete or Jira is still unreadable."""
        record = self.load_record(f"rescue:{key}")
        if record is None or record.get("state") != "completing":
            return False
        status = self.rescue_status(key)
        if not status:
            return True  # leave `completing` for a later operator pass to reconcile
        if status.casefold() not in COMPLETION_STATUSES:
            return False
        self.finish(key, record, "finished")
        return True

    def rescue_status(self, key: str) -> str:
        return self.current_status(key) or ""

    def recent_findings(self, key: str, record: dict[str, Any]) -> str:
        """Include recent Claude or operator findings in the flow-doctor return comment."""
        try:
            comments = self.jira.comments(key, 3)
        except (JiraError, ValueError, TypeError):
            comments = []
        findings = [item for item in comments if isinstance(item, dict) and item.get("body")
                    and not str(item.get("body", "")).startswith("AI Flow operator (master)")]
        if findings:
            return f"{len(findings)} recent ticket comment(s) contain findings; review them on the ticket (raw excerpts omitted)."
        return "; ".join(record.get("findings", [])) or record.get("stuck", "")

    def finish(self, key: str, record: dict[str, Any], state: str) -> None:
        self.redis.set_persistent(f"rescue:{key}", json.dumps({**record, "state": state}))

    def end(self, key: str, ok: bool, cause: str = "", findings: str = "") -> bool:
        """Record a terminal rescue outcome; failures return to development through the flow doctor."""
        try:
            record = json.loads(self.redis.get(f"rescue:{key}") or "{}")
        except (TypeError, ValueError):
            record = {}
        if not record:
            return False
        if ok:
            self.finish(key, record, "finished")
            return True
        if self.preserve_env_safety_block(key, record):
            return True
        return self.flow_doctor(key, record, cause or "the rescue ended without a result", findings)

    def preserve_env_safety_block(self, key: str, record: dict[str, Any]) -> bool:
        """Keep the explicit worktree `.env` credential guards out of flow-doctor recovery."""
        if record.get("state") not in master_rescue.PENDING_RESCUE_STATES:
            return False
        try:
            blocked = json.loads(self.redis.get(f"blocked:{key}") or "{}")
        except (TypeError, ValueError, RedisError):
            return False
        if not isinstance(blocked, dict) or blocked.get("kind") != "Config":
            return False
        reason = str(blocked.get("reason", "")).casefold()
        markers = ("worktree .env is not ignored by git", "worktree .env was committed")
        if not any(marker in reason for marker in markers):
            return False
        self.finish(key, record, "safety_blocked")
        return True

    def flow_doctor(self, key: str, record: dict[str, Any], cause: str, findings: str) -> bool:
        """Record the cause and return the ticket to development with its findings; no escalation to a person."""
        record = {**record, "state": "doctor_pending", "doctor_cause": cause,
                  "doctor_findings": findings or "; ".join(record.get("findings", [])) or record.get("stuck", ""),
                  "doctor_commented": record.get("doctor_commented", False)}
        self.finish(key, record, "doctor_pending")
        if not record["doctor_commented"]:
            try:
                self.jira.comment(key, f"AI Flow doctor: Claude's rescue of {key} ended without a result.\nCause: {cause}\n"
                                       f"Findings: {record['doctor_findings']}\nThe ticket returns to development ({DOCTOR_TARGET}); "
                                       "it is not escalated to a person.")
            except JiraError as error:
                self.log(f"flow doctor could not comment on {key}: {error}")
                return False
            record["doctor_commented"] = True
            self.finish(key, record, "doctor_pending")
        if not self.clear_handover_fields(key, bool(record.get("model"))):
            self.queue_handover_cleanup(key, bool(record.get("model")))
            return False
        try:
            status = str(self.jira.view(key).get("status", "") or "")
        except JiraError:
            return False
        if status != DOCTOR_TARGET and not self.jira.transition(key, DOCTOR_TARGET):
            return False
        self.sup.clear_block(key)
        self.finish(key, record, "failed")
        events.emit("operator.doctor", ticket=key, cause=cause[:300])
        return True

    def log(self, message: str) -> None:
        callback = getattr(self.op, "sup_log", None)
        if callback:
            callback(message)
            return
        from .supervisor import log as supervisor_log
        supervisor_log(message)
