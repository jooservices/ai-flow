"""Field providers for the node heartbeat payload."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
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
    order: int = 0
    prepare_order: int | None = None

    def validate(self, value: Any) -> Any:
        if (self.value_type is int and type(value) is not int) or not isinstance(value, self.value_type):
            raise TypeError(f"heartbeat field {self.name} must be {self.value_type.__name__}")
        return value

    def provide(self, context: HeartbeatContext) -> Any:
        return self.validate(self.collector(context))


_HEARTBEAT_PROVIDER_REGISTRY: dict[str, HeartbeatProvider] = {}
HEARTBEAT_PROVIDERS = MappingProxyType(_HEARTBEAT_PROVIDER_REGISTRY)


def register_heartbeat_provider(
    name: str,
    value_type: type,
    collector: Callable[[HeartbeatContext], Any],
    *,
    order: int,
    prepare: Callable[[HeartbeatContext], None] | None = None,
    prepare_order: int | None = None,
    registry: MutableMapping[str, HeartbeatProvider] | None = None,
) -> HeartbeatProvider:
    """Register a field with stable payload and optional preparation ordering."""
    target = _HEARTBEAT_PROVIDER_REGISTRY if registry is None else registry
    if name in target:
        raise ValueError(f"duplicate heartbeat provider name {name}")
    if any(provider.order == order for provider in target.values()):
        raise ValueError(f"duplicate heartbeat provider order {order}")
    if prepare is None and prepare_order is not None or prepare is not None and prepare_order is None:
        raise ValueError("heartbeat preparation and its order must be provided together")
    if prepare_order is not None and any(provider.prepare_order == prepare_order for provider in target.values()):
        raise ValueError(f"duplicate heartbeat preparation order {prepare_order}")
    provider = HeartbeatProvider(name, value_type, collector, prepare, order, prepare_order)
    target[name] = provider
    ordered = sorted(target.items(), key=lambda item: item[1].order)
    target.clear()
    target.update(ordered)
    return provider


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


register_heartbeat_provider("slots_total", int, _slots_total, order=0)


def _slots_used(context: HeartbeatContext) -> int:
    return sum(1 for worker in context.workers or [] if worker.get("state") == "working" and worker.get("alive"))


register_heartbeat_provider("slots_used", int, _slots_used, order=1)


def _release_version(context: HeartbeatContext) -> str:
    return context.sources.installed_release() or context.sources.version


register_heartbeat_provider("release_version", str, _release_version, order=2)


def _release_protocol(_context: HeartbeatContext) -> int:
    return 1


register_heartbeat_provider("release_protocol", int, _release_protocol, order=3)


def _tools(context: HeartbeatContext) -> dict:
    return context.state.tools


register_heartbeat_provider("tools", dict, _tools, order=4, prepare=_prepare_tools, prepare_order=1)


def _services(context: HeartbeatContext) -> dict:
    return context.sources.services_snapshot(context.state.service_state, context.state.service_lock)


register_heartbeat_provider("services", dict, _services, order=5)


def _metrics(context: HeartbeatContext) -> dict:
    relay = context.relay.health() if context.relay else {"enabled": False, "healthy": False}
    hostname = context.sources.hostname()
    return {**(context.node_metrics or {}), "hostname": hostname, "policy_relay": relay}


register_heartbeat_provider("metrics", dict, _metrics, order=6, prepare=_prepare_metrics, prepare_order=0)


def _workers(context: HeartbeatContext) -> list[dict]:
    return context.workers or []


register_heartbeat_provider("workers", list, _workers, order=7, prepare=_prepare_workers, prepare_order=2)


def _credential_expiries(context: HeartbeatContext) -> dict:
    return context.sources.credential_expiries(context.workers or [])


register_heartbeat_provider("credential_expiries", dict, _credential_expiries, order=8)


def _system_conditions(context: HeartbeatContext) -> list:
    return context.sources.system_conditions(context.node_metrics or {}, context.state.auto_fixed_conditions)


register_heartbeat_provider("system_conditions", list, _system_conditions, order=9)


def _credential_checks(context: HeartbeatContext) -> dict:
    return context.sources.credential_checks(context.workers or [])


register_heartbeat_provider("credential_checks", dict, _credential_checks, order=10)


def _auto_fixed_tools(context: HeartbeatContext) -> list[str]:
    return sorted(context.state.auto_fixed_tools)


register_heartbeat_provider("auto_fixed_tools", list, _auto_fixed_tools, order=11)


def _auto_fixed_conditions(context: HeartbeatContext) -> list[str]:
    return sorted(context.state.auto_fixed_conditions)


register_heartbeat_provider("auto_fixed_conditions", list, _auto_fixed_conditions, order=12)


def _runtime_settings(context: HeartbeatContext) -> dict:
    return context.sources.runtime_settings_inventory()


register_heartbeat_provider("runtime_settings", dict, _runtime_settings, order=13)


def _runtime_settings_public_key(context: HeartbeatContext) -> str:
    return context.runtime_settings_public_key


register_heartbeat_provider("runtime_settings_public_key", str, _runtime_settings_public_key, order=14)


def _environment_items(context: HeartbeatContext) -> dict[str, list[str]]:
    return context.environment_items or {}


register_heartbeat_provider(
    "environment_items", dict, _environment_items, order=15, prepare=_prepare_environment_items, prepare_order=3
)


def collect_heartbeat_fields(
    context: HeartbeatContext, providers: MutableMapping[str, HeartbeatProvider] | None = None
) -> dict[str, Any]:
    """Prepare shared node observations, then collect and validate every registered heartbeat field."""
    registered = HEARTBEAT_PROVIDERS if providers is None else providers
    preparations = sorted(
        (provider for provider in registered.values() if provider.prepare is not None),
        key=lambda provider: provider.prepare_order,
    )
    for provider in preparations:
        prepare = provider.prepare
        if prepare is not None:
            prepare(context)
    ordered = sorted(registered.values(), key=lambda provider: provider.order)
    return {provider.name: provider.provide(context) for provider in ordered}
