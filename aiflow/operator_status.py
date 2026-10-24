"""Formatting for the operator and pause state shown by ``aiflow-work check``."""
from __future__ import annotations

from typing import Any, Callable


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
            f"{policy.max_runs_per_day}/day, {policy.retries_per_ticket_per_day}/ticket/day; use `aiflow-work operator on|off`")
