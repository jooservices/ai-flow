"""SQL for canary settings, pending merges, merge-to-live times, and node work/health."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def fetch_settings(db) -> dict[str, Any] | None:
    """Load the single canary_settings row, if any."""
    return db.one(
        "SELECT minimum_age_s, minimum_runs, maximum_age_s, updated_by, updated_at FROM canary_settings")


def upsert_settings(db, minimum_age_s: int, minimum_runs: int, maximum_age_s: int, by: str) -> None:
    """Insert or update the singleton canary age and run thresholds."""
    db.run(
        "INSERT INTO canary_settings (id, minimum_age_s, minimum_runs, maximum_age_s, updated_by) VALUES"
        " (true,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET minimum_age_s=EXCLUDED.minimum_age_s,"
        " minimum_runs=EXCLUDED.minimum_runs, maximum_age_s=EXCLUDED.maximum_age_s,"
        " updated_by=EXCLUDED.updated_by, updated_at=now()",
        (minimum_age_s, minimum_runs, maximum_age_s, by),
    )


def upsert_pending_build(db, build: str, merged_at: datetime) -> None:
    """Record a merge that arrived while another canary is still under test."""
    db.run(
        "INSERT INTO canary_pending (build, merged_at) VALUES (%s,%s)"
        " ON CONFLICT (build) DO UPDATE SET merged_at=LEAST(canary_pending.merged_at, EXCLUDED.merged_at)",
        (build, merged_at),
    )


def pending_build(db, build: str) -> dict[str, Any] | None:
    """Return one pending merge row by build id."""
    return db.one("SELECT build, merged_at FROM canary_pending WHERE build=%s", (build,))


def record_merge_live(db, build: str, merged_at: datetime, live_at: datetime, seconds: int) -> None:
    """Persist how long a build took from merge to becoming live."""
    db.run(
        "INSERT INTO canary_merge_live (build, merged_at, live_at, seconds) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (build) DO UPDATE SET live_at=EXCLUDED.live_at, seconds=EXCLUDED.seconds",
        (build, merged_at, live_at, seconds),
    )


def delete_pending_build(db, build: str) -> None:
    """Drop a pending merge after it goes live or is otherwise cleared."""
    db.run("DELETE FROM canary_pending WHERE build=%s", (build,))


def count_node_work_since(db, node: str, started_at: datetime) -> int:
    """Count running or started runs on a node since the canary observation clock began."""
    row = db.one(
        "SELECT count(*) AS n FROM runs WHERE node=%s AND (status='running' OR started_at >= %s)",
        (node, started_at),
    )
    return int((row or {}).get("n") or 0)


def active_node_health(db, node: str) -> dict[str, Any] | None:
    """Return status and accepting_work for a non-removed node."""
    return db.one(
        "SELECT status, accepting_work FROM nodes WHERE name=%s AND removed_at IS NULL", (node,))


def list_pending_builds(db) -> list[dict[str, Any]]:
    """List pending merges oldest-first for the control canary dashboard."""
    return db.all("SELECT build, merged_at FROM canary_pending ORDER BY merged_at")


def recent_merge_live(db) -> list[dict[str, Any]]:
    """List the twenty most recent merge-to-live measurements."""
    return db.all(
        "SELECT build, merged_at, live_at, seconds FROM canary_merge_live"
        " ORDER BY live_at DESC LIMIT 20")
