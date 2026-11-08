"""Named phases used by the fleet node heartbeat route."""
import math

from ...repositories import nodes as node_repository


def _resource_sample(reported_metrics):
    keys = {"cpu_pct", "load1", "cpus", "mem_used", "mem_total"}
    sample = {}
    for key, value in reported_metrics.items():
        if key not in keys or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if (isinstance(value, float) and not math.isfinite(value)) or value < 0 or (key == "cpu_pct" and value > 100):
            continue
        sample[key] = value
    return sample


def update_node_heartbeat_row(ctx, node, body, reported_tools, reported_release, reported_metrics,
                              reported_services, inventory, json_value):
    """Persist node state and an indexed, bounded resource sample in one transaction."""
    sample = _resource_sample(reported_metrics)
    with ctx.db.transaction():
        ctx.db.run("UPDATE nodes SET status=CASE WHEN accepting_work THEN 'online' ELSE 'draining' END,"
                   " slots_total=%s, slots_used=%s, release_version=%s, tools=%s,"
                   " metrics=%s, runtime_settings=%s, runtime_settings_public_key=%s,"
                   " release_protocol=%s, last_heartbeat=now(),"
                   " idle_since=CASE WHEN %s=0 THEN coalesce(idle_since,now()) ELSE NULL END WHERE name=%s",
                   (body.slots_total, body.slots_used, reported_release, json_value(reported_tools),
                    json_value({**reported_metrics, "services": reported_services}),
                    json_value(inventory), body.runtime_settings_public_key,
                    body.release_protocol, body.slots_used, node))
        node_repository.record_heartbeat_sample(ctx.db, node, json_value(sample))
        node_repository.prune_heartbeat_samples(ctx.db)


def persist_heartbeat_credentials(ctx, node, body, repo, json_value):
    """Update only credential expiries belonging to accounts that run on this node."""
    for account_id, expires_at in body.credential_expiries.items():
        # The account row keeps the earliest expiry of its machines; each machine's own date is kept beside it.
        reported = repo.account(account_id)
        if not reported or not reported.runs_on(node):
            continue
        ctx.db.run("UPDATE account_machines SET credential_expires_at=%s WHERE account_id=%s AND node=%s",
                   (expires_at, account_id, node))
        ctx.db.run("UPDATE accounts SET credential_expires_at=(SELECT min(m.v) FROM (SELECT %s::timestamptz AS v"
                   " UNION ALL SELECT credential_expires_at FROM account_machines WHERE account_id=%s"
                   " AND node<>%s AND credential_expires_at IS NOT NULL) m) WHERE id=%s",
                   (expires_at, account_id, node, account_id))


def merge_worker_reports(workers) -> dict[str, dict]:
    """Keep the highest-ranked worker state for each account and count its reported instances."""
    merged: dict[str, dict] = {}
    rank = {"working": 5, "probing": 4, "idle": 3, "unavailable": 2, "error": 1, "stopped": 0}
    for worker in workers:
        account = str(worker.get("account", ""))
        if not account:
            continue
        state = str(worker.get("state", "unknown")) if worker.get("alive", True) else "stopped"
        current = merged.get(account)
        if current is None or rank.get(state, 0) > rank.get(current["_state"], 0):
            merged[account] = {**worker, "_state": state, "instances": (current or {}).get("instances", 0) + 1}
        else:
            current["instances"] = current.get("instances", 1) + 1
    return merged


def persist_worker_reports(ctx, node, merged, repo, json_value):
    """Persist merged state for assigned accounts, preserving probe and cooldown status rules."""
    for account, worker in merged.items():
        member = repo.account(account)
        if not member or not member.runs_on(node):
            continue
        state = worker.pop("_state")
        # Unavailable (last probe failed) keeps the status the probe set: cooldown, signed_out or error.
        ctx.db.run("UPDATE accounts SET worker_state=%s, status=CASE WHEN status='signed_out' THEN status"
                   " WHEN cooldown_until > now() THEN 'cooldown' WHEN %s = 'unavailable' THEN status"
                   " WHEN status = 'working' AND %s = 'idle' THEN status ELSE %s END,"
                   " updated_at=now() WHERE id=%s", (json_value(worker), state, state, state, account))
        ctx.db.run("UPDATE account_machines SET worker_state=%s WHERE account_id=%s AND node=%s",
                   (json_value({**worker, "state": state}), account, node))
        ctx.db.run("UPDATE accounts SET idle_since=CASE WHEN status='idle' THEN coalesce(idle_since,now()) ELSE NULL END,"
                   " cooldown_since=CASE WHEN status='cooldown' THEN coalesce(cooldown_since,now()) ELSE NULL END"
                   " WHERE id=%s", (account,))


def desired_heartbeat_state(ctx, node, body, previous, reported_release, inventory, repo, tool_repairs,
                            tooling_module, system_check_module, runtime_settings_module, fleet_release):
    """Build the node's desired accounts, sign-ins, release, project, and probe state."""
    sign_in_requests = ctx.db.all(
        "SELECT id,provider,sign_in_request_id,sign_in_status FROM managed_ai_accounts"
        " WHERE node=%s AND removed_at IS NULL AND sign_in_status IN ('queued','starting','waiting')"
        " ORDER BY created_at,id", (node,))
    signing_in = {row["id"] for row in sign_in_requests}
    # A shared account awaiting sign-in here gets no worker: its auth probe would reset the active user flow.
    pending_shared = {row["account_id"] for row in ctx.db.all(
        "SELECT account_id FROM account_machines WHERE node=%s AND status='pending'", (node,))}
    assigned = [account for account in repo.accounts_on(node)
                if not account.paused and account.id not in signing_in and account.id not in pending_shared]
    instances = [account.id if index == 1 else f"{account.id}.{index}" for account in assigned
                 for index in range(1, account.sessions + 1)]
    shared_signins = []
    for row in ctx.db.all("SELECT account_id, signin_attempt FROM account_machines WHERE node=%s"
                          " AND status='pending' ORDER BY account_id", (node,)):
        account = repo.account(row["account_id"])
        if account:
            shared_signins.append({"account": account.id, "provider": account.provider,
                                   "request_id": f"shared:{node}:{account.id}:{row['signin_attempt']}",
                                   "shared_account": True})
    # Legacy agents stay off automatic release targets until a trusted verifier has been bootstrapped.
    release = fleet_release(node, body.release_version) if body.release_protocol >= 1 else ""
    joined_node = ctx.db.one("SELECT projects,dynamic FROM nodes WHERE name=%s", (node,)) or {}
    node_projects = repo.projects_for_node(node, joined_node.get("projects") or [], joined_node.get("dynamic", False))
    project_stacks = {key: (repo.projects[key].stacks if repo.projects[key].repositories
                            else repo.projects[key].stack)
                      for key in node_projects if key in repo.projects}
    system_check_module.refresh_heartbeat(ctx, node, body, previous, reported_release)
    runtime_bootstrap = runtime_settings_module.bootstrap_settings_for_node(
        ctx, node, node_projects, inventory, body.runtime_settings_public_key, body.workers)
    return {"desired": {"accounts": [account.id for account in assigned], "workers": instances,
                         "sign_ins": [{"account": row["id"], "provider": row["provider"],
                                       "request_id": row["sign_in_request_id"], "status": row["sign_in_status"]}
                                      for row in sign_in_requests], "signin": shared_signins,
                         "release": release, "projects": node_projects, "project_stacks": project_stacks,
                         "project_environment": {key: [item.name for item in repo.projects[key].environment_items
                                                        if item.secret]
                                                 for key in node_projects if key in repo.projects},
                         "project_services": {key: repo.projects[key].required_services for key in node_projects
                                              if key in repo.projects and repo.projects[key].required_services},
                         "runtime_settings_bootstrap": runtime_bootstrap, "tool_repairs": tool_repairs,
                         "check_probes": tooling_module.pending_probes(ctx, node, node_projects),
                         "tools": repo.tools_for_node(node, node_projects), "versions": repo.versions}}
