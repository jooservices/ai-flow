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
import time
from typing import Any

from . import agents, events, master_rescue
from .incidents import Incident
from .jira import JiraError

DAY = 86400
DOCTOR_TARGET = "To Do"  # development
SUCCESS_STATUSES = {"done", "closed", "resolved"}
ACTIVE_RESCUE_STATUSES = {master_rescue.HANDOVER.casefold(), "in progress"}


class Rescue:
    def __init__(self, operator: Any) -> None:
        self.op, self.redis, self.jira, self.cfg = operator, operator.redis, operator.jira, operator.cfg

    @property
    def settings(self) -> master_rescue.MasterSettings:
        return self.cfg.registry.master

    def claude_unavailable(self) -> str:
        """'offline' when no machine has Claude, 'quota' when it cools down after a usage limit, '' when it can work."""
        if master_rescue.RESCUE_AGENT not in self.op.sup.online_pool():
            return "offline"
        return "quota" if self.op.sup.cooling(master_rescue.RESCUE_AGENT) else ""

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
        stuck = f"{facts.get('status')} for {facts['times']['minutes_in_status']} min"
        if facts.get("blocked_kind"):
            stuck += f" ({facts['blocked_kind']}: {str(facts.get('reason', ''))[:300]})"
        tried = [f"{facts.get('trigger')}"]
        if facts.get("blocked_kind") == "Attempts":
            tried.append(f"{self.cfg.max_attempts} failed AI attempts")
        if facts.get("blocked_kind") == "Rework" or facts.get("rework_rounds"):
            tried.append(f"{facts.get('rework_rounds') or 'several'} rework round(s)")
        comments = facts.get("last_comments", [])
        if not isinstance(comments, list):
            comments = []
        tried += [f"comment by {c.get('author', '?')}: {str(c.get('body', ''))[:200]}"
                  for c in comments[-2:] if isinstance(c, dict)]
        return [f"Stuck: {stuck}",
                "Tried: " + "; ".join(tried),
                f"Work time: {facts.get('worked_minutes', 0)} min in total, {facts.get('stretch_minutes', 0)} min in the current stretch",
                f"Remaining budget: {facts.get('remaining_minutes', 0)} min (master_minutes {self.settings.master_minutes}, "
                f"total_cap_minutes {self.settings.total_cap_minutes})"]

    def hand_over(self, incident: Incident) -> bool:
        key, facts = incident.ticket, incident.facts
        model, effort = self.model()
        try:
            self.jira.set_field(key, self.settings.taken_by_field, {"value": master_rescue.RESCUE_AGENT})
            if model:
                self.jira.set_field(key, self.cfg.model_field, {"value": f"{master_rescue.RESCUE_AGENT}:{model}"})
            moved = self.jira.transition(key, master_rescue.HANDOVER)
        except JiraError as error:
            self.op.sup_log(f"master rescue of {key} failed: {error}")
            moved = False
        if not moved:
            if not self.clear_handover_fields(key, bool(model)):
                self.redis.set_persistent(f"operator:rescue-cleanup:{key}", json.dumps({"model": bool(model)}))
            self.redis.set(f"operator:cool:{incident.id}", "1", 600)  # retry after the temporary cooldown
            return True
        self.redis.delete(f"lease:{key}")  # a worker still holding the ticket stops at its next heartbeat
        record = dict(state="active", started=int(time.time()), budget=facts.get("remaining_minutes", 0), model=f"{model}:{effort}".strip(":"),
                      stuck=facts.get("trigger", ""), findings=self.lines(incident), label_pending=True)
        self.redis.set_persistent(f"rescue:{key}", json.dumps(record))
        self.ensure_label(key, record)
        extra = self.lines(incident) + [f"Taken by: {master_rescue.RESCUE_AGENT}, model {model or 'default'}" + (f" (effort {effort})" if effort else "")]
        self.op.audit(incident, "master", f"HANDOVER to {master_rescue.RESCUE_AGENT}: status {master_rescue.HANDOVER}, label {master_rescue.RESCUE_LABEL}",
                      ["handover", f"label {master_rescue.RESCUE_LABEL}"], [], extra)
        events.emit("operator.rescue", ticket=key, outcome="handover", budget=record["budget"], model=record["model"])
        return True

    def ensure_label(self, key: str, record: dict[str, Any]) -> bool:
        """Retry the idempotent label write after Jira accepted Handover but rejected the label update."""
        if not record.get("label_pending", True):
            return True
        try:
            self.jira.add_label(key, master_rescue.RESCUE_LABEL)
        except JiraError as error:
            self.op.sup_log(f"master rescue of {key} moved to Handover but could not add its label: {error}")
            return False
        record["label_pending"] = False
        self.finish(key, record, record.get("state", "active"))
        return True

    def clear_taken_by(self, key: str) -> bool:
        """Clear Claude as the temporary owner after a refused Handover or unsuccessful rescue."""
        try:
            self.jira.set_field(key, self.settings.taken_by_field, None)
        except JiraError as error:
            self.op.sup_log(f"master rescue could not clear Taken by on {key}: {error}")
            return False
        return True

    def clear_model(self, key: str) -> bool:
        """Remove the rescue model field when Jira refuses the Handover transition."""
        try:
            self.jira.set_field(key, self.cfg.model_field, None)
        except JiraError as error:
            self.op.sup_log(f"master rescue could not clear the model field on {key}: {error}")
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
        if record.get("label_pending", True):
            self.ensure_label(key, record)
        if record.get("state") == "doctor_pending":
            self.flow_doctor(key, record, record.get("doctor_cause", "the rescue ended without a result"),
                             record.get("doctor_findings", ""))
            return
        if record.get("state") != "active":
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

    def rescue_status(self, key: str) -> str:
        try:
            return str(self.jira.view(key).get("status", "") or "")
        except JiraError:
            return ""

    def recent_findings(self, key: str, record: dict[str, Any]) -> str:
        """Include recent Claude or operator findings in the flow-doctor return comment."""
        try:
            comments = self.jira.comments(key, 3)
        except (JiraError, ValueError, TypeError):
            comments = []
        bodies = [str(item.get("body", "")) for item in comments if isinstance(item, dict)
                  and item.get("body") and not str(item.get("body", "")).startswith("AI Flow operator (master)")]
        return "\n".join(bodies) or "; ".join(record.get("findings", [])) or record.get("stuck", "")

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
        return self.flow_doctor(key, record, cause or "the rescue ended without a result", findings)

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
                self.op.sup_log(f"flow doctor could not comment on {key}: {error}")
                return False
            record["doctor_commented"] = True
            self.finish(key, record, "doctor_pending")
        if not self.clear_taken_by(key):
            return False
        try:
            status = str(self.jira.view(key).get("status", "") or "")
        except JiraError:
            return False
        if status != DOCTOR_TARGET and not self.jira.transition(key, DOCTOR_TARGET):
            return False
        self.op.sup.clear_block(key)
        self.finish(key, record, "failed")
        events.emit("operator.doctor", ticket=key, cause=cause[:300])
        return True
