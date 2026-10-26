"""Worker-side pickup and completion for the legacy operator's master rescues."""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from . import agents, events, master_rescue
from .rescue import Rescue, safe_run_findings


class MasterRescueWorker:
    """Keep rescue-specific worker policy outside the large supervisor module."""

    def __init__(self, supervisor: Any) -> None:
        self.sup = supervisor

    def active_record(self, issue: dict[str, Any]) -> dict[str, Any] | None:
        if (issue.get("status") != master_rescue.HANDOVER
                or master_rescue.RESCUE_LABEL not in issue.get("labels", [])):
            return None
        try:
            record = json.loads(self.sup.redis.get(f"rescue:{issue['key']}") or "{}")
        except (TypeError, ValueError, AttributeError):
            return None
        if not self.confirmed(record):
            return None
        timing = self.timing(record)
        if timing is None:
            return None
        started, budget = timing
        return record if time.time() < started + budget * 60 else None

    @staticmethod
    def confirmed(record: Any) -> bool:
        return (isinstance(record, dict) and record.get("state") == "active"
                and not record.get("label_pending") and not record.get("handover_audit_pending"))

    @staticmethod
    def timing(record: dict[str, Any]) -> tuple[float, float] | None:
        try:
            started_raw, budget_raw = record.get("started"), record.get("budget")
            if isinstance(started_raw, bool) or isinstance(budget_raw, bool):
                return None
            started, budget = float(started_raw), float(budget_raw)
        except (TypeError, ValueError):
            return None
        return (started, budget) if math.isfinite(started) and math.isfinite(budget) and budget > 0 else None

    def reserve(self, key: str) -> tuple[str | None, str | None]:
        agent = master_rescue.RESCUE_AGENT
        rescue = Rescue(self.sup)
        unavailable = rescue.claude_unavailable()
        if unavailable:
            rescue.defer_unstarted_to_owner(key, unavailable)
            return None, None
        if not self.online_here():
            return None, None
        owner = f"{self.sup.cfg.owner}:{key}:master_rescue:{uuid.uuid4().hex}"
        limit = self.sup.cfg.ai_limits.get(agent.casefold())
        if not self.sup.redis.acquire_session(self.sup.cfg.machine, agent, owner, self.sup.cfg.lease_seconds, limit):
            return None, None
        if not rescue.mark_worker_started(key):
            self.sup.redis.release_session(self.sup.cfg.machine, agent, owner)
            return None, None
        events.emit("ai.assign", ticket=key, role="master_rescue", agent=agent, limit=limit, from_field=True)
        return agent, owner

    def online_here(self) -> bool:
        agent = master_rescue.RESCUE_AGENT
        probe = self.sup.redis.get(f"probe:{self.sup.cfg.machine}:{agent}")
        return agent in agents.available((agent,)) and bool(probe) and not probe.startswith("down")

    def runtime(self, record: dict[str, Any]) -> tuple[tuple[str, str], float]:
        """Return the saved model and absolute rescue deadline; setup time counts against the budget."""
        spec = str(record.get("model", "") or "")
        if spec:
            model, _, effort = spec.partition(":")
        else:
            model, effort = self.sup.cfg.agent_models.get(master_rescue.RESCUE_AGENT, ("", ""))
        deadline = float(record["started"]) + float(record["budget"]) * 60
        return (model, effort), deadline

    @staticmethod
    def execution_seconds(deadline: float | None, default: int) -> int:
        """Recalculate the timeout after prompt, context, and environment setup, immediately before launch."""
        return max(0, int(deadline - time.time())) if deadline is not None else default

    def finish_attempt(self, key: str, view: dict[str, Any], repo: Any, base: str, agent: str,
                       workdir: Any, branch: str, text: str, why: str,
                       found: tuple[str, str] | None, quota: bool) -> bool:
        if quota:
            self.sup.quota_hit(key, agent, "master_rescue")
            cause = f"Claude rescue ended ({why})"
        elif found and found[0] == "DONE" and repo.ahead(workdir, base) > 0:
            if not self.begin_completion(key):
                return True
            if self.sup.finish_development(key, view, repo, base, agent, workdir, branch, text):
                return self.end(key, True)
            if Rescue(self.sup).reconcile_failed_completion(key):
                return True
            cause = "Claude's result could not complete the normal development handoff"
        elif found and found[0] == "BLOCKED":
            cause = "Claude reported that the rescue remained blocked"
        else:
            cause = ("Claude ended without a DONE result and committed changes" if why == "ok"
                     else f"Claude rescue ended ({why})")
        return self.end(key, False, cause, safe_run_findings(text, found))

    def end(self, key: str, ok: bool, cause: str = "", findings: str = "") -> bool:
        return Rescue(self.sup).end(key, ok=ok, cause=cause, findings=findings)

    def begin_completion(self, key: str) -> bool:
        return Rescue(self.sup).begin_completion(key)
