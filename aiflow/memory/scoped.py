"""The Workspaces notes on this memory: entries in a scope (global, namespace, namespace/project), projects and devices.

The same commands and rules as the machine-local SQLite notes of `scripts/mem` (search sees global + the namespace (+ the project), a change
needs the exact scope, verify/promote/decay/consolidate/stale), so a client can switch the storage without changing how it is used.
Ids are integers (`seq`); every method returns plain values (dicts, ints, bools) that the service sends as JSON."""
from __future__ import annotations

import json
import re
from typing import Any

from . import guard
from .store import Store

ENTRY_TYPES = ("fact", "decision", "lesson", "knowledge", "idea", "reference")
DEVICE_STATUSES = ("active", "inactive", "maintenance")
STAMP = "to_char({c} at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
ENTRY_COLUMNS = ("id", "uuid", "scope", "namespace", "project", "type", "title", "content", "tags", "status", "jira_issue_key", "source",
                 "created_at", "updated_at", "verified_at")
ENTRY_SQL = ("select e.seq, e.id::text, e.scope, e.namespace, e.project, e.kind, e.title, e.body, coalesce(e.tags_raw, array_to_string(e.tags, ',')), "
             "e.status, nullif(e.jira_key, ''), coalesce(e.source ->> 'text', ''), " + ", ".join(STAMP.format(c=f"e.{c}") for c in ("created_at", "updated_at")) +
             ", case when e.verified_at is null then null else " + STAMP.format(c="e.verified_at") + " end from memory_entry e")


def check_scope(namespace: str | None, project: str | None, global_scope: bool) -> None:
    if global_scope and namespace:
        raise ValueError("Choose either global scope or a namespace.")
    if project and not namespace:
        raise ValueError("A project scope requires --namespace.")
    if not global_scope and not namespace:
        raise ValueError("Choose --global or --namespace explicitly.")


def entry(row: tuple) -> dict[str, Any]:
    item = dict(zip(ENTRY_COLUMNS, row))
    scope = item.pop("scope")
    if scope == "global":
        item["namespace"] = item["project"] = None
    elif scope == "namespace":
        item["project"] = None
    item["id"] = int(item["id"])
    return item


def terms(query: str) -> str:
    words = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    if not words:
        raise ValueError("Search query must contain at least one word.")
    if len(words) > 40:
        raise ValueError("Search query is too long; use a focused set of terms.")
    pieces: list[str] = []
    for word in words:
        pieces += [p for p in re.findall(r"\w+", word, flags=re.UNICODE)]
    return " | ".join(dict.fromkeys(pieces))


class Scoped:
    def __init__(self, store: Store) -> None:
        self.store = store

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.store.run(*args, **kwargs)

    # ---- scope conditions (the same as the SQLite store) -------------------------------------------------------------------
    @staticmethod
    def reading(namespace: str | None, project: str | None, global_scope: bool) -> tuple[str, list[Any]]:
        check_scope(namespace, project, global_scope)
        if global_scope:
            return "e.scope = 'global'", []
        clause, args = "(e.scope = 'global' or (e.scope = 'namespace' and e.namespace = %s)", [namespace]
        if project:
            clause += " or (e.scope = 'project' and e.namespace = %s and e.project = %s)"
            args += [namespace, project]
        return clause + ")", args

    @staticmethod
    def exact(namespace: str | None, project: str | None, global_scope: bool) -> tuple[str, list[Any]]:
        check_scope(namespace, project, global_scope)
        if global_scope:
            return "e.scope = 'global'", []
        if project:
            return "(e.scope = 'project' and e.namespace = %s and e.project = %s)", [namespace, project]
        return "(e.scope = 'namespace' and e.namespace = %s)", [namespace]

    def seq_in(self, seq: int, namespace: str | None, project: str | None, global_scope: bool) -> bool:
        clause, args = self.exact(namespace, project, global_scope)
        return bool(self.run(f"select 1 from memory_entry e where e.seq = %s and {clause}", (seq, *args)))

    # ---- entries -----------------------------------------------------------------------------------------------------------------
    def add(self, title: str, content: str = "", entry_type: str = "fact", namespace: str | None = None, project: str | None = None,
            global_scope: bool = False, tags: str = "", jira_issue_key: str | None = None, source: str = "", created_by: str = "") -> int:
        check_scope(namespace, project, global_scope)
        if not title.strip():
            raise ValueError("Entry title is required.")
        if entry_type not in ENTRY_TYPES:
            raise ValueError(f"Unsupported entry type: {entry_type}")
        scope = "global" if global_scope else ("project" if project else "namespace")
        namespace = "" if global_scope else (namespace or "").strip()
        project = (project or "").strip() if not global_scope else ""
        try:
            clean = guard.check_entry(dict(kind=entry_type, scope=scope, namespace=namespace, project=project, title=title, body=content, tags=[]), relaxed=True)
        except guard.Rejected as error:
            raise ValueError(str(error)) from error
        if project:
            self.run("insert into memory_project (namespace, name) values (%s, %s) on conflict do nothing", (namespace, project), fetch=False)
        digest = guard.content_hash(entry_type, clean["title"], clean["body"])
        rows = self.run(
            "insert into memory_entry (kind, scope, namespace, project, jira_key, title, body, tags, tags_raw, source, status, content_hash, created_by, updated_by) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'fresh', %s, %s, %s) "
            "on conflict (namespace, project, content_hash) where status <> 'decayed' do nothing returning seq",
            (entry_type, scope, namespace, project, (jira_issue_key or "").strip(), clean["title"], clean["body"],
             [t.strip().lower() for t in tags.split(",") if t.strip()][:12], tags.strip(), json.dumps({"text": source.strip()}), digest, created_by, created_by))
        if rows:
            return int(rows[0][0])
        return int(self.run("select seq from memory_entry where namespace = %s and project = %s and content_hash = %s and status <> 'decayed'",
                            (namespace, project, digest))[0][0])

    def get(self, id: int) -> dict[str, Any] | None:  # noqa: A002 - the name the clients use
        rows = self.run(f"{ENTRY_SQL} where e.seq = %s", (id,))
        return entry(rows[0]) if rows else None

    def search(self, query: str, namespace: str | None = None, project: str | None = None, global_scope: bool = False,
               jira_issue_key: str | None = None, limit: int = 10, include_decayed: bool = False) -> list[dict[str, Any]]:
        clause, args = self.reading(namespace, project, global_scope)
        match = terms(query)
        sql = f"{ENTRY_SQL}, to_tsquery('simple', mem_unaccent(%s)) q where e.tsv @@ q and {clause}"
        values: list[Any] = [match, *args]
        if jira_issue_key:
            sql += " and e.jira_key = %s"
            values.append(jira_issue_key.strip())
        if not include_decayed:
            sql += " and e.status <> 'decayed'"
        sql += " order by ts_rank_cd(e.tsv, q) desc, e.seq limit %s"
        return [entry(row) for row in self.run(sql, (*values, int(limit)))]

    def list(self, namespace: str | None = None, project: str | None = None, global_scope: bool = False, jira_issue_key: str | None = None,
             entry_type: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clause, args = self.reading(namespace, project, global_scope)
        sql, values = f"{ENTRY_SQL} where {clause}", list(args)
        if jira_issue_key:
            sql, values = sql + " and e.jira_key = %s", values + [jira_issue_key.strip()]
        if entry_type:
            sql, values = sql + " and e.kind = %s", values + [entry_type]
        if status:
            sql, values = sql + " and e.status = %s", values + [status]
        else:
            sql += " and e.status <> 'decayed'"
        return [entry(row) for row in self.run(sql + " order by e.updated_at desc, e.seq desc limit %s", (*values, int(limit)))]

    def update(self, id: int, namespace: str | None = None, project: str | None = None, global_scope: bool = False, title: str | None = None,  # noqa: A002
               content: str | None = None, entry_type: str | None = None, tags: str | None = None, jira_issue_key: str | None = None,
               source: str | None = None) -> bool:
        check_scope(namespace, project, global_scope)
        sets, values = [], []
        for column, value in (("title", title), ("body", content), ("kind", entry_type), ("tags_raw", tags), ("jira_key", jira_issue_key), ("source", source)):
            if value is None:
                continue
            if column == "title" and not value.strip():
                raise ValueError("Entry title cannot be empty.")
            if column == "kind" and value not in ENTRY_TYPES:
                raise ValueError(f"Unsupported entry type: {value}")
            if column == "tags_raw":
                sets.append("tags = %s")
                values.append([t.strip().lower() for t in value.split(",") if t.strip()][:12])
            if column == "source":
                sets.append("source = %s::jsonb")
                values.append(json.dumps({"text": value.strip()}))
                continue
            sets.append(f"{column} = %s")
            values.append(value.strip())
        if not sets:
            raise ValueError("Provide at least one field to update.")
        if not self.seq_in(id, namespace, project, global_scope):
            return False
        current = self.get(id)
        new_title, new_body, new_kind = (title.strip() if title is not None else current["title"]), (content.strip() if content is not None else current["content"]), \
            (entry_type or current["type"])
        try:
            guard.scan_secrets(new_title, new_body)
        except guard.Rejected as error:
            raise ValueError(str(error)) from error
        sets += ["content_hash = %s", "updated_at = now()"]
        values.append(guard.content_hash(new_kind, new_title, new_body))
        try:
            self.run(f"update memory_entry e set {', '.join(sets)} where e.seq = %s", (*values, id), fetch=False)
        except Exception as error:
            if "memory_entry_unique_live" in str(error):
                raise ValueError("An identical entry already exists in this scope.") from error
            raise
        return True

    def status(self, id: int, status: str, namespace: str | None = None, project: str | None = None, global_scope: bool = False) -> bool:  # noqa: A002
        if status not in ("verified", "promoted", "decayed"):
            raise ValueError(f"Unsupported lifecycle action: {status}")
        check_scope(namespace, project, global_scope)
        if not self.seq_in(id, namespace, project, global_scope):
            return False
        current = self.run("select status from memory_entry where seq = %s", (id,))[0][0]
        if status == "verified":
            if current == "decayed":
                raise ValueError("Archived entries cannot be verified again.")
            self.run("update memory_entry set status = case when status = 'fresh' then 'verified' else status end, verified_at = now(), updated_at = now() where seq = %s",
                     (id,), fetch=False)
        elif status == "promoted":
            if current not in ("verified", "promoted"):
                raise ValueError("Verify the entry against current evidence before promoting it.")
            self.run("update memory_entry set status = 'promoted', updated_at = now() where seq = %s", (id,), fetch=False)
        else:
            self.run("update memory_entry set status = 'decayed', decayed_at = now(), updated_at = now() where seq = %s", (id,), fetch=False)
        self.store.event("scoped", status, None, {"seq": id})
        return True

    def stale(self, namespace: str | None = None, project: str | None = None, global_scope: bool = False, older_than_days: int = 90,
              include_unverified: bool = False) -> list[dict[str, Any]]:
        if older_than_days < 0:
            raise ValueError("The age window must be zero or greater.")
        clause, args = self.reading(namespace, project, global_scope)
        fresh = ("((e.verified_at is null and e.created_at < now() - make_interval(days => %s)) or e.verified_at < now() - make_interval(days => %s))"
                 if include_unverified else "(e.verified_at is not null and e.verified_at < now() - make_interval(days => %s))")
        days = [older_than_days, older_than_days] if include_unverified else [older_than_days]
        sql = f"{ENTRY_SQL} where e.status <> 'decayed' and {clause} and {fresh} order by coalesce(e.verified_at, e.created_at), e.seq"
        return [entry(row) for row in self.run(sql, (*args, *days))]

    def consolidate(self, namespace: str | None = None, project: str | None = None, global_scope: bool = False, older_than_days: int = 90) -> dict[str, int]:
        if older_than_days < 0:
            raise ValueError("The age window must be zero or greater.")
        check_scope(namespace, project, global_scope)
        if global_scope:
            scope, args = "e.scope = 'global'", []
        elif project:
            scope, args = "(e.scope = 'project' and e.namespace = %s and e.project = %s)", [namespace, project]
        else:
            scope, args = "(e.namespace = %s and e.scope in ('namespace', 'project'))", [namespace]
        promoted = self.run(f"update memory_entry e set status = 'promoted', updated_at = now() where {scope} and e.status = 'verified' returning e.seq", tuple(args))
        decayed = self.run(f"update memory_entry e set status = 'decayed', decayed_at = now(), updated_at = now() where {scope} and e.status <> 'decayed' "
                           f"and e.verified_at is null and e.created_at < now() - make_interval(days => %s) returning e.seq", (*args, older_than_days))
        return {"promoted": len(promoted), "decayed": len(decayed)}

    def stats(self) -> dict[str, int]:
        counts = {"total": 0}
        for kind, status, amount in self.run("select kind, status, count(*) from memory_entry group by kind, status order by kind, status"):
            counts["total"] += int(amount)
            counts[f"{kind}:{status}"] = int(amount)
        return counts

    # ---- projects ----------------------------------------------------------------------------------------------------------------
    PROJECT_COLUMNS = ("id", "namespace", "name", "description", "tags", "github_url", "jira_key", "jira_project_url", "published_as", "version", "stable")

    def projects(self, namespace: str | None = None) -> list[dict[str, Any]]:
        sql = f"select {', '.join(self.PROJECT_COLUMNS)} from memory_project"
        rows = self.run(sql + (" where namespace = %s" if namespace else "") + " order by namespace, name", (namespace,) if namespace else ())
        return [{**dict(zip(self.PROJECT_COLUMNS, row)), "stable": int(row[-1])} for row in rows]

    def project_update(self, namespace: str, name: str, description: str | None = None, tags: str | None = None, github_url: str | None = None,
                       jira_key: str | None = None, jira_project_url: str | None = None, published_as: str | None = None, version: str | None = None,
                       stable: bool | None = None) -> bool:
        namespace, name = namespace.strip(), name.strip()
        if not namespace or not name:
            raise ValueError("Namespace and project name are required.")
        sets, values = [], []
        for column, value in (("description", description), ("tags", tags), ("github_url", github_url), ("jira_key", jira_key),
                              ("jira_project_url", jira_project_url), ("published_as", published_as), ("version", version)):
            if value is None:
                continue
            clean = value.strip()
            sets.append(f"{column} = %s")
            values.append(None if column in ("jira_key", "jira_project_url", "published_as", "version") and not clean else clean)
        if stable is not None:
            sets.append("stable = %s")
            values.append(bool(stable))
        if not sets:
            raise ValueError("Provide at least one project field to update.")
        return bool(self.run(f"update memory_project set {', '.join(sets)} where namespace = %s and name = %s returning id", (*values, namespace, name)))

    # ---- devices -----------------------------------------------------------------------------------------------------------------
    DEVICE_COLUMNS = ("id", "uuid", "namespace", "name", "role", "host", "ip", "model", "firmware", "location", "status", "notes")

    def device(self, row: tuple) -> dict[str, Any]:
        return {**dict(zip(self.DEVICE_COLUMNS, row[:12])), "created_at": row[12], "updated_at": row[13]}

    def device_select(self) -> str:
        return (f"select id, uuid::text, namespace, name, role, host, ip, model, firmware, location, status, notes, {STAMP.format(c='created_at')}, "
                f"{STAMP.format(c='updated_at')} from memory_device")

    def device_add(self, namespace: str, name: str, role: str = "", host: str = "", ip: str = "", model: str = "", firmware: str = "", location: str = "",
                   status: str = "active", notes: str = "") -> int:
        namespace, name = namespace.strip(), name.strip()
        if not namespace or not name:
            raise ValueError("Namespace and device name are required.")
        if status not in DEVICE_STATUSES:
            raise ValueError(f"Unsupported device status: {status}")
        guard.scan_secrets(name, notes, host, location)
        return int(self.run("insert into memory_device (namespace, name, role, host, ip, model, firmware, location, status, notes) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                            (namespace, name, role.strip(), host.strip(), ip.strip(), model.strip(), firmware.strip(), location.strip(), status, notes.strip()))[0][0])

    def device_list(self, namespace: str, role: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not namespace.strip():
            raise ValueError("A namespace is required to list devices.")
        if status is not None and status not in DEVICE_STATUSES:
            raise ValueError(f"Unsupported device status: {status}")
        if limit < 1:
            raise ValueError("Device list limit must be at least 1.")
        sql, values = f"{self.device_select()} where namespace = %s", [namespace.strip()]
        if role:
            sql, values = sql + " and role = %s", values + [role]
        if status:
            sql, values = sql + " and status = %s", values + [status]
        return [self.device(row) for row in self.run(sql + " order by name, location limit %s", (*values, int(limit)))]

    def device_update(self, id: int, namespace: str, name: str | None = None, role: str | None = None, host: str | None = None, ip: str | None = None,  # noqa: A002
                      model: str | None = None, firmware: str | None = None, location: str | None = None, status: str | None = None,
                      notes: str | None = None) -> bool:
        if not namespace.strip():
            raise ValueError("A namespace is required to update a device.")
        sets, values = [], []
        for column, value in (("name", name), ("role", role), ("host", host), ("ip", ip), ("model", model), ("firmware", firmware), ("location", location),
                              ("status", status), ("notes", notes)):
            if value is None:
                continue
            clean = value.strip()
            if column == "name" and not clean:
                raise ValueError("Device name cannot be empty.")
            if column == "status" and clean not in DEVICE_STATUSES:
                raise ValueError(f"Unsupported device status: {clean}")
            sets.append(f"{column} = %s")
            values.append(clean)
        if not sets:
            raise ValueError("Provide at least one device field to update.")
        guard.scan_secrets(*[v for v in values if isinstance(v, str)])
        return bool(self.run(f"update memory_device set {', '.join(sets)}, updated_at = now() where namespace = %s and id = %s returning id",
                             (*values, namespace.strip(), id)))

    # ---- one-time import of the machine-local notes (idempotent through `origin`) ---------------------------------------------------
    def import_entry(self, origin: str, seq: int, uuid: str, scope: str, namespace: str, project: str, kind: str, title: str, body: str, tags_raw: str,
                     jira_key: str, source: str, status: str, created_by: str, updated_by: str, created_at: str, updated_at: str,
                     verified_at: str | None) -> bool:
        guard.scan_secrets(title, body)
        if project:
            self.run("insert into memory_project (namespace, name) values (%s, %s) on conflict do nothing", (namespace, project), fetch=False)
        rows = self.run(
            "insert into memory_entry (origin, seq, id, scope, namespace, project, kind, title, body, tags, tags_raw, jira_key, source, status, content_hash, "
            "created_by, updated_by, created_at, updated_at, verified_at, confirmations) values (%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
            "%s, %s, %s, %s, %s::timestamptz, %s::timestamptz, %s::timestamptz, 1) on conflict (origin) where origin is not null do nothing returning seq",
            (origin, seq, uuid, scope, namespace, project, kind, title, body, [t.strip().lower() for t in tags_raw.split(",") if t.strip()][:12], tags_raw,
             jira_key or "", json.dumps({"text": source or ""}), status, guard.content_hash(kind, title, body), created_by, updated_by,
             created_at + "+00", updated_at + "+00", (verified_at + "+00") if verified_at else None))
        self.run("select setval('memory_entry_seq', greatest((select max(seq) from memory_entry), 1))")
        return bool(rows)

    def import_project(self, namespace: str, name: str, description: str, tags: str, github_url: str, jira_key: str | None, jira_project_url: str | None,
                       published_as: str | None, version: str | None, stable: bool) -> bool:
        return bool(self.run("insert into memory_project (namespace, name, description, tags, github_url, jira_key, jira_project_url, published_as, version, stable) "
                             "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (namespace, name) do update set description = excluded.description, "
                             "tags = excluded.tags, github_url = excluded.github_url, jira_key = excluded.jira_key, jira_project_url = excluded.jira_project_url, "
                             "published_as = excluded.published_as, version = excluded.version, stable = excluded.stable returning id",
                             (namespace, name, description, tags, github_url, jira_key, jira_project_url, published_as, version, bool(stable))))

    def import_device(self, uuid: str, namespace: str, name: str, role: str, host: str, ip: str, model: str, firmware: str, location: str, status: str, notes: str,
                      created_at: str, updated_at: str) -> bool:
        return bool(self.run("insert into memory_device (uuid, namespace, name, role, host, ip, model, firmware, location, status, notes, created_at, updated_at) "
                             "values (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz) on conflict (namespace, uuid) do nothing returning id",
                             (uuid, namespace, name, role, host, ip, model, firmware, location, status, notes, created_at + "+00", updated_at + "+00")))


OPERATIONS = ("add", "get", "search", "list", "update", "status", "stale", "consolidate", "stats", "projects", "project_update", "device_add", "device_list",
              "device_update", "import_entry", "import_project", "import_device")
