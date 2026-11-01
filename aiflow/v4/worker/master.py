"""Master monitoring cycles: read a fenced control-plane snapshot and submit restricted action plans."""
from __future__ import annotations

import contextlib
import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import replace
from functools import partial
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


# Action bodies may carry only these fields. run_id and fence are added from the claim.
MASTER_ACTION_FIELDS = frozenset({
    "action", "ticket", "intent", "category", "headline", "what", "why", "result", "next", "fields",
    "add_labels", "remove_labels", "kind", "subject", "severity", "title", "detail", "target_status",
    "options", "observation_key", "account_id", "pause_minutes", "target_run_id", "signature"})
STRICT_FORMAT_REMINDER = (
    "\n\nSTRICT FORMAT RETRY: Your previous response was invalid. Return only one JSON object with exactly "
    "the required summary and actions fields. Every action must be an object with action and intent. "
    "Use an empty actions array if no safe action is needed. Do not include markdown or prose.")


def prepare_master_workspace(srv_root: str, run_as: list[str]) -> tuple[Path, Path]:
    """Create the master worktree and the run log directory."""
    work = Path(srv_root) / "work" / ".master"
    work.mkdir(parents=True, exist_ok=True)
    if run_as:
        with contextlib.suppress(LookupError, PermissionError):
            shutil.chown(work, "aiflow-worker", "aiflow-worker")
    if not (work / ".git").exists():
        subprocess.run(run_as + ["git", "init", "-q", str(work)], capture_output=True, timeout=30)
    logs = Path(srv_root) / "logs" / "runs"
    logs.mkdir(parents=True, exist_ok=True)
    return work, logs


def load_master_snapshot(api: ControlClient, claimed: Claimed) -> dict:
    """Read the fenced snapshot and attach this cycle's flow-bug reports."""
    snapshot = api.call("GET", "/v1/master/context", params={"run_id": claimed.run_id, "fence": claimed.fence})
    flow_bugs = api.call("POST", "/v1/master/flow-bugs", {"run_id": claimed.run_id, "fence": claimed.fence})
    snapshot["flow_bug_reports"] = flow_bugs.get("reports", [])
    return snapshot


def master_review_progress(snapshot: dict) -> dict:
    """Heartbeat progress for the snapshot before any model call."""
    return {"phase": "master-review", "incidents": len(snapshot.get("open_incidents", [])),
            "needs_owner": len(snapshot.get("needs_owner", [])),
            "running": len(snapshot.get("running_clocks", [])),
            "flow_bugs": len(snapshot["flow_bug_reports"])}


def finish_idle_master(result: dict, started: float) -> dict:
    """Record an idle cycle that did not call the model."""
    result.update(outcome="done", summary="no incidents, owner decisions, failures or overdue runs need review",
                  details={"no_watch": True, "model_time_s": 0, "duration_s": int(time.time() - started), "log": ""})
    return result


def master_model_timeout(snapshot: dict) -> int:
    """Turn the autonomy budget into the model timeout, capped at ten minutes."""
    budget = snapshot.get("autonomy") or {}
    remaining = budget.get("remaining_seconds")
    if remaining is None:
        return 600
    return max(1, min(600, int(remaining)))


def build_master_run_spec(claimed: Claimed, snapshot: dict, work: Path, srv_root: str, control_url: str,
                          logs: Path, timeout_s: int) -> RunSpec:
    """Build the read-only master run spec for this snapshot."""
    return RunSpec(prompt=build_prompt(claimed, snapshot), cwd=str(work),
                   account_home=str(Path(srv_root) / "accounts" / claimed.account), model=claimed.model,
                   effort=claimed.effort, timeout_s=timeout_s, control_url=control_url, run_token=claimed.run_token,
                   log_path=str(logs / f"master-{claimed.run_id}.jsonl"), sandbox="read-only")


def prefixed_master_argv(run_as: list[str], base_argv, current):
    """Prefix the adapter command with the worker's run-as command."""
    return run_as + base_argv(current)


def bind_master_argv(adapter, run_as: list[str]) -> None:
    """Replace the adapter command so the model runs under the same prefix as git."""
    adapter.argv = partial(prefixed_master_argv, run_as, adapter.argv)


def record_master_ai_progress(heartbeat: Heartbeat, events: dict, started: float, event: dict) -> None:
    """Update the heartbeat from one model event."""
    events["n"] += 1
    heartbeat.progress = {"phase": "master-ai", "events": events["n"],
                          "last": str(event.get("type", ""))[:40], "elapsed_s": int(time.time() - started)}


def master_quota_fields(responses) -> dict:
    """Quota fields from the newest response that reported quota."""
    quota_response = next((item for item in reversed(responses) if item.quota), None)
    if not quota_response:
        return {}
    observed = getattr(quota_response, "quota_observed_at", None)
    return {"quota": quota_response.quota, "quota_observed_at": observed.isoformat() if observed else None}


def finish_master_model_error(result: dict, response, claimed: Claimed, quota_fields: dict,
                              model_started: float) -> dict:
    """Record quota, auth, timeout or failure for the first model response."""
    if response.error_kind in ("quota", "auth"):
        result.update(outcome="quota", error_kind=response.error_kind,
                      summary=f"master account {claimed.account} cannot work now ({response.error_kind})",
                      tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                      **quota_fields, details={"model_time_s": math.ceil(time.monotonic() - model_started)})
        return result
    result.update(outcome="timeout" if response.error_kind == "timeout" else "failed",
                  error_kind=response.error_kind, summary=f"master cycle failed: {response.error_message[:400]}",
                  tokens_in=response.tokens_in, tokens_out=response.tokens_out, cost_usd=response.cost_usd,
                  **quota_fields, details={"model_time_s": math.ceil(time.monotonic() - model_started)})
    return result


def finish_master_retry_error(result: dict, responses, response, claimed: Claimed, quota_fields: dict,
                              model_started: float) -> None:
    """Record a provider failure during the strict-format retry."""
    result.update(tokens_in=sum(item.tokens_in for item in responses),
                  tokens_out=sum(item.tokens_out for item in responses),
                  cost_usd=sum(item.cost_usd for item in responses), **quota_fields,
                  error_kind=response.error_kind,
                  reset_at=response.reset_at.isoformat() if response.reset_at else None)
    if response.error_kind in ("quota", "auth"):
        result.update(outcome="quota",
                      summary=f"master account {claimed.account} cannot work now ({response.error_kind})")
    else:
        failed = f"master cycle failed during strict-format retry: {response.error_message[:400]}"
        result.update(outcome="timeout" if response.error_kind == "timeout" else "failed", summary=failed)
    result["details"] = {"plan_attempts": len(responses),
                         "model_time_s": math.ceil(time.monotonic() - model_started)}


def retry_invalid_master_plan(adapter, spec: RunSpec, heartbeat: Heartbeat, on_progress, result: dict, responses,
                              response, claimed: Claimed, quota_fields: dict, timeout_s: int, model_started: float):
    """Retry once when time remains. Return the plan, the latest response, quota fields and a finished result."""
    retry_budget = timeout_s - math.ceil(time.monotonic() - model_started)
    if retry_budget <= 0:
        return None, response, quota_fields, None
    retry_spec = replace(spec, prompt=spec.prompt + STRICT_FORMAT_REMINDER, timeout_s=min(600, retry_budget))
    response = run_with_inbox(adapter, retry_spec, heartbeat, on_progress)
    responses.append(response)
    quota_fields = master_quota_fields(responses)
    if heartbeat.lost.is_set():
        raise LeaseLost()
    if response.error_kind:
        finish_master_retry_error(result, responses, response, claimed, quota_fields, model_started)
        return None, response, quota_fields, result
    return parse_plan(response.final_text or ""), response, quota_fields, None


def record_master_usage(result: dict, responses, response, quota_fields: dict) -> None:
    """Store token totals and the latest reset time on the cycle result."""
    result.update(tokens_in=sum(item.tokens_in for item in responses),
                  tokens_out=sum(item.tokens_out for item in responses),
                  cost_usd=sum(item.cost_usd for item in responses), **quota_fields,
                  error_kind=response.error_kind,
                  reset_at=response.reset_at.isoformat() if response.reset_at else None)


def master_action_payload(action: dict, claimed: Claimed) -> dict:
    """Keep the allowed action fields and stamp the fenced run."""
    payload = {key: value for key, value in action.items() if key in MASTER_ACTION_FIELDS}
    payload.update(run_id=claimed.run_id, fence=claimed.fence)
    return payload


def perform_master_actions(api: ControlClient, claimed: Claimed, heartbeat: Heartbeat, result: dict, plan: dict,
                           model_time_s: int, started: float, response) -> dict:
    """Submit each planned action. A fencing conflict stops the cycle; other refusals are counted."""
    completed, escalated, denied = 0, 0, 0
    for action in plan["actions"]:
        if heartbeat.lost.is_set():
            raise LeaseLost()
        payload = master_action_payload(action, claimed)
        try:
            answer = api.call("POST", "/v1/master/actions", payload)
            completed += 1
            escalated += bool(answer.get("escalated"))
        except Conflict:
            raise
        except Exception as exc:  # noqa: BLE001
            denied += 1
            log.warning("master action refused for run %s (%s): %s", claimed.run_id, payload.get("action", ""), exc)
    result.update(outcome="done", summary=plan["summary"], details={"actions": len(plan["actions"]),
                                                                     "completed": completed,
                                                                     "escalated": escalated, "refused": denied,
                                                                     "model_time_s": model_time_s,
                                                                     "duration_s": int(time.time() - started),
                                                                     "log": response.log_path})
    return result


def apply_master_plan(api: ControlClient, claimed: Claimed, heartbeat: Heartbeat, result: dict, plan, responses,
                      response, quota_fields: dict, model_started: float, started: float) -> dict:
    """Record usage, report whether the plan parsed, and perform its actions."""
    record_master_usage(result, responses, response, quota_fields)
    model_time_s = math.ceil(time.monotonic() - model_started)
    api.call("POST", "/v1/master/plan-outcome", {"run_id": claimed.run_id, "fence": claimed.fence,
                                                 "valid": bool(plan)})
    if not plan:
        result["summary"] = "master returned no valid JSON action plan after the strict-format retry"
        result["details"] = {"plan_attempts": len(responses), "invalid_plan": True, "model_time_s": model_time_s,
                             "output": (response.final_text or "")[-1000:], "log": response.log_path}
        return result
    return perform_master_actions(api, claimed, heartbeat, result, plan, model_time_s, started, response)


def run_master_cycle(api: ControlClient, claimed: Claimed, heartbeat: Heartbeat, result: dict, work: Path,
                     logs: Path, srv_root: str, control_url: str, run_as: list[str], started: float) -> dict:
    """Review one snapshot and return the cycle result. Lease loss and conflicts propagate."""
    snapshot = load_master_snapshot(api, claimed)
    heartbeat.progress = master_review_progress(snapshot)
    if not needs_model_review(snapshot):
        return finish_idle_master(result, started)
    timeout_s = master_model_timeout(snapshot)
    spec = build_master_run_spec(claimed, snapshot, work, srv_root, control_url, logs, timeout_s)
    adapter = get_adapter(claimed.provider)
    bind_master_argv(adapter, run_as)
    events = {"n": 0}
    on_progress = partial(record_master_ai_progress, heartbeat, events, started)
    model_started = time.monotonic()
    response = run_with_inbox(adapter, spec, heartbeat, on_progress)
    responses = [response]
    if heartbeat.lost.is_set():
        raise LeaseLost()
    quota_fields = master_quota_fields(responses)
    if response.error_kind:
        return finish_master_model_error(result, response, claimed, quota_fields, model_started)
    plan = parse_plan(response.final_text or "")
    if not plan:
        plan, response, quota_fields, finished = retry_invalid_master_plan(
            adapter, spec, heartbeat, on_progress, result, responses, response, claimed, quota_fields, timeout_s,
            model_started)
        if finished is not None:
            return finished
    return apply_master_plan(api, claimed, heartbeat, result, plan, responses, response, quota_fields,
                             model_started, started)


def run_master(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    """Run one account-backed, fenced master pass. All effects are dispatched through /v1/master/actions."""
    hb = Heartbeat(api, c, heartbeat_s)
    result: dict[str, Any] = {"fence": c.fence, "outcome": "failed", "details": {}}
    work, logs = prepare_master_workspace(srv_root, run_as)
    started = time.time()
    hb.start()
    try:
        return run_master_cycle(api, c, hb, result, work, logs, srv_root, control_url, run_as, started)
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
