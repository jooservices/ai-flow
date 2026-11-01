"""Reactions of the fleet feature."""
from __future__ import annotations

from ...platform.events import IN_TRANSACTION, Registry
from .events import AiAccountSignInCompleted, NodeJoined


def register_listeners(ctx, registry: Registry) -> None:
    """Register the listeners that need the application context (they close over it)."""

    @registry.on(NodeJoined, delivery=IN_TRANSACTION, name="fleet.telemetry_node_joined")
    def telemetry_node_joined(envelope, item: NodeJoined) -> None:
        """Keep the `node.joined` telemetry row (the Command Center timeline) in the join's transaction."""
        ctx.events.emit("node.joined", node=item.node, reason=f"{item.role} · {item.project}")

    @registry.on(NodeJoined, name="fleet.schedule_join_readiness")
    def schedule_join_readiness(envelope, item: NodeJoined) -> None:
        """Start the machine's first readiness check once the join has committed (it was inline in the join route)."""
        from ... import system_check_api  # looked up when called, so a test may patch the function
        system_check_api.schedule_join_readiness(ctx, item.node)

    @registry.on(AiAccountSignInCompleted, delivery=IN_TRANSACTION, name="fleet.telemetry_account_sign_in_completed")
    def telemetry_account_sign_in_completed(envelope, item: AiAccountSignInCompleted) -> None:
        """Keep the `account.sign_in_completed` timeline row in the transaction that finished the sign-in."""
        ctx.events.emit("account.sign_in_completed", account=item.account, provider=item.provider, node=item.node,
                        outcome="ready", reason="owner confirmed the successful provider check")
