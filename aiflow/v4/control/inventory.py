"""One-time import of machines and AI accounts from the control repo into the control database.

Machines, AI accounts, their roles and worker counts are runtime facts (docs/v4/inventory-in-database.md). Older
installations kept them in `accounts.yaml` and `nodes/*.yaml`. `import_control_repo` copies them into
`managed_ai_accounts`, `nodes`, `shared_accounts` and `account_machines` exactly once, recorded in
`control_imports` together with the imported revision. Afterwards the control repo files are no longer read for
these facts and the Machines and AI screen is the only place that changes them; a repeat call changes nothing.
SQL lives in `repositories/`; this module orchestrates it inside the import transaction.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import yaml

from ..core.controlrepo import Account, ControlRepo, Node
from .repositories import accounts as accounts_repo, imports, nodes as nodes_repo

log = logging.getLogger("aiflow4.inventory")
IMPORT_NAME = "inventory"


def is_imported(db) -> bool:
    return imports.is_done(db, IMPORT_NAME)


def needs_runtime_overlay(db) -> bool:
    """True when the live view must merge database inventory over the control-repo snapshot."""
    try:
        flags = db.one(
            "SELECT EXISTS(SELECT 1 FROM nodes WHERE removed_at IS NOT NULL) AS removed_nodes,"
            " EXISTS(SELECT 1 FROM managed_ai_accounts) AS managed_accounts,"
            " EXISTS(SELECT 1 FROM shared_accounts) AS shared_accounts,"
            " EXISTS(SELECT 1 FROM account_machines) AS account_machines,"
            " EXISTS(SELECT 1 FROM account_machine_exclusions) AS account_machine_exclusions,"
            " EXISTS(SELECT 1 FROM control_imports WHERE name=%s) AS inventory_imported,"
            " EXISTS(SELECT 1 FROM nodes WHERE cardinality(managed_projects) > 0) AS managed_projects",
            (IMPORT_NAME,)) or {}
    except Exception:  # noqa: BLE001 - missing columns before the inventory migrations: apply the merge
        return True
    return any(flags.values())


def apply(base: ControlRepo, db) -> ControlRepo:
    """The control repo snapshot with the database's accounts: after the import the database is the only source.

    Before the import, console-managed accounts are merged over the repo's. Removed accounts and accounts on
    removed machines never stay schedulable."""
    removed_nodes = nodes_repo.removed_names(db)
    removed_ids = accounts_repo.removed_ids(db)
    imported = is_imported(db)
    project_assignments = nodes_repo.managed_assignments(db)
    managed = [row for row in accounts_repo.active_managed(db) if row["node"] not in removed_nodes]
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
        for row in nodes_repo.active(db):
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


def legacy_repository_changes(gateway, repository: str, branch: str) -> tuple[list[str], dict[str, str]]:
    """The legacy inventory on a branch: paths to delete, and project files rewritten without their `nodes` list."""
    files = set(gateway.repository_files(repository, branch))
    deletions = sorted(path for path in files if path == "accounts.yaml" or path.startswith("nodes/"))
    writes: dict[str, str] = {}
    for path in sorted(path for path in files if path.startswith("projects/")
                       and path.lower().endswith((".yaml", ".yml"))):
        content = gateway.file_content(repository, branch, path)
        try:
            value = yaml.safe_load(content) if content is not None else None
        except yaml.YAMLError:
            continue
        if isinstance(value, dict) and "nodes" in value:
            value.pop("nodes", None)
            writes[path] = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    return deletions, writes


def _imported_machines(conn, repo: ControlRepo) -> int:
    """Insert the repo's machines and project assignments; rows synced earlier keep their live heartbeat state."""
    project_assignments: dict[str, list[str]] = {}
    for key, project in repo.projects.items():
        for name in project.nodes or []:
            project_assignments.setdefault(name, []).append(key)
    # Startup sync can replace nodes.projects with the current file assignments before this one-time import runs.
    # Keep projects created through the console, stored separately in nodes.managed_projects, as well.
    managed = nodes_repo.managed_assignments(conn)
    machines = 0
    for name in sorted(set(repo.nodes) | set(project_assignments) | set(managed)):
        node = repo.nodes.get(name)
        projects = list(dict.fromkeys([*(node.projects if node else []), *project_assignments.get(name, []),
                                       *managed.get(name, [])]))
        if node:
            profile = {"capabilities": node.capabilities, "purpose": node.purpose, "notes": node.notes,
                       "location": node.location, "tools": node.tools, "packages": node.packages,
                       "services": node.services}
            nodes_repo.upsert_imported(conn, node.name, node.ip, node.role, node.slots, projects, profile)
        else:
            # A project can assign a machine without a nodes/*.yaml hardware profile; keep that assignment.
            nodes_repo.upsert_assignment_only(conn, name, projects)
        machines += 1
    return machines


def import_control_repo(db, repo: ControlRepo, revision: str = "") -> dict:
    """Copy the repo's accounts and nodes into the database once; later calls return {"imported": False}.

    `revision` is the control repo commit the snapshot was read from. It is stored with the marker. Local legacy
    files stay until remote cleanup succeeds (or there is no remote), so a deferred cleanup leaves the checkout
    matching the still-uncleaned branch."""
    with db.pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('aiflow4-inventory-import', 0))")
        if imports.claim(conn, IMPORT_NAME):
            machines = _imported_machines(conn, repo)
            for account in repo.accounts:
                accounts_repo.import_account(conn, account)
            detail = {"accounts": len(repo.accounts), "machines": machines}
            imports.finish(conn, IMPORT_NAME, {**detail, "revision": revision})
            result = {"imported": True, **detail}
        else:
            result = {"imported": False, "accounts": 0, "machines": 0}
    return result


def checkout_revision(root: str | Path) -> str:
    """The commit the control repo checkout is at ("" when it is not a git checkout)."""
    try:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=30, check=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def imported_revision(db) -> str:
    """The control repo commit the one-time import was taken from ("" when unknown or not yet imported)."""
    return str(imports.detail(db, IMPORT_NAME).get("revision") or "")


def remote_cleaned(db) -> bool:
    return bool(imports.detail(db, IMPORT_NAME).get("remote_cleaned"))


def mark_remote_cleaned(database_url: str) -> None:
    """Record that the remote branch no longer carries legacy inventory, so later runs skip the cleanup."""
    from .db import DB
    db = DB(database_url)
    try:
        imports.merge_detail(db, IMPORT_NAME, {"remote_cleaned": True})
    finally:
        db.close()


def clean_remote(gateway_factory, repository: str, _revision: str = "") -> list[str] | None:
    """Remove legacy inventory from the remote branch's current head in one compare-and-swap commit.

    Only `accounts.yaml`, `nodes/*` and project-level `nodes` lists change. Newer policy on an advanced head stays.
    The import never re-reads those files, so accounts added on the remote after import are not imported. Returns
    the changed paths, or None when the cleanup is deferred (the branch advanced mid-commit). `_revision` is
    accepted for call-site compatibility and is not used as the CAS parent."""
    gateway = gateway_factory()
    branch = gateway.default_branch(repository)
    expected_sha = gateway.branch_sha(repository, branch)
    if not expected_sha:
        log.warning("legacy inventory cleanup in the control repository deferred (remote head is unknown)")
        return None
    deletions, writes = legacy_repository_changes(gateway, repository, branch)
    if not deletions and not writes:
        return []
    try:
        gateway.commit_changes(repository, branch, expected_sha, writes, deletions,
                               "inventory: move machines and accounts to the database")
    except Exception as exc:  # noqa: BLE001 - a branch that advanced mid-cleanup rejects the fast-forward
        log.warning("legacy inventory cleanup in the control repository deferred (%s)", type(exc).__name__)
        return None
    return [*deletions, *writes]


def import_and_clean(db, repo: ControlRepo, gateway_factory, repository: str) -> dict:
    """Run the one-time import; once done, remove the legacy files from the control repo (retried on failure).

    Local files are removed only after the remote commit succeeds, so a deferred cleanup leaves the checkout
    matching the still-uncleaned remote. When no GitHub App is configured, local removal is enough."""
    result = import_control_repo(db, repo, checkout_revision(repo.root))
    if is_imported(db) and not remote_cleaned(db):
        if gateway_factory is None or not repository:
            remove_legacy_files(repo.root)
            imports.merge_detail(db, IMPORT_NAME, {"remote_cleaned": True})
        else:
            try:
                if clean_remote(gateway_factory, repository, imported_revision(db)) is not None:
                    imports.merge_detail(db, IMPORT_NAME, {"remote_cleaned": True})
                    remove_legacy_files(repo.root)
            except Exception as exc:  # noqa: BLE001 - checkout and backup filtering still protect runtime state
                log.warning("legacy inventory cleanup in the control repository deferred (%s)", type(exc).__name__)
    elif is_imported(db) and remote_cleaned(db):
        remove_legacy_files(repo.root)
    if result["imported"]:
        log.info("imported %s machines and %s AI accounts from the control repo into the database",
                 result["machines"], result["accounts"])
    return result


def assign_project(connection, key: str, nodes: list[str]) -> None:
    """Let the given worker machines serve a new project (both the live and the console-managed lists)."""
    nodes_repo.assign_project(connection, key, nodes)
