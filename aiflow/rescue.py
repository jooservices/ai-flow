"""Master AI rescue run (JAIF-92): performs the `rescue` incidents of the operator queue (rules: `master_rescue.py`).

    rescue incident -> Claude available?  yes: Handover status, Taken by = Claude, label master-rescue, audit comment   (one per ticket)
                                          no (quota / offline): the owner at once
                    -> owner-only work or no budget left: the owner at once
    Claude's rescue budget expires without a result -> the flow doctor records the cause and returns
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
        tried += [f"comment by {c.get('author', '?')}: {str(c.get('body', ''))[:200]}" for c in facts.get("last_comments", [])[-2:]]
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
            self.clear_taken_by(key)
            self.redis.set(f"operator:cool:{incident.id}", "1", 600)  # retry after the temporary cooldown
            return True
        self.redis.delete(f"lease:{key}")  # a worker still holding the ticket stops at its next heartbeat
        record = dict(state="active", started=int(time.time()), budget=facts.get("remaining_minutes", 0), model=f"{model}:{effort}".strip(":"),
                      stuck=facts.get("trigger", ""), findings=self.lines(incident))
        self.redis.set_persistent(f"rescue:{key}", json.dumps(record))
        try:
            self.jira.add_label(key, master_rescue.RESCUE_LABEL)
        except JiraError as error:
            self.op.sup_log(f"master rescue of {key} moved to Handover but could not add its label: {error}")
        extra = self.lines(incident) + [f"Taken by: {master_rescue.RESCUE_AGENT}, model {model or 'default'}" + (f" (effort {effort})" if effort else "")]
        self.op.audit(incident, "master", f"HANDOVER to {master_rescue.RESCUE_AGENT}: status {master_rescue.HANDOVER}, label {master_rescue.RESCUE_LABEL}",
                      ["handover", f"label {master_rescue.RESCUE_LABEL}"], [], extra)
        events.emit("operator.rescue", ticket=key, outcome="handover", budget=record["budget"], model=record["model"])
        return True

    def clear_taken_by(self, key: str) -> None:
        """Undo the temporary owner field when Jira refuses the Handover transition."""
        try:
            self.jira.set_field(key, self.settings.taken_by_field, None)
        except JiraError as error:
            self.op.sup_log(f"master rescue could not clear Taken by on {key}: {error}")

    # ---- the end of a rescue -----------------------------------------------------------------------------------------
    def check_rescues(self) -> None:
        """A rescue whose budget ran out while the ticket is still in Handover ended without a result."""
        for name in self.redis.scan("rescue:*"):
            key = name.split(":", 1)[1]
            try:
                record = json.loads(self.redis.get(name) or "{}")
            except (TypeError, ValueError):
                continue
            if record.get("state") != "active" or time.time() - record.get("started", 0) < record.get("budget", 0) * 60:
                continue
            try:
                status = str(self.jira.view(key).get("status", ""))
            except JiraError:
                continue
            if status == master_rescue.HANDOVER:
                self.end(key, ok=False, cause=f"the rescue budget of {record.get('budget', 0)} min ran out without a result")
            else:
                self.finish(key, record, "finished")

    def finish(self, key: str, record: dict[str, Any], state: str) -> None:
        self.redis.set_persistent(f"rescue:{key}", json.dumps({**record, "state": state}))

    def end(self, key: str, ok: bool, cause: str = "", findings: str = "") -> None:
        """Record a terminal rescue outcome; the budget-expiry path sends a missing result to the flow doctor."""
        try:
            record = json.loads(self.redis.get(f"rescue:{key}") or "{}")
        except (TypeError, ValueError):
            record = {}
        if ok:
            self.finish(key, record, "finished")
            return
        self.flow_doctor(key, record, cause or "the rescue ended without a result", findings)

    def flow_doctor(self, key: str, record: dict[str, Any], cause: str, findings: str) -> None:
        """Record the cause and return the ticket to development with its findings; no escalation to a person."""
        notes = findings or "; ".join(record.get("findings", [])) or record.get("stuck", "")
        self.jira.comment(key, f"AI Flow doctor: Claude's rescue of {key} ended without a result.\nCause: {cause}\nFindings: {notes}\n"
                               f"The ticket returns to development ({DOCTOR_TARGET}); it is not escalated to a person.")
        self.clear_taken_by(key)
        self.jira.transition(key, DOCTOR_TARGET)
        self.op.sup.clear_block(key)
        self.finish(key, record, "failed")
        events.emit("operator.doctor", ticket=key, cause=cause[:300])
