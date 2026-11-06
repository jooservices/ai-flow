"""Field providers for the node heartbeat payload."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True, slots=True)
class HeartbeatSources:
    """Node-agent operations injected into field providers for straightforward unit testing."""

    metrics: Callable[..., Any]
    tool_versions: Callable[..., Any]
    worker_states: Callable[..., Any]
    installed_release: Callable[..., Any]
    credential_expiries: Callable[..., Any]
    system_conditions: Callable[..., Any]
    credential_checks: Callable[..., Any]
    services_snapshot: Callable[..., Any]
    runtime_settings_inventory: Callable[..., Any]
    read_env_file: Callable[..., Any]
    hostname: Callable[..., Any]
    now: Callable[..., Any]
    version: str


@dataclass(slots=True)
class HeartbeatContext:
    """Inputs and per-heartbeat cached observations shared by the registered providers."""

    state: Any
    slots: int
    relay: Any
    runtime_settings_public_key: str
    sources: HeartbeatSources
    node_metrics: dict | None = None
    workers: list[dict] | None = None
    environment_items: dict[str, list[str]] | None = None


@dataclass(frozen=True, slots=True)
class HeartbeatProvider:
    """One stable heartbeat field, including its collector and top-level value contract."""

    name: str
    value_type: type
    collector: Callable[[HeartbeatContext], Any]
    prepare: Callable[[HeartbeatContext], None] | None = None

    def validate(self, value: Any) -> Any:
        if (self.value_type is int and type(value) is not int) or not isinstance(value, self.value_type):
            raise TypeError(f"heartbeat field {self.name} must be {self.value_type.__name__}")
        return value

    def provide(self, context: HeartbeatContext) -> Any:
        return self.validate(self.collector(context))


def _prepare_metrics(context: HeartbeatContext) -> None:
    context.node_metrics, context.state.previous_metrics = context.sources.metrics(context.state.previous_metrics)


def _prepare_tools(context: HeartbeatContext) -> None:
    if context.sources.now() - context.state.tools_at > 600:
        context.state.tools, context.state.tools_at = context.sources.tool_versions(), context.sources.now()


def _prepare_workers(context: HeartbeatContext) -> None:
    context.workers = context.sources.worker_states()


def _prepare_environment_items(context: HeartbeatContext) -> None:
    context.environment_items = {}
    for project, names in context.state.project_environment.items():
        local = context.sources.read_env_file(f"/etc/aiflow/projects/{project}.env")
        present = [name for name in names if local.get(name)]
        if present:
            context.environment_items[project] = present


def _slots_total(context: HeartbeatContext) -> int:
    return context.slots


def _slots_used(context: HeartbeatContext) -> int:
    return sum(1 for worker in context.workers or []
               if worker.get("state") == "working" and worker.get("alive"))


def _release_version(context: HeartbeatContext) -> str:
    return context.sources.installed_release() or context.sources.version


def _release_protocol(_context: HeartbeatContext) -> int:
    return 1


def _tools(context: HeartbeatContext) -> dict:
    return context.state.tools


def _services(context: HeartbeatContext) -> dict:
    return context.sources.services_snapshot(context.state.service_state, context.state.service_lock)


def _metrics(context: HeartbeatContext) -> dict:
    relay = context.relay.health() if context.relay else {"enabled": False, "healthy": False}
    return {**(context.node_metrics or {}), "hostname": context.sources.hostname(), "policy_relay": relay}


def _workers(context: HeartbeatContext) -> list[dict]:
    return context.workers or []


def _credential_expiries(context: HeartbeatContext) -> dict:
    return context.sources.credential_expiries(context.workers or [])


def _system_conditions(context: HeartbeatContext) -> list:
    return context.sources.system_conditions(context.node_metrics or {}, context.state.auto_fixed_conditions)


def _credential_checks(context: HeartbeatContext) -> dict:
    return context.sources.credential_checks(context.workers or [])


def _auto_fixed_tools(context: HeartbeatContext) -> list[str]:
    return sorted(context.state.auto_fixed_tools)


def _auto_fixed_conditions(context: HeartbeatContext) -> list[str]:
    return sorted(context.state.auto_fixed_conditions)


def _runtime_settings(context: HeartbeatContext) -> dict:
    return context.sources.runtime_settings_inventory()


def _runtime_settings_public_key(context: HeartbeatContext) -> str:
    return context.runtime_settings_public_key


def _environment_items(context: HeartbeatContext) -> dict[str, list[str]]:
    return context.environment_items or {}


HEARTBEAT_PROVIDERS = MappingProxyType({
    "slots_total": HeartbeatProvider("slots_total", int, _slots_total),
    "slots_used": HeartbeatProvider("slots_used", int, _slots_used),
    "release_version": HeartbeatProvider("release_version", str, _release_version),
    "release_protocol": HeartbeatProvider("release_protocol", int, _release_protocol),
    "tools": HeartbeatProvider("tools", dict, _tools, _prepare_tools),
    "services": HeartbeatProvider("services", dict, _services),
    "metrics": HeartbeatProvider("metrics", dict, _metrics, _prepare_metrics),
    "workers": HeartbeatProvider("workers", list, _workers, _prepare_workers),
    "credential_expiries": HeartbeatProvider("credential_expiries", dict, _credential_expiries),
    "system_conditions": HeartbeatProvider("system_conditions", list, _system_conditions),
    "credential_checks": HeartbeatProvider("credential_checks", dict, _credential_checks),
    "auto_fixed_tools": HeartbeatProvider("auto_fixed_tools", list, _auto_fixed_tools),
    "auto_fixed_conditions": HeartbeatProvider("auto_fixed_conditions", list, _auto_fixed_conditions),
    "runtime_settings": HeartbeatProvider("runtime_settings", dict, _runtime_settings),
    "runtime_settings_public_key": HeartbeatProvider("runtime_settings_public_key", str,
                                                       _runtime_settings_public_key),
    "environment_items": HeartbeatProvider("environment_items", dict, _environment_items,
                                             _prepare_environment_items),
})


def collect_heartbeat_fields(context: HeartbeatContext) -> dict[str, Any]:
    """Prepare shared node observations, then collect and validate every registered heartbeat field."""
    for name in ("metrics", "tools", "workers", "environment_items"):
        prepare = HEARTBEAT_PROVIDERS[name].prepare
        if prepare is not None:
            prepare(context)
    return {name: provider.provide(context) for name, provider in HEARTBEAT_PROVIDERS.items()}
