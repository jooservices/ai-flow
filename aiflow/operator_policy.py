"""Operator policy (JAIF-71): the thresholds and the escalation list, read from `config/operator.toml` (git-ignored, like aiflow.toml).

Without the file every value is the default below and the operator is OFF. Anything that is not understood stops the worker with a
clear message (`bin/aiflow-work check` shows it), so a typo never silently widens what the operator may do."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import ConfigError

ESCALATE_ALWAYS = (
    "spending money", "credentials, tokens or secrets", "deleting data, databases, repositories or branches", "force-push",
    "changing the product direction or the requirements", "a release", "merging by hand", "anything the policy does not cover",
)
NUMBERS = {"blocked_minutes": 30, "stuck_minutes": 120, "ci_wait_minutes": 120, "rework_limit": 3, "retries_per_ticket_per_day": 2,
           "max_runs_per_hour": 6, "max_runs_per_day": 30, "run_minutes": 20, "notify_interval_minutes": 60}


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    agent: str = "Claude"
    blocked_minutes: int = 30          # a Blocked ticket older than this is an incident (Quota and Offline return by themselves)
    stuck_minutes: int = 120           # a worked ticket with no lease and no change for this long is an incident
    ci_wait_minutes: int = 120         # a pull request in QA with no change for this long is an incident
    rework_limit: int = 3              # rework rounds on one ticket before an incident
    retries_per_ticket_per_day: int = 2  # operator runs on one ticket per day; above that the incident is escalated
    max_runs_per_hour: int = 6         # operator runs per hour in total
    max_runs_per_day: int = 30         # operator runs per day in total
    run_minutes: int = 20              # time limit of one operator run
    notify_interval_minutes: int = 60  # at most one grouped message to the owner per this many minutes
    escalate_always: tuple[str, ...] = ESCALATE_ALWAYS


def load(config_dir: Path | None) -> Policy:
    path = config_dir / "operator.toml" if config_dir else None
    if not path or not path.is_file():
        return Policy()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as error:
        raise ConfigError(f"{path}: {error}") from error
    for section in data:
        if section not in ("operator", "escalation"):
            raise ConfigError(f"{path}: unknown section `{section}` (known: operator, escalation).")
    table, values = dict(data.get("operator", {})), {}
    if "enabled" in table:
        if not isinstance(table.pop("enabled"), bool):
            raise ConfigError(f"{path} [operator]: `enabled` must be true or false.")
        values["enabled"] = data["operator"]["enabled"]
    if "agent" in table:
        agent = table.pop("agent")
        if agent not in ("Claude", "Codex", "Grok"):
            raise ConfigError(f"{path} [operator]: `agent` must be Claude, Codex or Grok (OpenCode cannot be restricted to read-only).")
        values["agent"] = agent
    for name, value in table.items():
        if name not in NUMBERS:
            raise ConfigError(f"{path} [operator]: unknown setting `{name}` (known: enabled, agent, {', '.join(NUMBERS)}).")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{path} [operator]: `{name}` must be a whole number of at least 1.")
        values[name] = value
    extra = data.get("escalation", {})
    if set(extra) - {"always"}:
        raise ConfigError(f"{path} [escalation]: only `always` is allowed.")
    if "always" in extra:
        always = extra["always"]
        if not isinstance(always, list) or not always or not all(isinstance(item, str) and item.strip() for item in always):
            raise ConfigError(f"{path} [escalation]: `always` must be a non-empty list of text values.")
        values["escalate_always"] = tuple(always)
    return Policy(**values)
