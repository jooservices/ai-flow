"""One-time import of machines and AI accounts from the control repo into the control database.

Machines, AI accounts, their roles and worker counts are runtime facts (docs/v4/inventory-in-database.md). Older
installations kept them in `accounts.yaml` and `nodes/*.yaml`. `import_control_repo` copies them into
`managed_ai_accounts`, `nodes`, `shared_accounts` and `account_machines` exactly once, recorded in
`control_imports`. Afterwards the control repo files are no longer read for these facts and the Machines and AI
screen is the only place that changes them; a repeat call changes nothing.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import yaml

from ..core.controlrepo import Account, ControlRepo, Node
from .db import J

IMPORT_NAME = "inventory"


def is_imported(db) -> bool:
    return bool(db.one("SELECT 1 AS done FROM control_imports WHERE name=%s", (IMPORT_NAME,)))


def apply(base: ControlRepo, db) -> ControlRepo:
    """The control repo snapshot with the database's accounts: after the import the database is the only source.

    Before the import, console-managed accounts are merged over the repo's. Removed accounts and accounts on
    removed machines never stay schedulable."""
    removed_nodes = {row["name"] for row in db.all("SELECT name FROM nodes WHERE removed_at IS NOT NULL")}
    removed_ids = {row["id"] for row in db.all("SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}
    imported = is_imported(db)
    project_assignments = {row["name"]: list(row["managed_projects"] or []) for row in db.all(
        "SELECT name,managed_projects FROM nodes WHERE removed_at IS NULL AND managed_projects<>'{}'")}
    managed = [row for row in db.all("SELECT * FROM managed_ai_accounts WHERE removed_at IS NULL ORDER BY created_at,id")
               if row["node"] not in removed_nodes]
    if not managed and not removed_nodes and not removed_ids and not imported and not project_assignments:
        return base
    managed_ids = {row["id"] for row in managed}
    accounts = {account.id: account for account in ([] if imported else base.accounts)
                if account.id not in managed_ids and account.id not in removed_ids
                and account.node not in removed_nodes}
    for row in managed:
        accounts[row["id"]] = Account(
            id=row["id"], provider=row["provider"], node=row["node"], tier=row["tier"],
            roles=list(row["roles"] or []), parallel=int(row["parallel"]),
            explicit_only=bool(row["explicit_only"]), paused=bool(row["paused"]))
    if imported:
        # Once imported, the checkout is policy only. Rebuild machine profiles and project assignments from the
        # registry so a stale file brought back by a repo refresh cannot become a second source of truth.
        nodes: dict[str, Node] = {}
        for row in db.all("SELECT name,ip,role,slots_total,projects,profile FROM nodes WHERE removed_at IS NULL"):
            profile = row.get("profile") or {}
            profile = profile if isinstance(profile, dict) else {}
            profile_fields = {key: profile[key] for key in (
                "capabilities", "purpose", "notes", "location", "tools", "packages", "services"
            ) if key in profile}
            nodes[row["name"]] = Node(
                name=row["name"], ip=row["ip"], role=row["role"], slots=int(row["slots_total"]),
                projects=list(row["projects"] or []), **profile_fields)
        projects = {key: replace(project, nodes=[]) for key, project in base.projects.items()}
        return replace(base, accounts=list(accounts.values()), nodes=nodes, projects=projects,
                       inventory_in_db=True, managed_project_assignments=project_assignments)
    return replace(base, accounts=list(accounts.values()), inventory_in_db=False,
                   managed_project_assignments=project_assignments)


def remove_legacy_files(root: str | Path) -> None:
    """Remove imported inventory from the active checkout, preserving project policy and other settings."""
    checkout = Path(root)
    accounts = checkout / "accounts.yaml"
    if accounts.is_file() and not accounts.is_symlink():
        accounts.unlink()
    nodes = checkout / "nodes"
    if nodes.is_dir() and not nodes.is_symlink():
        shutil.rmtree(nodes)
    project_dir = checkout / "projects"
    if not project_dir.is_dir() or project_dir.is_symlink():
        return
    for path in sorted(project_dir.glob("*.yaml")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(value, dict) or "nodes" not in value:
            continue
        value.pop("nodes", None)
        path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def remove_legacy_repository_inventory(gateway, repository: str) -> list[str]:
    """Commit removal of legacy inventory from the control repository's current branch, idempotently."""
    branch = gateway.default_branch(repository)
    files = set(gateway.repository_files(repository, branch))
    changed: list[str] = []
    for path in sorted(path for path in files if path == "accounts.yaml" or path.startswith("nodes/")):
        gateway.delete_file(repository, branch, path, "inventory: move machines and accounts to the database")
        changed.append(path)
    for path in sorted(path for path in files if path.startswith("projects/")
                       and path.lower().endswith((".yaml", ".yml"))):
        content = gateway.file_content(repository, branch, path)
        if content is None:
            continue
        try:
            value = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(value, dict) or "nodes" not in value:
            continue
        value.pop("nodes", None)
        gateway.commit_file(repository, branch, path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
                            "inventory: move project machine assignments to the database")
        changed.append(path)
    return changed


def import_control_repo(db, repo: ControlRepo) -> dict:
    """Copy the repo's accounts and nodes into the database once; later calls return {"imported": False}."""
    result: dict
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('aiflow4-inventory-import', 0))")
            first = conn.execute("INSERT INTO control_imports (name) VALUES (%s) ON CONFLICT DO NOTHING"
                                 " RETURNING name", (IMPORT_NAME,)).fetchone()
            if not first:
                result = {"imported": False, "accounts": 0, "machines": 0}
            else:
                machines = accounts = 0
                project_assignments: dict[str, list[str]] = {}
                for key, project in repo.projects.items():
                    for name in project.nodes or []:
                        project_assignments.setdefault(name, []).append(key)
                for name in sorted(set(repo.nodes) | set(project_assignments)):
                    node = repo.nodes.get(name)
                    projects = list(dict.fromkeys([*(node.projects if node else []), *project_assignments.get(name, [])]))
                    profile = ({"capabilities": node.capabilities, "purpose": node.purpose, "notes": node.notes,
                                "location": node.location, "tools": node.tools, "packages": node.packages,
                                "services": node.services} if node else {})
                    if node:
                        # Rows already synced from the repo keep their live heartbeat state; import configuration
                        # alongside the project assignments and retain it in the registry after file cleanup.
                        conn.execute(
                        "INSERT INTO nodes (name, ip, role, slots_total, projects, dynamic, profile)"
                        " VALUES (%s,%s,%s,%s,%s,false,%s) ON CONFLICT (name) DO UPDATE SET dynamic=false,"
                            " projects=nodes.projects || ARRAY(SELECT imported.project FROM unnest(EXCLUDED.projects)"
                            " AS imported(project) WHERE NOT (imported.project = ANY(nodes.projects))),"
                            " profile=EXCLUDED.profile",
                            (node.name, node.ip, node.role, node.slots, projects, J(profile)))
                    else:
                        # A project can assign a machine without a nodes/*.yaml hardware profile. Keep that assignment
                        # in the registry so project files stop being a live source after this one-time import.
                        conn.execute(
                            "INSERT INTO nodes (name, projects, dynamic, status, profile) VALUES (%s,%s,true,'offline','{}')"
                            " ON CONFLICT (name) DO UPDATE SET dynamic=true, projects=nodes.projects || ARRAY("
                            " SELECT imported.project FROM unnest(EXCLUDED.projects) AS imported(project)"
                            " WHERE NOT (imported.project = ANY(nodes.projects)))",
                            (name, projects))
                    machines += 1
                for account in repo.accounts:
                    # An account the owner already removed or changed on screen has a row: it is never overwritten.
                    conn.execute(
                        "INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,explicit_only,paused)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (account.id, account.provider, account.node, account.tier, list(account.roles),
                         min(8, max(1, int(account.parallel or 1))), account.explicit_only, account.paused))
                    conn.execute("INSERT INTO accounts (id,provider,node,tier,roles) VALUES (%s,%s,%s,%s,%s)"
                                 " ON CONFLICT (id) DO NOTHING",
                                 (account.id, account.provider, account.node, account.tier, list(account.roles)))
                    if account.session_limit:
                        conn.execute("INSERT INTO shared_accounts (id,provider,tier,roles,explicit_only,session_limit,"
                                     "managed,created_by) VALUES (%s,%s,%s,%s,%s,%s,false,'import')"
                                     " ON CONFLICT (id) DO NOTHING",
                                     (account.id, account.provider, account.tier, list(account.roles),
                                      account.explicit_only, min(50, int(account.session_limit))))
                    for extra in account.extra_machines:
                        conn.execute("INSERT INTO account_machines (account_id,node,status,enabled_by)"
                                     " VALUES (%s,%s,'signed_in','import') ON CONFLICT DO NOTHING", (account.id, extra))
                    accounts += 1
                detail = {"accounts": accounts, "machines": machines}
                conn.execute("UPDATE control_imports SET detail=%s WHERE name=%s", (J(detail), IMPORT_NAME))
                result = {"imported": True, **detail}
    # Keep the operation retryable: a failed cleanup does not repeat or overwrite the database import, but a later
    # invocation still removes any legacy files that a fresh checkout restored.
    remove_legacy_files(repo.root)
    return result
