"""Backup and restore primitives exercised by the clean-install acceptance test.

This is deliberately not exposed as an operator CLI command: the feature has no Command Center flow yet. The
acceptance-only helper writes one archive (mode 0600) with
- the registry: every table of the public schema as PostgreSQL COPY text (users, projects, machines and their tokens,
  tickets, runs), in an order that respects foreign keys,
- the secret files of the control machine (`/etc/aiflow/secrets/control`, `/etc/aiflow/secrets/console`,
  `/etc/aiflow/secrets/jira` and `/etc/aiflow/secrets/github`), which hold the keys the sessions, machines' tokens and
  connected integrations depend on,
- `CURRENT`, the release the backup was taken on.

Restore runs on a control machine whose release is installed (so the schema exists): it puts the
secret files back (directory 0700, files 0600), replaces the registry content and tells which release to run. People
sign in again, machines carry on with their node tokens, and Jira and GitHub integrations keep working from their
restored credentials. Secret values are never printed.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
from pathlib import Path

import psycopg
from psycopg import sql

from .control import Layout

FORMAT = 1
SECRET_DIRS = ("control", "console", "jira", "github")  # below /etc/aiflow/secrets


class BackupError(Exception):
    pass


def _tables(conn: psycopg.Connection) -> list[str]:
    """Public tables, parents before the tables that reference them."""
    names = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    edges = conn.execute(
        "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid=c.connamespace WHERE c.contype='f' AND n.nspname='public'").fetchall()
    parents = {n: {p for child, p in edges if child == n and p != n} for n in names}
    ordered: list[str] = []
    while parents:
        ready = sorted(n for n, ps in parents.items() if ps <= set(ordered))
        if not ready:  # a cycle: keep the rest in name order; the restore defers nothing it cannot
            ready = sorted(parents)
        for n in ready:
            ordered.append(n)
            del parents[n]
    return ordered


def _reset_owned_sequences(conn: psycopg.Connection, tables: list[str]) -> None:
    """Advance each sequence owned by a restored column past the restored data."""
    for table in tables:
        relation = sql.Identifier("public", table).as_string(conn)
        sequences = conn.execute(
            "SELECT a.attname, pg_get_serial_sequence(%s, a.attname) "
            "FROM pg_class t JOIN pg_namespace n ON n.oid=t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=t.oid "
            "WHERE n.nspname='public' AND t.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
            "AND pg_get_serial_sequence(%s, a.attname) IS NOT NULL",
            (relation, table, relation),
        ).fetchall()
        for column, sequence in sequences:
            maximum = conn.execute(
                sql.SQL("SELECT max({}) FROM {}")
                .format(sql.Identifier(column), sql.Identifier("public", table))
            ).fetchone()[0]
            conn.execute(
                "SELECT setval(%s::regclass, COALESCE(%s::bigint, 1), %s)",
                (sequence, maximum, maximum is not None),
            )


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _check_member(name: str, manifest: dict) -> None:
    parts = name.split("/")
    if name == "manifest.json":
        return
    if len(parts) == 2 and parts[0] == "db" and parts[1].endswith(".copy") and parts[1][:-5] in manifest["tables"]:
        return
    if (len(parts) == 3 and parts[0] == "secrets" and parts[1] in SECRET_DIRS
            and parts[2] not in ("", ".", "..") and "\0" not in parts[2]):
        return
    raise BackupError(f"unexpected entry {name} in the backup")


def create(database_url: str, out: Path, layout: Layout | None = None) -> dict:
    lay = layout or Layout()
    out = Path(out)
    # a securely created temporary file next to the destination (mode 0600), then an atomic replace: an existing
    # destination with looser permissions is never reused
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    tmp = Path(tmp_name)
    try:
        manifest = _write(fd, database_url, lay)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return manifest


def _write(fd: int, database_url: str, lay: Layout) -> dict:
    os.fchmod(fd, 0o600)
    counts: dict[str, int] = {}
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar, \
            psycopg.connect(database_url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")  # one consistent snapshot
        tables = _tables(conn)
        for t in tables:
            with conn.cursor().copy(f'COPY "{t}" TO STDOUT') as copy:
                data = b"".join(bytes(chunk) for chunk in copy)
            counts[t] = int(conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
            _add(tar, f"db/{t}.copy", data)
        for d in SECRET_DIRS:
            src = lay.p("/etc/aiflow/secrets") / d
            for f in sorted(src.glob("*")) if src.is_dir() else []:
                if f.is_file():
                    _add(tar, f"secrets/{d}/{f.name}", f.read_bytes())
        current = lay.releases / "CURRENT"
        release = current.read_text().strip() if current.is_file() else ""
        manifest = {"format": FORMAT, "release": release, "tables": tables, "counts": counts,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _add(tar, "manifest.json", json.dumps(manifest).encode())
    return manifest


def restore(database_url: str, archive: Path, layout: Layout | None = None) -> dict:
    lay = layout or Layout()
    with tarfile.open(archive, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        if "manifest.json" not in members:
            raise BackupError(f"{archive} is not an AI Flow backup (no manifest.json)")
        manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
        if manifest.get("format") != FORMAT:
            raise BackupError(f"unsupported backup format {manifest.get('format')}")
        for name in members:  # exactly the entries this module writes, all checked before anything is changed
            _check_member(name, manifest)
        for t in manifest["tables"]:
            if f"db/{t}.copy" not in members:
                raise BackupError(f"the backup lacks the data of table {t}")
        # Restore is a product operation. Mark its connection as internal so the direct-SQL audit trigger
        # does not mistake the restore's COPY statements for human intervention.
        with psycopg.connect(database_url, application_name="aiflow4-control-restore") as conn:
            existing = set(_tables(conn))
            missing = [t for t in manifest["tables"] if t not in existing]
            if missing:
                raise BackupError("the installed release lacks tables of the backup (" + ", ".join(missing)
                                  + "); install the release named in the backup first: " + manifest["release"])
            # Security migration 013 makes the audit log append-only, including at the database level. Restore
            # replaces mutable registry tables, but must preserve destination audit history and append the archived
            # history without issuing UPDATE, DELETE or TRUNCATE against that table.
            # Product identity and intervention evidence are control metadata, not replaceable registry state.
            # Truncating audit_product_roles would make subsequent TRUNCATEs look like human actions, and
            # truncating the append-only ledger would erase evidence during restore.
            mutable = sorted(existing - {"audit_log", "audit_product_roles", "operator_interventions"})
            if mutable:
                conn.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in mutable) + " CASCADE")
            for t in manifest["tables"]:
                if t == "audit_log":
                    conn.execute("CREATE TEMP TABLE _aiflow_restore_audit_log (LIKE public.audit_log) ON COMMIT DROP")
                    with conn.cursor().copy("COPY _aiflow_restore_audit_log FROM STDIN") as copy:
                        copy.write(tar.extractfile(members[f"db/{t}.copy"]).read())
                    conn.execute(
                        "INSERT INTO audit_log (ts, actor, category, action, target, ip, detail) "
                        "SELECT source.ts, source.actor, source.category, source.action, source.target, source.ip, "
                        "source.detail FROM _aiflow_restore_audit_log AS source "
                        "WHERE NOT EXISTS (SELECT 1 FROM audit_log AS current WHERE "
                        "current.ts=source.ts AND current.actor=source.actor AND current.category=source.category "
                        "AND current.action=source.action AND current.target=source.target AND current.ip=source.ip "
                        "AND current.detail=source.detail) ORDER BY source.id"
                    )
                    continue
                if t == "audit_product_roles":
                    # Keep the roles registered on the destination database; they determine product-vs-human audit.
                    continue
                if t == "operator_interventions":
                    # Preserve destination evidence and append archived events idempotently by their stable source key.
                    # The id is deliberately omitted so the destination sequence remains authoritative.
                    conn.execute("CREATE TEMP TABLE _aiflow_restore_operator_interventions "
                                 "(LIKE public.operator_interventions INCLUDING DEFAULTS) ON COMMIT DROP")
                    with conn.cursor().copy("COPY _aiflow_restore_operator_interventions FROM STDIN") as copy:
                        copy.write(tar.extractfile(members[f"db/{t}.copy"]).read())
                    conn.execute(
                        "INSERT INTO operator_interventions "
                        "(source_key,happened_at,source,actor,action,target,reason,details,bug_ticket_key,created_at,session_id) "
                        "SELECT source_key,happened_at,source,actor,action,target,reason,details,bug_ticket_key,created_at,session_id "
                        "FROM _aiflow_restore_operator_interventions ORDER BY id "
                        "ON CONFLICT (source_key) DO NOTHING"
                    )
                    continue
                with conn.cursor().copy(f'COPY "{t}" FROM STDIN') as copy:
                    copy.write(tar.extractfile(members[f"db/{t}.copy"]).read())
            _reset_owned_sequences(conn, manifest["tables"])
        for name, m in members.items():
            if name.startswith("secrets/"):
                _, d, fname = name.split("/", 2)
                target_dir = lay.p("/etc/aiflow/secrets") / d
                target_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(target_dir, 0o700)
                fd = os.open(target_dir / fname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(fd, 0o600)  # also for a file that already existed with looser permissions
                with os.fdopen(fd, "wb") as f:
                    f.write(tar.extractfile(m).read())
    return manifest
