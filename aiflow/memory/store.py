"""The memory in PostgreSQL. Every query is parameterised; the caller passes the namespace and this module enforces the boundary:
a search sees the global entries and the entries of that one namespace, never another company's."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from . import guard
from .embed import Embedder, entry_text, literal

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VECTOR = Path(__file__).with_name("schema_vector.sql")
COLUMNS = ("id", "kind", "scope", "namespace", "project", "repo", "jira_key", "title", "body", "tags", "source", "status", "confidence", "supersedes",
           "created_by", "confirmations", "created_at", "verified_at", "last_used_at", "use_count", "helpful", "unhelpful")
VISIBLE = ("verified", "promoted")
QUERY_TIMEOUT = 3.0  # a search never waits long for Ollama: it falls back to words


def public(row: tuple, names: tuple[str, ...]) -> dict[str, Any]:
    item = dict(zip(names, row))
    for key, value in list(item.items()):
        if hasattr(value, "isoformat"):
            item[key] = value.isoformat()
        elif key in ("id", "supersedes") and value is not None:
            item[key] = str(value)
    return item


class NotFound(LookupError):
    pass


class Store:
    def __init__(self, connect: Callable[[], Any], embedder: Embedder | None = None) -> None:
        self.connect = connect
        self.embedder = embedder  # None = the search works by words only

    # ---- plumbing ------------------------------------------------------------------------------------------------
    def run(self, sql: str, args: tuple = (), fetch: bool = True) -> list[tuple]:
        conn = self.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, args)
            rows = cursor.fetchall() if fetch and cursor.description else []
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def migrate(self) -> None:
        self.apply(SCHEMA)
        try:
            self.apply(SCHEMA_VECTOR)
        except Exception:
            self.embedder = None  # no pgvector in this PostgreSQL: the search works by words only

    def apply(self, script: Path) -> None:
        conn = self.connect()
        try:
            cursor = conn.cursor()
            for statement in self.statements(script.read_text(encoding="utf-8")):
                cursor.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def statements(script: str) -> list[str]:
        """Split the schema on `;` at the end of a line, outside of $$ ... $$ bodies; comments removed."""
        out, current, inside = [], [], False
        for line in script.splitlines():
            if line.strip().startswith("--"):
                continue
            current.append(line)
            inside ^= line.count("$$") % 2 == 1
            if not inside and line.rstrip().endswith(";"):
                out.append("\n".join(current).strip().rstrip(";"))
                current = []
        return [s for s in out if s]

    def event(self, actor: str, action: str, entry_id: str | None, detail: dict | None = None) -> None:
        self.run("insert into memory_event (actor, action, entry_id, detail) values (%s, %s, %s, %s::jsonb)",
                 (actor, action, entry_id, json.dumps(detail or {})), fetch=False)

    def ping(self) -> bool:
        return self.run("select 1")[0][0] == 1

    # ---- write ---------------------------------------------------------------------------------------------------
    def put(self, entry: dict, actor: str, status: str = "fresh") -> tuple[str, bool]:
        """Store a proposed entry. Returns (id, created). The same content proposed again confirms the existing entry instead."""
        clean = guard.check_entry(entry)
        if status not in ("fresh", "verified", "promoted"):
            raise guard.Rejected("status must be fresh, verified or promoted")
        digest = guard.content_hash(clean["kind"], clean["title"], clean["body"])
        found = self.run("select id, created_by, status from memory_entry where namespace = %s and project = %s and content_hash = %s and status <> 'decayed'",
                         (clean["namespace"], clean["project"], digest))
        if found:
            entry_id, creator, current = str(found[0][0]), found[0][1], found[0][2]
            if clean["created_by"] and clean["created_by"] != creator:
                self.run("update memory_entry set confirmations = confirmations + 1, updated_at = now() where id = %s", (entry_id,), fetch=False)
            if status != "fresh" and current == "fresh":
                self.run("update memory_entry set status = %s, verified_at = now(), updated_at = now() where id = %s", (status, entry_id), fetch=False)
            self.event(actor, "confirm", entry_id, {"by": clean["created_by"]})
            return entry_id, False
        rows = self.run(
            "insert into memory_entry (kind, scope, namespace, project, repo, jira_key, title, body, tags, source, status, confidence, supersedes, "
            "expires_at, content_hash, created_by, verified_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, "
            "case when %s = 'fresh' then null else now() end) returning id",
            (clean["kind"], clean["scope"], clean["namespace"], clean["project"], clean["repo"], clean["jira_key"], clean["title"], clean["body"],
             clean["tags"], json.dumps(clean["source"]), status, clean["confidence"], clean["supersedes"], clean["expires_at"], digest,
             clean["created_by"], status))
        entry_id = str(rows[0][0])
        if clean["supersedes"] and status != "fresh":
            self.decay(str(clean["supersedes"]), actor, f"superseded by {entry_id}")
        self.event(actor, "propose" if status == "fresh" else "add", entry_id, {"kind": clean["kind"], "scope": clean["scope"], "status": status})
        return entry_id, True

    def set_status(self, entry_id: str, actor: str, status: str, reason: str = "") -> None:
        sql = {"verified": "update memory_entry set status = 'verified', verified_at = now(), updated_at = now() where id = %s and status in ('fresh', 'verified') returning id",
               "promoted": "update memory_entry set status = 'promoted', updated_at = now() where id = %s and status in ('verified', 'promoted') returning id",
               "decayed": "update memory_entry set status = 'decayed', decayed_at = now(), updated_at = now(), decay_reason = %s where id = %s and status <> 'decayed' returning id"}
        args = (reason[:300], entry_id) if status == "decayed" else (entry_id,)
        if not self.run(sql[status], args):
            raise NotFound(f"{entry_id}: not found or not in a state that allows {status}")
        self.event(actor, status, entry_id, {"reason": reason[:300]} if reason else {})

    def verify(self, entry_id: str, actor: str) -> None:
        self.set_status(entry_id, actor, "verified")

    def promote(self, entry_id: str, actor: str) -> None:
        self.set_status(entry_id, actor, "promoted")

    def decay(self, entry_id: str, actor: str, reason: str) -> None:
        self.set_status(entry_id, actor, "decayed", reason)

    def feedback(self, entry_id: str, actor: str, jira_key: str, helpful: bool) -> None:
        if not self.run("select 1 from memory_entry where id = %s", (entry_id,)):
            raise NotFound(entry_id)
        self.run("insert into memory_feedback (entry_id, jira_key, helpful, actor) values (%s, %s, %s, %s)", (entry_id, jira_key, helpful, actor), fetch=False)
        column = "helpful" if helpful else "unhelpful"
        self.run(f"update memory_entry set {column} = {column} + 1, updated_at = now() where id = %s", (entry_id,), fetch=False)
        self.event(actor, "feedback", entry_id, {"helpful": helpful, "ticket": jira_key})

    # ---- vectors (search by meaning) -----------------------------------------------------------------------------
    def embed_pending(self, batch: int = 16) -> int:
        """Give a vector to the entries that have none (or one of another model). Returns how many were filled; 0 when Ollama is not available."""
        if self.embedder is None:
            return 0
        rows = self.run("select id, title, body, tags from memory_entry where status <> 'decayed' and (embedding is null or embedding_model is distinct from %s) "
                        "order by created_at limit %s", (self.embedder.model, batch))
        if not rows:
            return 0
        vectors = self.embedder.embed([entry_text(title, body, tags) for _, title, body, tags in rows])
        if vectors is None:
            return 0
        for (entry_id, *_), vector in zip(rows, vectors):
            self.run("update memory_entry set embedding = %s::vector, embedding_model = %s, embedded_at = now() where id = %s",
                     (literal(vector), self.embedder.model, entry_id), fetch=False)
        return len(rows)

    def query_vector(self, query: str) -> list[float] | None:
        if self.embedder is None or not query.strip():
            return None
        found = self.embedder.embed([query], timeout=QUERY_TIMEOUT)
        return found[0] if found else None

    @staticmethod
    def fuse(first: list[Any], second: list[Any], key: Callable[[Any], Any], limit: int) -> list[Any]:
        """Reciprocal rank fusion of two result lists (the words list and the meaning list): an entry both lists like comes first."""
        score: dict[Any, float] = {}
        items: dict[Any, Any] = {}
        for ranking in (first, second):
            for position, item in enumerate(ranking):
                score[key(item)] = score.get(key(item), 0.0) + 1.0 / (60 + position)
                items.setdefault(key(item), item)
        return [items[k] for k in sorted(score, key=lambda k: -score[k])][:limit]

    # ---- read ----------------------------------------------------------------------------------------------------
    def get(self, entry_id: str, namespaces: list[str] | None = None) -> dict[str, Any]:
        rows = self.run(f"select {', '.join(COLUMNS)}, decay_reason from memory_entry where id = %s", (entry_id,))
        if not rows:
            raise NotFound(entry_id)
        item = public(rows[0], COLUMNS + ("decay_reason",))
        if item["namespace"] and namespaces is not None and "*" not in namespaces and item["namespace"] not in namespaces:
            raise NotFound(entry_id)  # another company's entry is as good as absent
        return item

    def search(self, query: str, namespace: str = "", project: str = "", repo: str = "", kinds: list[str] | None = None,
               statuses: list[str] | None = None, limit: int = 8, touch: bool = False, body_chars: int = 600) -> list[dict[str, Any]]:
        """Entries of the global scope plus those of `namespace`, best first. Without a query: the newest entries of the scope."""
        kinds = [k for k in (kinds or guard.KINDS) if k in guard.KINDS]
        statuses = [s for s in (statuses or VISIBLE) if s in guard.STATUSES]
        limit = max(1, min(int(limit), 50))
        words = guard.tsquery(query)
        scope_sql = ("status = any(%s) and kind = any(%s) and (expires_at is null or expires_at > now()) "
                     "and (scope = 'global' or namespace = %s) and (project = '' or project = %s) and (repo = '' or repo = %s or %s = '')")
        scope_args = (statuses, kinds, namespace, project, repo, repo)
        if words:
            sql = (f"select {', '.join(COLUMNS)}, rank, sim from ("
                   f"select {', '.join(COLUMNS)}, ts_rank_cd(tsv, q) as rank, similarity(title, %s) as sim "
                   f"from memory_entry, to_tsquery('simple', mem_unaccent(%s)) q where {scope_sql} and (tsv @@ q or title %% %s)) hit "
                   "order by (rank * 2 + sim + case when project = %s and project <> '' then 0.5 else 0 end + (helpful - unhelpful) * 0.05 "
                   "+ case status when 'promoted' then 0.3 when 'verified' then 0.15 else 0 end) desc, created_at desc limit %s")
            rows = self.run(sql, (query[:300], words, *scope_args, query[:300], project, limit))
            names = COLUMNS + ("rank", "sim")
        else:
            sql = (f"select {', '.join(COLUMNS)} from memory_entry where {scope_sql} order by "
                   "(case status when 'promoted' then 2 when 'verified' then 1 else 0 end) desc, created_at desc limit %s")
            rows = self.run(sql, (*scope_args, limit))
            names = COLUMNS
        items = [public(row, names) for row in rows]
        vector = self.query_vector(query) if words else None
        if vector is not None:
            by_meaning = self.run(f"select {', '.join(COLUMNS)} from memory_entry where {scope_sql} and embedding is not null and embedding_model = %s "
                                  "and 1 - (embedding <=> %s::vector) >= %s order by embedding <=> %s::vector limit %s",
                                  (*scope_args, self.embedder.model, literal(vector), self.embedder.min_similarity, literal(vector), limit))
            items = self.fuse(items, [public(row, COLUMNS) for row in by_meaning], lambda item: item["id"], limit)
        for item in items:
            item["body"] = item["body"][:body_chars]
        if touch and items:
            self.run("update memory_entry set use_count = use_count + 1, last_used_at = now() where id = any(%s::uuid[])", ([i["id"] for i in items],), fetch=False)
        return items

    def stats(self, namespaces: list[str] | None = None) -> dict[str, Any]:
        where = "" if namespaces is None else " where namespace = '' or namespace = any(%s)"
        args = () if namespaces is None else (namespaces,)
        by = self.run(f"select status, kind, count(*) from memory_entry{where} group by status, kind order by status, kind", args)
        event_sql = "select count(*) from memory_event" if namespaces is None else (
            "select count(*) from memory_event e join memory_entry m on m.id = e.entry_id where m.namespace = '' or m.namespace = any(%s)")
        events = self.run(event_sql, () if namespaces is None else args)
        return {"entries": [{"status": s, "kind": k, "count": n} for s, k, n in by], "events": events[0][0]}

    # ---- the curator ---------------------------------------------------------------------------------------------
    def curate(self, actor: str = "curator") -> dict[str, int]:
        """Plain rules, no AI: confirmed or helpful fresh entries become verified, proven verified ones promoted, doubtful or stale ones decay."""
        done = {}
        done["verified"] = self._ids("update memory_entry set status = 'verified', verified_at = now(), updated_at = now() where status = 'fresh' "
                                     "and (confirmations >= 2 or helpful >= 2) and unhelpful = 0 returning id", actor, "verified")
        done["promoted"] = self._ids("update memory_entry set status = 'promoted', updated_at = now() where status = 'verified' and helpful >= 3 "
                                     "and helpful >= 3 * unhelpful returning id", actor, "promoted")
        done["decayed_unhelpful"] = self._ids("update memory_entry set status = 'decayed', decayed_at = now(), decay_reason = 'unhelpful', updated_at = now() "
                                              "where status in ('fresh', 'verified') and unhelpful >= 3 and unhelpful > helpful returning id", actor, "decayed")
        done["decayed_expired"] = self._ids("update memory_entry set status = 'decayed', decayed_at = now(), decay_reason = 'expired', updated_at = now() "
                                            "where status <> 'decayed' and expires_at is not null and expires_at < now() returning id", actor, "decayed")
        done["decayed_stale_fresh"] = self._ids("update memory_entry set status = 'decayed', decayed_at = now(), decay_reason = 'never confirmed in 30 days', "
                                                "updated_at = now() where status = 'fresh' and created_at < now() - interval '30 days' and helpful = 0 returning id",
                                                actor, "decayed")
        return done

    def _ids(self, sql: str, actor: str, action: str) -> int:
        rows = self.run(sql)
        for (entry_id,) in rows:
            self.event(actor, action, str(entry_id), {"by": "curator"})
        return len(rows)
