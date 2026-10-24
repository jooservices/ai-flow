"""Use cases for browser-managed first-run setup."""
from __future__ import annotations

from .repositories import setup_wizard


def set_step_skipped(ctx, step_id: str, skipped: bool, actor: str) -> None:
    """Record a user's skip or resume choice through the setup repository."""
    setup_wizard.set_step_skipped(ctx.db, step_id, skipped, actor)
