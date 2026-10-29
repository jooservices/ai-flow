"""Formatting for the operator and pause state shown by ``aiflow-work check``."""
from __future__ import annotations

from typing import Any, Callable

from . import events
from .master_rescue import check_line as master_check_line
from .master_rescue import WORK_STATUSES


def check_line(config: Any, redis_factory: Callable[..., Any], redis_error: type[Exception]) -> str:
    """Return the check output for policy, runtime switch, flow pause and limits."""
    policy = config.operator_policy
    runtime_switch, flow_pause = "unknown (Redis unavailable)", "unknown (Redis unavailable)"
    runtime_redis = redis_factory(config.redis_host, config.redis_port, config.redis_db, config.redis_prefix,
                                  config.redis_password, config.redis_user)
    try:
        runtime_switch = "OFF" if runtime_redis.get("operator:off") is not None else "ON"
        flow_pause = "paused" if runtime_redis.get("paused") is not None else "running"
    except redis_error:
        pass
    finally:
        runtime_redis.close()
    return (f"  operator: policy {'ON' if policy.enabled else 'OFF (config/operator.toml)'}, runtime switch {runtime_switch}, "
            f"flow {flow_pause}, agent {policy.agent}, limits {policy.max_runs_per_hour}/hour "
            f"{policy.max_runs_per_day}/day, {policy.retries_per_ticket_per_day}/ticket/day; use `aiflow-work operator on|off`\n"
            f"{master_check_line(config.registry.master)}")


def record_status_change(redis: Any, key: str, to: str, previous: str, source: str, since: Any, now: float) -> None:
    """Emit a status event and persist completed work time before the next status snapshot."""
    events.emit("ticket.status", ticket=key, to_status=to, from_status=previous, source=source,
                minutes_in_from=round((now - float(since)) / 60, 2) if since else None)
    if previous not in WORK_STATUSES or not since:
        return
    try:
        worked = float(redis.get(f"worked:{key}") or 0) + max(0.0, now - float(since)) / 60
    except (TypeError, ValueError):
        return
    redis.set_persistent(f"worked:{key}", str(round(worked, 2)))
