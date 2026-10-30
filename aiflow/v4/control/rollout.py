"""Fleet upgrades: rolling node releases, the control/node version skew policy and the pre-upgrade check.

docs/v4/fleet-upgrade.md describes the operator view. In short:

rollout     When `CURRENT` names a new release, one `fleet_rollouts` row tracks the move from the previous fleet
            release. The heartbeat gives the target only to the rollout's members (`fleet_rollout_nodes`): first
            one canary (wave 0), then waves of `rollout_batch_size` nodes. Every other node keeps the previous
            release. `tick` (control loops) runs the health check: a member passes when it reports the target and
            keeps heartbeating on it for `rollout_soak_s`; it fails when it misses the install deadline, loses its
            heartbeat, switches back or rejects the release. The next wave starts only when the whole wave passed.
halt        One failure halts the rollout: no node receives the target any more, every member is given its
            previous release again (the node reinstalls it through the same checksum and cosign verified path)
            and the `fleet_rollout` incident names the release, the failed node and, once known, whether the
            rollback succeeded (`fleet_rollback`). Runs cut off by a node restart lose their lease and the
            watchdog's lease-lost requeue moves their tickets back to To Do with their branch.
skew        `skew` compares a node release with the control plane's release by X.Y: a node may run at most
            `version_max_minor_lag` minors behind, never ahead and never on another major. An incompatible node
            gets no new work and has a `version_skew` incident; it still receives releases, so it can catch up.
check       `check_upgrade` runs before the control plane changes release (aiflow4-deploy): migrations may only
            be added, never edited or deleted once recorded in `schema_migrations`, and no online node may end
            up outside the skew policy. A blocked check keeps the running release and records why.
Migrations only go forward; a rollback never runs one backwards (db.DB.migrate).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import J
from .incidents import close_incident, open_incident
from .state import NODE_OFFLINE_S

log = logging.getLogger("aiflow4.rollout")

RELEASE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_LOOSE = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
MIGRATION_NAME = re.compile(r"\d{3}_[A-Za-z0-9_]+\.sql")
ACTIVE = ("canary", "rolling")
_LOCK = 424205


# --- version skew policy ---------------------------------------------------------------------------------------
def parse_version(value: str | None) -> tuple[int, int, int] | None:
    m = _LOOSE.fullmatch((value or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def skew(control: str, node: str, max_minor_lag: int = 1) -> dict[str, Any]:
    """Whether a node release may work with the control plane's release (compared by X.Y)."""
    c, n = parse_version(control), parse_version(node)
    if c is None:
        return {"compatible": True, "state": "off", "reason": f"control version {control or 'unknown'} is not vX.Y.Z"}
    if n is None:
        return {"compatible": True, "state": "unknown", "reason": f"node version {node or 'unknown'} is not vX.Y.Z"}
    if n[0] != c[0]:
        return {"compatible": False, "state": "major",
                "reason": f"node runs {node}, another major than the control plane's {control}"}
    if n[1] > c[1]:
        return {"compatible": False, "state": "ahead", "reason": f"node runs {node}, newer than the control plane's {control}"}
    lag = c[1] - n[1]
    if lag > max_minor_lag:
        return {"compatible": False, "state": "behind",
                "reason": f"node runs {node}, {lag} minors behind the control plane's {control}"
                          f" (at most {max_minor_lag} allowed)"}
    return {"compatible": True, "state": "ok", "reason": ""}


def canary_override(releases: Path) -> dict:
    """The auto-deployer's pre-promotion canary (CANARY.json): one node on a candidate before the control plane."""
    try:
        data = json.loads((releases / "CANARY.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_current(releases: Path) -> str:
    try:
        return (releases / "CURRENT").read_text().strip()
    except OSError:
        return ""


def node_policy(ctx, node: str, release: str, releases: Path | None = None) -> dict[str, Any]:
    """The skew verdict for one node; the auto-deploy canary node may run its candidate ahead of the control."""
    verdict = skew(ctx.settings.control_version, release, ctx.settings.version_max_minor_lag)
    if not verdict["compatible"] and releases is not None:
        canary = canary_override(releases)
        if canary.get("node") == node and canary.get("target") and canary.get("target") == release:
            return {"compatible": True, "state": "canary", "reason": f"auto-deploy canary for {release}"}
    return verdict


def node_compatible(ctx, node: str, releases: Path | None = None) -> dict[str, Any]:
    row = ctx.db.one("SELECT release_version FROM nodes WHERE name=%s", (node,))
    return node_policy(ctx, node, (row or {}).get("release_version") or "", releases)


def watch_skew(ctx, releases: Path | None = None) -> None:
    """Watchdog: a `version_skew` incident per online node outside the policy, closed when it is back inside."""
    bad: set[str] = set()
    for n in ctx.db.all("SELECT name, status, release_version FROM nodes"):
        if n["status"] != "online":
            continue
        verdict = node_policy(ctx, n["name"], n["release_version"], releases)
        if verdict["compatible"]:
            continue
        bad.add(n["name"])
        open_incident(ctx, "version_skew", n["name"], "warning",
                      f"node {n['name']} is incompatible with the control plane",
                      f"{verdict['reason']}. The node gets no new work until it runs a compatible release; the"
                      " fleet rollout gives it the current release.")
    for i in ctx.db.all("SELECT subject FROM incidents WHERE kind='version_skew' AND closed_at IS NULL"):
        if i["subject"] not in bad:
            close_incident(ctx, "version_skew", i["subject"], "the node runs a compatible release")


# --- rollout state ---------------------------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def latest(db) -> dict | None:
    return db.one("SELECT * FROM fleet_rollouts ORDER BY id DESC LIMIT 1")


def members(db, rollout_id: int) -> list[dict]:
    return db.all("SELECT * FROM fleet_rollout_nodes WHERE rollout_id=%s ORDER BY wave, node", (rollout_id,))


def _member(db, rollout_id: int, node: str) -> dict | None:
    return db.one("SELECT * FROM fleet_rollout_nodes WHERE rollout_id=%s AND node=%s", (rollout_id, node))


def rollback_version(rollout: dict, member: dict) -> str:
    return member["from_version"] if RELEASE.fullmatch(member["from_version"] or "") else rollout["previous"]


def ensure(ctx, target: str) -> dict | None:
    """The rollout of `target` (CURRENT), created when CURRENT changed. None when CURRENT is not vX.Y.Z."""
    if not RELEASE.fullmatch(target or ""):
        return None
    r = latest(ctx.db)
    if r and r["target"] == target:
        return r
    with ctx.db.pool.connection() as c:
        with c.transaction():
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
            r = c.execute("SELECT * FROM fleet_rollouts ORDER BY id DESC LIMIT 1").fetchone()
            if r and r["target"] == target:
                return r
            if r:
                previous = r["target"] if r["status"] == "completed" else r["previous"]
            else:  # the first rollout: the release most nodes run now
                seen = Counter(n["release_version"] for n in c.execute(
                    "SELECT release_version FROM nodes WHERE token_hash <> ''").fetchall()
                               if RELEASE.fullmatch(n["release_version"] or "") and n["release_version"] != target)
                previous = seen.most_common(1)[0][0] if seen else ""
            if r and r["status"] in ACTIVE:
                c.execute("UPDATE fleet_rollouts SET status='superseded', reason=%s, updated_at=now(),"
                          " finished_at=now() WHERE id=%s", (f"superseded by {target}", r["id"]))
            status = "completed" if not previous or previous == target else "canary"
            created = c.execute(
                "INSERT INTO fleet_rollouts (target, previous, status, batch_size, finished_at)"
                " VALUES (%s,%s,%s,%s, CASE WHEN %s='completed' THEN now() END) RETURNING *",
                (target, previous, status, max(1, int(ctx.settings.rollout_batch_size)), status)).fetchone()
    ctx.events.emit("fleet.rollout.started", outcome=status, reason=f"{previous or 'nothing'} -> {target}",
                    target=target, previous=previous)
    return created


def desired_release(ctx, node: str, reported: str, current: str) -> str:
    """The release one node's heartbeat names: the target for rollout members, the previous one for the rest."""
    r = ensure(ctx, current)
    if r is None:
        return current
    if r["status"] == "completed":
        return r["target"]
    m = _member(ctx.db, r["id"], node)
    if r["status"] == "halted":
        return rollback_version(r, m) if m else r["previous"]
    if m is None and reported == r["target"]:
        # already there (the control host's own agent, the auto-deploy canary): it joins the current wave and is
        # health checked with it rather than being sent back
        ctx.db.run("INSERT INTO fleet_rollout_nodes (rollout_id, node, wave, from_version, detail)"
                   " VALUES (%s,%s,%s,%s,'already on the target') ON CONFLICT DO NOTHING",
                   (r["id"], node, r["wave"], r["previous"]))
        m = _member(ctx.db, r["id"], node)
    return r["target"] if m and m["status"] in ("assigned", "healthy") else r["previous"]


def _nodes(db) -> dict[str, dict]:
    return {n["name"]: n for n in db.all(
        "SELECT name, status, release_version, release_protocol, last_heartbeat, token_hash FROM nodes")}


def _fresh(n: dict | None, now: datetime) -> bool:
    return bool(n and n["status"] == "online" and n["last_heartbeat"]
                and (now - n["last_heartbeat"]).total_seconds() <= NODE_OFFLINE_S)


def _eligible(nodes: dict[str, dict], taken: set[str], now: datetime) -> list[str]:
    return sorted(name for name, n in nodes.items()
                  if name not in taken and _fresh(n, now) and (n["release_protocol"] or 0) >= 1 and n["token_hash"])


def _assign(ctx, r: dict, wave: int, names: list[str], nodes: dict[str, dict]) -> None:
    for name in names:
        ctx.db.run("INSERT INTO fleet_rollout_nodes (rollout_id, node, wave, from_version) VALUES (%s,%s,%s,%s)"
                   " ON CONFLICT DO NOTHING", (r["id"], name, wave, nodes[name]["release_version"] or r["previous"]))
        ctx.events.emit("fleet.rollout.assigned", node=name, outcome="canary" if wave == 0 else f"wave {wave}",
                        reason=f"{nodes[name]['release_version'] or '?'} -> {r['target']}", target=r["target"])


def tick(ctx, current: str | None = None) -> dict | None:
    """One pass of the rollout: health checks, the next wave, a halt and the rollback watch."""
    r = ensure(ctx, current) if current else latest(ctx.db)
    if r is None:
        return None
    if r["status"] in ACTIVE:
        _advance(ctx, r)
    elif r["status"] == "halted" and r["rollback"] in ("pending", "failed"):
        _watch_rollback(ctx, r)
    return latest(ctx.db)


def _member_problem(n: dict | None, m: dict, r: dict, now, s, on_target: bool, reported: str) -> str:
    """Why an assigned node stops the rollout (lost heartbeat, switched back, install timeout), or an empty string."""
    if not _fresh(n, now):
        age = int((now - n["last_heartbeat"]).total_seconds()) if n and n["last_heartbeat"] else None
        return f"lost its heartbeat ({'never seen' if age is None else f'{age} s ago'})"
    if m["upgraded_at"] and not on_target:
        return f"switched back to {reported or 'unknown'} after installing {r['target']}"
    if not on_target and (now - m["assigned_at"]).total_seconds() > s.rollout_install_timeout_s:
        return (f"did not report {r['target']} within {s.rollout_install_timeout_s} s"
                f" (reports {reported or 'unknown'})")
    return ""


def _advance(ctx, r: dict) -> None:
    now, s = _now(), ctx.settings
    nodes = _nodes(ctx.db)
    rows = members(ctx.db, r["id"])
    for m in rows:
        if m["status"] != "assigned":
            continue
        n = nodes.get(m["node"])
        reported = (n or {}).get("release_version") or ""
        on_target = reported == r["target"]
        if on_target and m["upgraded_at"] is None:
            m["upgraded_at"] = now
            ctx.db.run("UPDATE fleet_rollout_nodes SET upgraded_at=%s WHERE rollout_id=%s AND node=%s",
                       (now, r["id"], m["node"]))
        why = _member_problem(n, m, r, now, s, on_target, reported)
        if why:
            halt(ctx, r, m["node"], why)
            return
        if on_target and (now - m["upgraded_at"]).total_seconds() >= s.rollout_soak_s:
            m["status"] = "healthy"
            ctx.db.run("UPDATE fleet_rollout_nodes SET status='healthy', healthy_at=now() WHERE rollout_id=%s AND node=%s",
                       (r["id"], m["node"]))
            ctx.events.emit("fleet.rollout.healthy", node=m["node"], outcome="healthy", target=r["target"],
                            reason=f"{r['target']} for {s.rollout_soak_s} s")
    wave = [m for m in rows if m["wave"] == r["wave"]]
    if any(m["status"] != "healthy" for m in wave):
        return
    taken = {m["node"] for m in rows}
    if r["status"] == "canary" and not wave:
        free = _eligible(nodes, taken, now)
        if not free:
            return
        on_target = [x for x in free if nodes[x]["release_version"] == r["target"]]
        pick = s.rollout_canary_node if s.rollout_canary_node in free else (on_target or free)[0]
        _assign(ctx, r, 0, [pick], nodes)
        return
    # the canary or the whole wave passed: the next wave, or done when every reachable node is a member
    nxt = r["wave"] + 1
    free = _eligible(nodes, taken, now)
    if not free:
        ctx.db.run("UPDATE fleet_rollouts SET status='completed', updated_at=now(), finished_at=now() WHERE id=%s",
                   (r["id"],))
        ctx.events.emit("fleet.rollout.completed", outcome="completed", target=r["target"],
                        reason=f"{len(rows)} nodes on {r['target']}")
        return
    ctx.db.run("UPDATE fleet_rollouts SET status='rolling', wave=%s, updated_at=now() WHERE id=%s", (nxt, r["id"]))
    _assign(ctx, r, nxt, free[:max(1, int(r["batch_size"]))], nodes)


def halt(ctx, r: dict, node: str, why: str) -> None:
    """Stop the rollout after a failed health check and send every member back to its previous release."""
    updated = ctx.db.one("UPDATE fleet_rollouts SET status='halted', failed_node=%s, reason=%s, rollback='pending',"
                         " updated_at=now(), finished_at=now() WHERE id=%s AND status IN ('canary','rolling')"
                         " RETURNING *", (node, why[:1000], r["id"]))
    if not updated:
        return
    ctx.db.run("UPDATE fleet_rollout_nodes SET status='rolling_back', rollback_at=now(),"
               " detail=CASE WHEN node=%s THEN %s ELSE detail END WHERE rollout_id=%s", (node, why[:1000], r["id"]))
    rows = members(ctx.db, r["id"])
    back = ", ".join(f"{m['node']} -> {rollback_version(updated, m)}" for m in rows) or "none"
    stage = "canary" if updated["wave"] == 0 else f"wave {updated['wave']}"
    ctx.events.emit("fleet.rollout.halted", node=node, outcome="halted", reason=why[:300], target=r["target"],
                    previous=r["previous"])
    open_incident(ctx, "fleet_rollout", r["target"], "critical",
                  f"rollout of {r['target']} halted: node {node} failed its health check",
                  f"Release {r['target']} (previous {r['previous']}), {stage}. Failed node: {node}: {why}.\n"
                  f"No node receives {r['target']} any more. Rollback: {back}. Rollback result: pending.\n"
                  "Runs on restarted nodes lose their lease; their tickets go back to To Do and keep their branch.")
    _watch_rollback(ctx, updated)


def _watch_rollback(ctx, r: dict) -> None:
    now, nodes = _now(), _nodes(ctx.db)
    for m in members(ctx.db, r["id"]):
        if m["status"] not in ("rolling_back", "rollback_failed"):
            continue
        want = rollback_version(r, m)
        reported = (nodes.get(m["node"]) or {}).get("release_version") or ""
        if reported == want:
            ctx.db.run("UPDATE fleet_rollout_nodes SET status='rolled_back' WHERE rollout_id=%s AND node=%s",
                       (r["id"], m["node"]))
            ctx.events.emit("fleet.rollback.node", node=m["node"], outcome="rolled_back", target=r["target"],
                            reason=f"back on {want}")
        elif m["status"] == "rolling_back" and (now - m["rollback_at"]).total_seconds() > ctx.settings.rollout_install_timeout_s:
            ctx.db.run("UPDATE fleet_rollout_nodes SET status='rollback_failed', detail=%s WHERE rollout_id=%s AND node=%s",
                       (f"still reports {reported or 'unknown'} after {ctx.settings.rollout_install_timeout_s} s"
                        + (f"; {m['detail']}" if m["detail"] else ""), r["id"], m["node"]))
    rows = members(ctx.db, r["id"])
    if any(m["status"] == "rolling_back" for m in rows):
        return
    ok = all(m["status"] == "rolled_back" for m in rows)
    result = "succeeded" if ok else "failed"
    if result == r["rollback"]:
        return
    ctx.db.run("UPDATE fleet_rollouts SET rollback=%s, updated_at=now() WHERE id=%s", (result, r["id"]))
    lines = "\n".join(f"- {m['node']}: {m['status']} (to {rollback_version(r, m)})"
                      + (f": {m['detail']}" if m["detail"] else "") for m in rows) or "- no node had been upgraded"
    detail = (f"Release {r['target']}, previous {r['previous']}. Failed node: {r['failed_node']}: {r['reason']}.\n"
              f"Rollback result: {result}.\n{lines}")
    ctx.db.run("UPDATE incidents SET detail=%s WHERE kind='fleet_rollout' AND subject=%s AND closed_at IS NULL",
               (detail, r["target"]))
    ctx.events.emit("fleet.rollback.finished", node=r["failed_node"], outcome=result, target=r["target"],
                    reason=r["reason"][:300])
    if ok and r["rollback"] == "failed":  # a late node came back on its previous release
        close_incident(ctx, "fleet_rollback", r["target"], "every node runs its previous release again")
        return
    open_incident(ctx, "fleet_rollback", r["target"], "warning" if ok else "critical",
                  f"rollback of {r['target']} {'succeeded' if ok else 'FAILED'}", detail)


def release_rejected(ctx, node: str, version: str, detail: str) -> None:
    """A node refused a release (checksum, signature, installer): the rollout's health check fails."""
    r = latest(ctx.db)
    if not r:
        return
    m = _member(ctx.db, r["id"], node)
    if r["status"] in ACTIVE and version == r["target"]:
        halt(ctx, r, node, f"rejected the release: {detail}"[:1000])
    elif r["status"] == "halted" and m and m["status"] == "rolling_back" and version == rollback_version(r, m):
        ctx.db.run("UPDATE fleet_rollout_nodes SET status='rollback_failed', detail=%s WHERE rollout_id=%s AND node=%s",
                   (f"rejected its previous release {version}: {detail}"[:1000], r["id"], node))
        _watch_rollback(ctx, r)


def summary(db) -> dict | None:
    """The latest rollout and its members, for /v1/state and Command Center."""
    r = latest(db)
    if not r:
        return None
    return {"id": r["id"], "target": r["target"], "previous": r["previous"], "status": r["status"], "wave": r["wave"],
            "batch_size": r["batch_size"], "failed_node": r["failed_node"], "reason": r["reason"],
            "rollback": r["rollback"], "started_at": r["started_at"].isoformat(),
            "nodes": [{"node": m["node"], "wave": m["wave"], "status": m["status"], "from": m["from_version"],
                       "detail": m["detail"]} for m in members(db, r["id"])]}


# --- pre-upgrade check of the control plane --------------------------------------------------------------------
def check_upgrade(db, target: str, migrations: Path, baseline: Path | None = None, current: str = "",
                  max_minor_lag: int = 1) -> dict[str, Any]:
    """Whether the control plane may move to `target`; never changes the schema."""
    reasons: list[str] = []
    if not RELEASE.fullmatch(target or ""):
        reasons.append(f"target {target or '(empty)'} is not a vX.Y.Z release")
    with db.pool.connection() as c:
        has_table = c.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()["t"]
        has_sum = has_table and c.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='schema_migrations' AND column_name='checksum'"
        ).fetchone()
        applied = {r["version"]: (r.get("checksum") or "") for r in c.execute(
            "SELECT version" + (", checksum" if has_sum else "") + " FROM schema_migrations").fetchall()} if has_table else {}
    files = {f.stem: f for f in sorted(migrations.glob("*.sql"))}
    for version, recorded in sorted(applied.items()):
        f = files.get(version)
        if f is None:
            reasons.append(f"migration {version} is applied but {target} does not ship it (applied migrations are"
                           " never deleted)")
            continue
        content = f.read_bytes()
        old = baseline / f"{version}.sql" if baseline else None
        if recorded and hashlib.sha256(content).hexdigest() != recorded:
            reasons.append(f"migration {version} was changed after it was applied (add a new NNN_*.sql instead)")
        elif not recorded and old is not None and old.is_file() and old.read_bytes() != content:
            reasons.append(f"migration {version} differs from the running release (add a new NNN_*.sql instead)")
    new = [name for name in files if name not in applied]
    for name in new:
        if not MIGRATION_NAME.fullmatch(f"{name}.sql"):
            reasons.append(f"new migration {name}.sql is not named NNN_*.sql")
    nodes = []
    for n in db.all("SELECT name, release_version FROM nodes WHERE status IN ('online','draining') ORDER BY name"):
        verdict = skew(target, n["release_version"], max_minor_lag)
        nodes.append({"node": n["name"], "release": n["release_version"], **verdict})
        if not verdict["compatible"]:
            reasons.append(f"node {n['name']} would be outside the version policy on {target}: {verdict['reason']}")
    return {"ok": not reasons, "target": target, "current": current, "reasons": reasons,
            "new_migrations": new, "nodes": nodes}


def record_check(ctx, result: dict[str, Any]) -> None:
    """Keep the verdict in write_audit; a blocked upgrade is a critical incident naming the reasons."""
    target, current = result["target"], result.get("current") or "the running release"
    verdict = "allowed" if result["ok"] else "blocked: " + "; ".join(result["reasons"])
    ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
               " VALUES ('control','upgrade_check','','control',%s,%s,%s)",
               (result["ok"], verdict[:2000], J(result)))
    ctx.events.emit("control.upgrade_check", outcome="allowed" if result["ok"] else "blocked",
                    reason=verdict[:300], target=target)
    if result["ok"]:
        close_incident(ctx, "upgrade_blocked", target, "the pre-upgrade check passed")
    else:
        open_incident(ctx, "upgrade_blocked", target, "critical", f"control upgrade to {target} blocked",
                      "\n".join(f"- {x}" for x in result["reasons"]) + f"\nThe control plane keeps running {current}.")
