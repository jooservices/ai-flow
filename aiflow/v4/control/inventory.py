"""One-time import of machines and AI accounts from the control repo into the control database.

Machines, AI accounts, their roles and worker counts are runtime facts (docs/v4/inventory-in-database.md). Older
installations kept them in `accounts.yaml` and `nodes/*.yaml`. `import_control_repo` copies them into
`managed_ai_accounts`, `nodes`, `shared_accounts` and `account_machines` exactly once, recorded in
`control_imports`. Afterwards the control repo files are no longer read for these facts and the Machines and AI
screen is the only place that changes them; a repeat call changes nothing.
"""
from __future__ import annotations

from dataclasses import replace

from ..core.controlrepo import Account, ControlRepo
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
    managed = [row for row in db.all("SELECT * FROM managed_ai_accounts WHERE removed_at IS NULL ORDER BY created_at,id")
               if row["node"] not in removed_nodes]
    if not managed and not removed_nodes and not removed_ids and not imported:
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
    return replace(base, accounts=list(accounts.values()), inventory_in_db=imported)


def import_control_repo(db, repo: ControlRepo) -> dict:
    """Copy the repo's accounts and nodes into the database once; later calls return {"imported": False}."""
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('aiflow4-inventory-import', 0))")
            first = conn.execute("INSERT INTO control_imports (name) VALUES (%s) ON CONFLICT DO NOTHING"
                                 " RETURNING name", (IMPORT_NAME,)).fetchone()
            if not first:
                return {"imported": False, "accounts": 0, "machines": 0}
            machines = accounts = 0
            project_assignments: dict[str, list[str]] = {}
            for key, project in repo.projects.items():
                for name in project.nodes or []:
                    project_assignments.setdefault(name, []).append(key)
            for name in sorted(set(repo.nodes) | set(project_assignments)):
                node = repo.nodes.get(name)
                projects = list(dict.fromkeys([*(node.projects if node else []), *project_assignments.get(name, [])]))
                # Rows already synced from the repo keep their live state; only the assignment moves to the database.
                if node:
                    conn.execute(
                        "INSERT INTO nodes (name, ip, role, slots_total, projects, dynamic) VALUES (%s,%s,%s,%s,%s,true)"
                        " ON CONFLICT (name) DO UPDATE SET dynamic=true, projects=nodes.projects || ARRAY("
                        " SELECT imported.project FROM unnest(EXCLUDED.projects) AS imported(project)"
                        " WHERE NOT (imported.project = ANY(nodes.projects)))",
                        (node.name, node.ip, node.role, node.slots, projects))
                else:
                    # A project can assign a machine without a nodes/*.yaml hardware profile. Keep that assignment
                    # in the registry so project files stop being a live source after this one-time import.
                    conn.execute(
                        "INSERT INTO nodes (name, projects, dynamic, status) VALUES (%s,%s,true,'offline')"
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
            return {"imported": True, **detail}
