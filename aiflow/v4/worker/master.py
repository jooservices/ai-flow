"""Master monitoring cycles: read a fenced control-plane snapshot and submit restricted action plans."""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, run_with_inbox, with_knowledge_context

log = logging.getLogger("aiflow4.master")


def build_prompt(c: Claimed, snapshot: dict[str, Any]) -> str:
    template = (REPO_ROOT / "prompts" / "v4" / "master.md").read_text(encoding="utf-8")
    values = {"account": c.account, "provider": c.provider, "model": c.model or "unknown",
              "node": c.project.get("node", ""), "run": c.run_id, "fence": c.fence,
              "snapshot": json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)}
    for key, value in values.items():
        template = template.replace("{" + key + "}", str(value))
    return with_knowledge_context(template, c.context_pack)


def parse_plan(text: str) -> dict[str, Any] | None:
    """Accept one JSON action plan and reject prose or malformed action lists."""
    try:
        plan = json.loads((text or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(plan, dict) or not isinstance(plan.get("summary"), str):
        return None
    actions = plan.get("actions")
    if not isinstance(actions, list) or len(actions) > 20 or any(not isinstance(a, dict) for a in actions):
        return None
    if any(not isinstance(a.get("action"), str) or not isinstance(a.get("intent"), str) for a in actions):
        return None
    return {"summary": plan["summary"][:1000], "actions": actions}


def needs_model_review(snapshot: dict[str, Any]) -> bool:
    """Skip paid model calls when the control plane has no item that needs a decision."""
    if any(snapshot.get(key) for key in ("open_incidents", "needs_owner", "offline_nodes", "quota_accounts",
                                         "quota_incidents", "flow_bug_reports")):
        return True
    return any("hard" in (clock.get("passed") or []) for clock in snapshot.get("running_clocks", []))


def run_master(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    """Run one account-backed, fenced master pass. All effects are dispatched through /v1/master/actions."""
    hb = Heartbeat(api, c, heartbeat_s)
    result: dict[str, Any] = {"fence": c.fence, "outcome": "failed", "details": {}}
    work = Path(srv_root) / "work" / ".master"
    work.mkdir(parents=True, exist_ok=True)
    if run_as:
        try:
            shutil.chown(work, "aiflow-worker", "aiflow-worker")
        except (LookupError, PermissionError):
            pass
    if not (work / ".git").exists():
        subprocess.run(run_as + ["git", "init", "-q", str(work)], capture_output=True, timeout=30)
    logs = Path(srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    hb.start()
    try:
        snapshot = api.call("GET", "/v1/master/context", params={"run_id": c.run_id, "fence": c.fence})
        flow_bugs = api.call("POST", "/v1/master/flow-bugs", {"run_id": c.run_id, "fence": c.fence})
        snapshot["flow_bug_reports"] = flow_bugs.get("reports", [])
        hb.progress = {"phase": "master-review", "incidents": len(snapshot.get("open_incidents", [])),
                       "needs_owner": len(snapshot.get("needs_owner", [])),
                       "running": len(snapshot.get("running_clocks", [])),
                       "flow_bugs": len(snapshot["flow_bug_reports"])}
        if not needs_model_review(snapshot):
            result.update(outcome="done", summary="no incidents, owner decisions, failures or overdue runs need review",
                          details={"no_watch": True, "model_time_s": 0,
                                   "duration_s": int(time.time() - t0), "log": ""})
            return result
        budget = snapshot.get("autonomy") or {}
        remaining = budget.get("remaining_seconds")
        timeout_s = 600 if remaining is None else max(1, min(600, int(remaining)))
        spec = RunSpec(prompt=build_prompt(c, snapshot), cwd=str(work),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                       timeout_s=timeout_s, control_url=control_url, run_token=c.run_token,
                       log_path=str(logs / f"master-{c.run_id}.jsonl"), sandbox="read-only")
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda current: run_as + base_argv(current)
        events = {"n": 0}

        def on_progress(event: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "master-ai", "events": events["n"],
                           "last": str(event.get("type", ""))[:40], "elapsed_s": int(time.time() - t0)}

        model_started = time.monotonic()
        response = run_with_inbox(adapter, spec, hb, on_progress)
        responses = [response]
        if hb.lost.is_set():
            raise LeaseLost()
        quota_response = next((item for item in reversed(responses) if item.quota), None)
        quota_fields = {"quota": quota_response.quota,
                        "quota_observed_at": (quota_response.quota_observed_at.isoformat()
                                               if quota_response.quota_observed_at else None)} if quota_response else {}
        if response.error_kind in ("quota", "auth"):
            result.update(outcome="quota", error_kind=response.error_kind,
                          summary=f"master account {c.account} cannot work now ({response.error_kind})",
                          tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                          **quota_fields,
                          details={"model_time_s": math.ceil(time.monotonic() - model_started)})
            return result
        if response.error_kind:
            result.update(outcome="timeout" if response.error_kind == "timeout" else "failed",
                          error_kind=response.error_kind, summary=f"master cycle failed: {response.error_message[:400]}",
                          tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                          **quota_fields,
                          details={"model_time_s": math.ceil(time.monotonic() - model_started)})
            return result
        plan = parse_plan(response.final_text or "")
        if not plan:
            retry_budget = timeout_s - math.ceil(time.monotonic() - model_started)
            if retry_budget > 0:
                reminder = ("\n\nSTRICT FORMAT RETRY: Your previous response was invalid. Return only one JSON object with exactly "
                            "the required summary and actions fields. Every action must be an object with action and intent. "
                            "Use an empty actions array if no safe action is needed. Do not include markdown or prose.")
                retry_spec = replace(spec, prompt=spec.prompt + reminder, timeout_s=min(600, retry_budget))
                response = run_with_inbox(adapter, retry_spec, hb, on_progress)
                responses.append(response)
                quota_response = next((item for item in reversed(responses) if item.quota), None)
                quota_fields = {"quota": quota_response.quota,
                                "quota_observed_at": (quota_response.quota_observed_at.isoformat()
                                                       if quota_response.quota_observed_at else None)} if quota_response else {}
                if hb.lost.is_set():
                    raise LeaseLost()
                if response.error_kind:
                    result.update(tokens_in=sum(r.tokens_in for r in responses),
                                  tokens_out=sum(r.tokens_out for r in responses),
                                  cost_usd=sum(r.cost_usd for r in responses), **quota_fields,
                                  error_kind=response.error_kind,
                                  reset_at=response.reset_at.isoformat() if response.reset_at else None)
                    if response.error_kind in ("quota", "auth"):
                        result.update(outcome="quota", summary=f"master account {c.account} cannot work now ({response.error_kind})")
                    else:
                        result.update(outcome="timeout" if response.error_kind == "timeout" else "failed",
                                      summary=f"master cycle failed during strict-format retry: {response.error_message[:400]}")
                    result["details"] = {"plan_attempts": len(responses),
                                          "model_time_s": math.ceil(time.monotonic() - model_started)}
                    return result
                plan = parse_plan(response.final_text or "")

        result.update(tokens_in=sum(r.tokens_in for r in responses), tokens_out=sum(r.tokens_out for r in responses),
                      cost_usd=sum(r.cost_usd for r in responses), **quota_fields,
                      error_kind=response.error_kind,
                      reset_at=response.reset_at.isoformat() if response.reset_at else None)
        model_time_s = math.ceil(time.monotonic() - model_started)
        api.call("POST", "/v1/master/plan-outcome", {"run_id": c.run_id, "fence": c.fence,
                                                       "valid": bool(plan)})
        if not plan:
            result["summary"] = "master returned no valid JSON action plan after the strict-format retry"
            result["details"] = {"plan_attempts": len(responses), "invalid_plan": True,
                                 "model_time_s": model_time_s, "output": (response.final_text or "")[-1000:],
                                 "log": response.log_path}
            return result

        completed, escalated, denied = 0, 0, 0
        for action in plan["actions"]:
            if hb.lost.is_set():
                raise LeaseLost()
            payload = {key: value for key, value in action.items() if key in {
                "action", "ticket", "intent", "category", "headline", "what", "why", "result", "next", "fields",
                "add_labels", "remove_labels", "kind", "subject", "severity", "title", "detail", "target_status",
                "options", "observation_key", "account_id", "pause_minutes", "target_run_id", "signature"}}
            payload.update(run_id=c.run_id, fence=c.fence)
            try:
                answer = api.call("POST", "/v1/master/actions", payload)
                completed += 1
                escalated += bool(answer.get("escalated"))
            except Conflict:
                raise
            except Exception as exc:  # noqa: BLE001
                denied += 1
                log.warning("master action refused for run %s (%s): %s", c.run_id, payload.get("action", ""), exc)
        result.update(outcome="done", summary=plan["summary"], details={"actions": len(plan["actions"]),
                                                                         "completed": completed,
                                                                         "escalated": escalated, "refused": denied,
                                                                         "model_time_s": model_time_s,
                                                                         "duration_s": int(time.time() - t0),
                                                                         "log": response.log_path})
        return result
    except LeaseLost:
        result.update(outcome="lost", summary="master lease lost; stopped without further actions")
        return result
    except Conflict:
        result.update(outcome="lost", summary="master fencing token refused; another account owns the cycle")
        return result
    except Exception as exc:  # noqa: BLE001
        log.exception("master cycle failed")
        result.update(outcome="failed", summary=f"master worker error: {exc}"[:1000])
        return result
    finally:
        hb.stop.set()
