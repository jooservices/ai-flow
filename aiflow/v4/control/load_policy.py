"""Deterministic machine and host load gates for work offers.

Load is sampled by node heartbeats. A held machine stays held until it clears a fixed recovery margin, while
existing runs remain untouched. Host limits use the heartbeat hostname shared by machines on the same host.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

DEFAULT_MACHINE_LIMITS = {"cpu_load_per_cpu": 0.85, "memory_used_pct": 85.0, "disk_free_pct": 10.0}
DEFAULT_HOST_LIMITS = {"cpu_load_per_cpu": 0.85}
CPU_MARGIN = 0.05
PERCENT_MARGIN = 5.0
THROTTLE_BAND = 0.70


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def host_name(metrics: Any) -> str:
    raw = str(metrics.get("hostname") or "").strip().lower() if isinstance(metrics, dict) else ""
    return raw if len(raw) <= 253 and re.fullmatch(r"[a-z0-9_.-]+", raw) else ""


def machine_limits(raw: Any) -> dict[str, float]:
    values = dict(DEFAULT_MACHINE_LIMITS)
    if isinstance(raw, dict):
        for key in values:
            value = _number(raw.get(key))
            if value is not None:
                values[key] = value
    return values


def host_limits(raw: Any) -> dict[str, float]:
    value = _number(raw.get("cpu_load_per_cpu")) if isinstance(raw, dict) else None
    return {"cpu_load_per_cpu": value if value is not None else DEFAULT_HOST_LIMITS["cpu_load_per_cpu"]}


def measured(metrics: Any) -> dict[str, float | None]:
    metrics = metrics if isinstance(metrics, dict) else {}
    load1, cpus = _number(metrics.get("load1")), _number(metrics.get("cpus"))
    mem_used, mem_total = _number(metrics.get("mem_used")), _number(metrics.get("mem_total"))
    disk_free, disk_total = _number(metrics.get("disk_free")), _number(metrics.get("disk_total"))
    disk_pct = _number(metrics.get("disk_free_pct"))
    if disk_pct is None and disk_total and disk_total > 0 and disk_free is not None:
        disk_pct = disk_free * 100.0 / disk_total
    return {
        "cpu_load_per_cpu": load1 / cpus if load1 is not None and cpus is not None and cpus > 0 else None,
        "memory_used_pct": mem_used * 100.0 / mem_total if mem_used is not None and mem_total and mem_total > 0 else None,
        "disk_free_pct": disk_pct,
    }


def _metric_label(metric: str, *, host: bool = False) -> str:
    if metric == "cpu_load_per_cpu":
        return ("host CPU load" if host else "CPU load") + " {value:.2f} per CPU"
    if metric == "memory_used_pct":
        return "memory use {value:.1f}%"
    return "free disk {value:.1f}%"


def evaluate_machine(metrics: Any, limits_raw: Any, previous: Any = None,
                      at: datetime | None = None) -> dict[str, Any]:
    limits, values = machine_limits(limits_raw), measured(metrics)
    previous = previous if isinstance(previous, dict) else {}
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    hold_metrics: list[str] = []
    for metric, threshold in limits.items():
        value = values.get(metric)
        if value is None:
            continue
        violated = value >= threshold if metric != "disk_free_pct" else value <= threshold
        if violated:
            hold_metrics.append(metric)
    # Hysteresis applies only to the measurements that caused the current hold.
    for metric in previous.get("hold_metrics", []) if previous.get("mode") == "held" else []:
        if metric in hold_metrics:
            continue
        value = values.get(metric)
        if value is None:
            hold_metrics.append(metric)
            continue
        threshold = limits[metric]
        margin = CPU_MARGIN if metric == "cpu_load_per_cpu" else PERCENT_MARGIN
        recovered = value < max(0.0, threshold - margin) if metric != "disk_free_pct" else value > min(100.0, threshold + margin)
        if not recovered:
            hold_metrics.append(metric)
    # Stable cause order makes the screen and event stream deterministic when several limits are crossed.
    order = ("cpu_load_per_cpu", "memory_used_pct", "disk_free_pct")
    hold_metrics = [metric for metric in order if metric in hold_metrics]
    if hold_metrics:
        metric = hold_metrics[0]
        value = values.get(metric)
        reason = "held back: " + _metric_label(metric).format(value=value if value is not None else 0)
        return {"mode": "held", "reason": reason, "metric": metric, "value": value,
                "hold_metrics": hold_metrics,
                "held_at": previous.get("held_at") if previous.get("mode") == "held" else stamp,
                "measured_at": stamp, "values": values, "limits": limits, "throttle_ratio": 0.0}
    cpu = values.get("cpu_load_per_cpu")
    cpu_limit = limits["cpu_load_per_cpu"]
    if cpu is not None and cpu >= THROTTLE_BAND * cpu_limit:
        return {"mode": "throttled", "reason": "reduced offers: CPU load {:.2f} per CPU".format(cpu),
                "metric": "cpu_load_per_cpu", "value": cpu, "hold_metrics": [],
                "held_at": None, "measured_at": stamp, "values": values, "limits": limits,
                "throttle_ratio": 0.5}
    return {"mode": "normal", "reason": "", "metric": "", "value": None, "hold_metrics": [],
            "held_at": None, "measured_at": stamp, "values": values, "limits": limits,
            "throttle_ratio": 1.0}


def evaluate_host(metrics: Any, limit_raw: Any, previous: Any = None, *, active_runs: int = 0,
                  at: datetime | None = None) -> dict[str, Any]:
    limits = host_limits(limit_raw)
    value_map = measured(metrics)
    value, cpus = value_map["cpu_load_per_cpu"], _number((metrics or {}).get("cpus")) if isinstance(metrics, dict) else None
    load1 = _number((metrics or {}).get("load1")) if isinstance(metrics, dict) else None
    previous = previous if isinstance(previous, dict) else {}
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    threshold = limits["cpu_load_per_cpu"]
    over = value is not None and value >= threshold
    was_held = previous.get("mode") == "held"
    recovered = value is not None and value < max(0.0, threshold - CPU_MARGIN)
    held = over or (was_held and not recovered)
    if held:
        reason = "held back: " + _metric_label("cpu_load_per_cpu", host=True).format(value=value or 0.0)
        mode, ratio = "held", 0.0
        held_at = previous.get("held_at") if was_held else stamp
    elif value is not None and value >= THROTTLE_BAND * threshold:
        reason = "reduced host offers: CPU load {:.2f} per CPU".format(value)
        mode, ratio, held_at = "throttled", 0.5, None
    else:
        reason, mode, ratio, held_at = "", "normal", 1.0, None
    active = max(0, int(active_runs))
    # Bound concurrent claims across every machine on this host by its configured CPU budget. The recent measured
    # load remains the hard gate; this fixed ceiling makes simultaneous claims share host capacity.
    baseline = max(0.0, (load1 or 0.0) - active) if load1 is not None else None
    max_runs = None
    if cpus is not None:
        hard_ceiling = math.floor(threshold * cpus)
        max_runs = active if mode == "held" else hard_ceiling
    return {"mode": mode, "reason": reason, "metric": "cpu_load_per_cpu" if mode == "held" else "",
            "value": value, "held_at": held_at, "measured_at": stamp, "limits": limits,
            "throttle_ratio": ratio, "load1": load1, "cpus": cpus,
            "active_at_measurement": active, "baseline_load1": baseline, "max_runs": max_runs}


def current_violations(metrics: Any, limits_raw: Any, host_limit_raw: Any = None) -> list[dict[str, Any]]:
    """Current hard-limit violations, without the no-flapping hold margin."""
    values, limits = measured(metrics), machine_limits(limits_raw)
    out = []
    for metric, threshold in limits.items():
        value = values.get(metric)
        if value is not None and (value >= threshold if metric != "disk_free_pct" else value <= threshold):
            out.append({"scope": "machine", "metric": metric, "value": value, "limit": threshold})
    host_limit = host_limits(host_limit_raw)["cpu_load_per_cpu"]
    cpu = values["cpu_load_per_cpu"]
    if cpu is not None and cpu >= host_limit:
        out.append({"scope": "host", "metric": "cpu_load_per_cpu", "value": cpu, "limit": host_limit})
    return out


def record_heartbeat(ctx: Any, node: str) -> None:
    """Persist the current machine and host state and emit only state transitions."""
    row = ctx.db.one("SELECT metrics,load_limits,load_state FROM nodes WHERE name=%s", (node,))
    if not row:
        return
    metrics = row.get("metrics") or {}
    previous = row.get("load_state") or {}
    state = evaluate_machine(metrics, row.get("load_limits"), previous)
    ctx.db.run("UPDATE nodes SET load_state=%s WHERE name=%s", (ctx_json(state), node))
    if previous.get("mode") and (state["mode"] != previous.get("mode") or
                                  (state["mode"] == "held" and state.get("metric") != previous.get("metric"))):
        resumed = previous.get("mode") == "held" or state["mode"] == "normal"
        event = "machine.load_resumed" if resumed else "machine.load_throttled"
        ctx.events.emit(event, node=node, outcome=state["mode"], reason=state["reason"],
                        data={"scope": "machine", "cpu_load_per_cpu": state["values"].get("cpu_load_per_cpu"),
                              "memory_used_pct": state["values"].get("memory_used_pct"),
                              "disk_free_pct": state["values"].get("disk_free_pct"),
                              "limits": state["limits"], "measured_at": state["measured_at"]})
    hostname = host_name(metrics)
    if not hostname:
        return
    host = ctx.db.one("SELECT cpu_load_per_cpu,state FROM machine_host_load_limits WHERE hostname=%s", (hostname,))
    limits = {"cpu_load_per_cpu": float(host["cpu_load_per_cpu"])} if host else DEFAULT_HOST_LIMITS
    active = int(ctx.db.one("SELECT count(*) AS n FROM runs r JOIN nodes n ON n.name=r.node"
                            " WHERE r.status='running' AND lower(n.metrics->>'hostname')=%s", (hostname,)).get("n") or 0)
    old_host_state = (host or {}).get("state") or {}
    host_state = evaluate_host(metrics, limits, old_host_state, active_runs=active)
    ctx.db.run("INSERT INTO machine_host_load_limits (hostname,cpu_load_per_cpu,state,updated_at)"
               " VALUES (%s,%s,%s,now()) ON CONFLICT (hostname) DO UPDATE SET state=EXCLUDED.state,updated_at=now()",
               (hostname, limits["cpu_load_per_cpu"], ctx_json(host_state)))
    if old_host_state.get("mode") and host_state["mode"] != old_host_state.get("mode"):
        resumed = old_host_state.get("mode") == "held" or host_state["mode"] == "normal"
        event = "host.load_resumed" if resumed else "host.load_throttled"
        ctx.events.emit(event, node=node, outcome=host_state["mode"], reason=host_state["reason"],
                        data={"scope": "host", "hostname": hostname,
                              "cpu_load_per_cpu": host_state.get("value"), "load1": host_state.get("load1"),
                              "cpus": host_state.get("cpus"), "limit": limits["cpu_load_per_cpu"],
                              "max_runs": host_state.get("max_runs"), "measured_at": host_state["measured_at"]})


def ctx_json(value: Any) -> Any:
    from .db import J
    return J(value)


def offer_status(ctx: Any, node: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT metrics,load_limits,load_state,slots_total FROM nodes WHERE name=%s"
                     " AND removed_at IS NULL", (node,)) or {}
    state = row.get("load_state") or {}
    metrics = row.get("metrics") or {}
    hostname = host_name(metrics)
    host = ctx.db.one("SELECT cpu_load_per_cpu,state FROM machine_host_load_limits WHERE hostname=%s", (hostname,)) if hostname else None
    host_state = (host or {}).get("state") or {}
    machine_state = evaluate_machine(metrics, row.get("load_limits"), state,
                                     at=datetime.fromisoformat(state["measured_at"]) if state.get("measured_at") else None)
    if machine_state["mode"] == "held":
        return {"allowed": False, "reason": machine_state["reason"], "machine_state": machine_state,
                "host_state": host_state, "hostname": hostname}
    if host_state.get("mode") == "held":
        return {"allowed": False, "reason": f"{host_state['reason']} on {hostname}",
                "machine_state": machine_state, "host_state": host_state, "hostname": hostname}
    return {"allowed": True, "reason": "", "machine_state": machine_state,
            "host_state": host_state, "hostname": hostname}


def claim_limits(ctx: Any, node: str) -> dict[str, Any]:
    row = ctx.db.one("SELECT slots_total FROM nodes WHERE name=%s", (node,)) or {}
    status = offer_status(ctx, node)
    active = int(ctx.db.one("SELECT count(*) AS n FROM runs WHERE node=%s AND status='running'", (node,)).get("n") or 0)
    slots = max(0, int(row.get("slots_total") or 0))
    machine_state, host_state = status["machine_state"], status["host_state"]
    machine_max = max(active, slots)
    if machine_state.get("mode") == "throttled":
        machine_max = active + max(0, (slots - active) // 2)
    host_max = host_state.get("max_runs") if status["hostname"] else None
    return {"allowed": status["allowed"] and active < machine_max and
            (host_max is None or active_host_runs(ctx, status["hostname"]) < int(host_max)),
            "reason": status["reason"] or ("reduced offers: half of free machine workers are in use"
                if machine_state.get("mode") == "throttled" and active >= machine_max else "host capacity is full"
                if host_max is not None and active_host_runs(ctx, status["hostname"]) >= int(host_max) else ""),
            "max_node_runs": machine_max, "max_host_runs": host_max, "hostname": status["hostname"],
            "machine_state": machine_state, "host_state": host_state}


def active_host_runs(ctx: Any, hostname: str) -> int:
    row = ctx.db.one("SELECT count(*) AS n FROM runs r JOIN nodes n ON n.name=r.node"
                     " WHERE r.status='running' AND lower(n.metrics->>'hostname')=%s", (hostname,)) or {}
    return int(row.get("n") or 0)

