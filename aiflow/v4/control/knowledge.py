"""Versioned resources and durable scoped knowledge, context packs and study cases."""
from __future__ import annotations

import re
import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

KINDS = ("study_case", "knowledge", "skill", "agent", "guide", "policy")
FOLDERS = {"study_case": "study-cases", "knowledge": "knowledge", "skill": "skills",
           "agent": "agents", "guide": "guides"}
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,78}[a-z0-9])?$")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d ().-]{8,}\d)(?!\w)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret)"
    r"\b\s*[:=]\s*['\"]?[^\s,'\";]{6,}"
)
_TOKEN_SHAPES = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SENSITIVE_DECLARATION = re.compile(r"(?i)\b(?:personal data|personally identifiable|social security number|ssn)\s*[:=]")
_log = logging.getLogger("aiflow4.knowledge")


def sensitive_content(title: str, content: str) -> bool:
    """Reject common credentials and directly identifying contact data before persistent storage."""
    text = f"{title}\n{content}"
    return bool(_EMAIL.search(text) or _PHONE.search(text) or _SECRET_ASSIGNMENT.search(text)
                or _TOKEN_SHAPES.search(text) or _BEARER.search(text) or _PRIVATE_KEY.search(text)
                or _SSN.search(text) or _SENSITIVE_DECLARATION.search(text))


def _redact_sensitive(text: str) -> str:
    """Remove common PII and credential shapes from automatically captured source text."""
    text = _SECRET_ASSIGNMENT.sub("[redacted credential]", text)
    text = _TOKEN_SHAPES.sub("[redacted token]", text)
    text = _BEARER.sub("[redacted bearer token]", text)
    text = _PRIVATE_KEY.sub("[redacted private key]", text)
    text = _EMAIL.sub("[redacted email]", text)
    text = _PHONE.sub("[redacted phone]", text)
    text = _SSN.sub("[redacted personal identifier]", text)
    text = _SENSITIVE_DECLARATION.sub("[redacted personal data]", text)
    return text


def _scope(project: str, epic: str, scope: str) -> tuple[str, str, str]:
    project, epic, scope = project.strip().upper(), epic.strip().upper(), scope.strip()
    if scope not in {"project", "general"}:
        raise Invalid("scope must be project or general")
    if scope == "general":
        if project or epic:
            raise Invalid("general knowledge cannot name a project or epic")
    elif not PROJECT_RE.fullmatch(project):
        raise Invalid("project knowledge requires an uppercase project key")
    if epic and (scope != "project" or not PROJECT_RE.fullmatch(epic.split("-", 1)[0])
                 or not epic.startswith(project + "-")):
        raise Invalid("epic scope requires a project epic key")
    return project, epic, scope


def _signature(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _ticket_epic(db, ticket: str, parent: str = "") -> str:
    """Resolve a mirrored ticket's epic ancestor, including nested parent links."""
    current = parent
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        row = db.one("SELECT type,parent_key FROM tickets WHERE key=%s", (current,))
        if not row:
            break
        if row["type"] == "Epic":
            return current
        current = row.get("parent_key") or ""
    return ""


def _embedding(text: str) -> str | None:
    try:
        from .memory import embed_texts, _vector_literal
        vectors = embed_texts([text[:20_000]])
        return _vector_literal(vectors[0]) if vectors else None
    except Exception as exc:  # noqa: BLE001 - lexical knowledge remains usable without the local model
        _log.info("knowledge embedding unavailable: %s", type(exc).__name__)
        return None


def save_item(db, *, project: str = "", epic: str = "", scope: str = "project", kind: str,
              title: str, content: str, actor: str = "AI Flow", source_type: str = "manual",
              source_ref: str = "", source_url: str = "", ticket_key: str = "", run_id: str = "",
              pr_url: str = "", approved: bool = False, approved_by: str = "",
              status: str = "active", mistake_signature: str = "", repeated_mistake: bool = False) -> dict:
    """Save a filtered, project-scoped knowledge item; automatic records are idempotent by source."""
    if kind not in {"study_case", "decision", "convention", "skill", "guide"}:
        raise Invalid("unknown knowledge kind")
    project, epic, scope = _scope(project, epic, scope)
    title, content, actor = title.strip(), content.strip(), actor.strip() or "AI Flow"
    if not title or len(title) > 200 or not content or len(content) > 30_000:
        raise Invalid("title and content are required; content is limited to 30000 characters")
    if sensitive_content(title, content):
        raise Invalid("knowledge containing credentials or personal contact data cannot be saved")
    if status not in {"active", "stale", "retired"}:
        raise Invalid("unknown knowledge status")
    embedding = _embedding(title + "\n" + content)
    source_ref = source_ref[:500]
    item = db.one(
        "INSERT INTO knowledge_items (project,scope,epic,kind,title,content,created_by,source_type,source_ref,source_url,"
        "ticket_key,run_id,pr_url,approved,approved_by,status,mistake_signature,repeated_mistake,embedding) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
        "ON CONFLICT (source_type,source_ref,kind) WHERE source_ref <> '' DO NOTHING RETURNING *",
        (project, scope, epic, kind, title, content, actor[:200], source_type, source_ref, source_url[:2000],
         ticket_key[:100], run_id[:100], pr_url[:2000], approved, approved_by[:200], status,
         mistake_signature, repeated_mistake, embedding))
    if item:
        # New conflicting decisions make the prior same-title version due for review.
        if kind == "decision" and source_type == "manual":
            db.run("UPDATE knowledge_items SET status='stale',updated_at=now() WHERE id<>%s AND kind='decision' "
                   "AND project=%s AND scope=%s AND epic=%s AND lower(title)=lower(%s) AND status='active'",
                   (item["id"], project, scope, epic, title))
        return item
    if source_ref:
        old = db.one("SELECT * FROM knowledge_items WHERE source_type=%s AND source_ref=%s AND kind=%s",
                     (source_type, source_ref, kind))
        return old or {}
    return {}


def capture_run(db, run_id: str, jira_site: str = "", actor: str = "AI Flow") -> dict | None:
    """Write one durable decision record for every completed run, plus a case for time-costing outcomes."""
    run = db.one("SELECT r.*,t.summary AS ticket_summary,t.description AS ticket_description,t.parent_key "
                 "FROM runs r LEFT JOIN tickets t ON t.key=r.ticket_key WHERE r.id=%s AND r.status='finished'", (run_id,))
    if not run:
        return None
    details = run.get("details") or {}
    files = details.get("changed_files") or details.get("files") or []
    if not isinstance(files, list):
        files = [str(files)] if files else []
    decisions = details.get("decisions") or details.get("decision") or ""
    why = details.get("why") or details.get("rationale") or details.get("decision_reason") or ""
    lines = [f"What was done: {run['summary'] or 'The run finished without a summary.'}",
             f"Why: {why or run.get('ticket_description') or 'Reason not recorded by the run or ticket.'}",
             f"Decisions: {decisions or 'No explicit decision was recorded; the run outcome was ' + str(run['outcome'] or 'finished') + '.'}",
             "Files and areas touched: " + (", ".join(str(f)[:300] for f in files[:100]) or "not reported"),
             f"Outcome: {run['step']} / {run['outcome'] or 'finished'}"]
    if run.get("error_kind"):
        lines.append(f"Failure: {run['error_kind']}: {run['summary']}")
    if run.get("ticket_summary"):
        lines.insert(0, f"Ticket: {run['ticket_key']} — {run['ticket_summary']}")
    jira_url = f"{jira_site.rstrip('/')}/browse/{run['ticket_key']}" if jira_site else ""
    scope = "project" if run["project"] else "general"
    item = save_item(db, project=run["project"], scope=scope,
                     epic=_ticket_epic(db, run["ticket_key"], run.get("parent_key") or ""), kind="decision",
                     title=f"{run['ticket_key']} {run['step']} run", content=_redact_sensitive("\n".join(lines)), actor=actor,
                     source_type="run", source_ref=str(run["id"]), source_url=jira_url,
                     ticket_key=run["ticket_key"], run_id=str(run["id"]), pr_url=run.get("pr_url") or "",
                     approved=True, approved_by="AI Flow automatic capture")
    if run["outcome"] in {"failed", "timeout", "rework", "blocked", "escalated"}:
        cause = (details.get("findings") or details.get("cause") or run.get("error_kind") or run["summary"] or
                 f"{run['step']} ended as {run['outcome']}")
        fix = details.get("fix") or details.get("remediation") or (
            "Apply the reported findings and rerun verification." if run["outcome"] == "rework" else
            "Resolve the recorded cause before retrying; ask the ticket owner when the run is blocked.")
        prevention = details.get("avoid_next_time") or details.get("prevention") or (
            "Review this case before repeating the affected verification or implementation step.")
        write_study_case(db, project=run["project"], epic=_ticket_epic(db, run["ticket_key"], run.get("parent_key") or ""),
                         title=f"{run['ticket_key']} {run['step']} {run['outcome']}",
                         situation=f"{run['ticket_summary'] or run['ticket_key']}: {run['summary']}",
                         cause=str(cause), fix=str(fix), prevention=str(prevention), source_type="run",
                         source_ref=str(run["id"]), source_url=jira_url, ticket_key=run["ticket_key"],
                         run_id=str(run["id"]), pr_url=run.get("pr_url") or "", scope=scope)
    return item


def capture_reviewer_run(db, run_id: str, jira_site: str = "", *, ticket_key: str, outcome: str) -> dict | None:
    """Capture a reviewer run and backfill its ticket only after a successful merge."""
    item = capture_run(db, run_id, jira_site)
    if outcome in {"merged", "skipped_gate"}:
        capture_closed_tickets(db, jira_site, ticket_key=ticket_key)
    return item


def write_study_case(db, *, project: str, title: str, situation: str, cause: str, fix: str,
                     prevention: str, epic: str = "", source_type: str = "event", source_ref: str = "",
                     source_url: str = "", ticket_key: str = "", run_id: str = "", pr_url: str = "",
                     scope: str = "project") -> dict | None:
    """Capture a failure/rework/question/incident with cause, resolution and prevention."""
    cause = cause.strip()[:5000]
    signature = _signature(cause)
    repeated = False
    if signature:
        repeated = bool(db.one(
            "SELECT 1 FROM knowledge_items k WHERE k.kind='study_case' AND k.mistake_signature=%s "
            "AND k.status<>'retired' AND (%s='' OR k.run_id<>%s) AND ("
            "(k.scope='project' AND k.project=%s) OR (k.scope='general' AND %s='') OR EXISTS ("
            "SELECT 1 FROM knowledge_deliveries d WHERE d.item_id=k.id AND d.project=%s "
            "AND (%s='' OR d.run_id::text<>%s))) LIMIT 1",
            (signature, run_id, run_id, project, project, project, run_id, run_id)))
    body = _redact_sensitive(f"Situation: {situation.strip()[:5000]}\nCause: {cause}\nFix: {fix.strip()[:5000]}\n"
                             f"Avoid next time: {prevention.strip()[:5000]}")
    try:
        return save_item(db, project=project, epic=epic, scope=scope, kind="study_case", title=_redact_sensitive(title[:200]), content=body,
                         actor="AI Flow", source_type=source_type, source_ref=source_ref, source_url=source_url,
                         ticket_key=ticket_key, run_id=run_id, pr_url=pr_url, approved=True,
                         approved_by="AI Flow automatic capture", mistake_signature=signature,
                         status="active", repeated_mistake=repeated) | {"repeated_mistake": repeated}
    except Invalid as exc:
        if "cannot be saved" in str(exc):
            _log.info("study case rejected by sensitive-content filter (%s)", source_type)
            return None
        raise


def capture_closed_tickets(db, jira_site: str = "", *, ticket_key: str = "") -> int:
    """Backfill study cases for closed tickets, optionally limiting the work to one newly closed ticket."""
    query = ("SELECT t.key,t.project,t.summary,t.description,t.parent_key FROM tickets t WHERE t.status='Done' "
             "AND NOT EXISTS (SELECT 1 FROM knowledge_items k WHERE k.kind='study_case' "
             "AND k.source_type='ticket' AND k.source_ref=t.key) "
             "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')")
    params = ()
    if ticket_key:
        query += " AND t.key=%s"
        params = (ticket_key,)
    rows = db.all(query, params)
    count = 0
    for ticket in rows:
        runs = db.all("SELECT id,step,outcome,summary,details,pr_url FROM runs WHERE ticket_key=%s "
                      "AND status='finished' ORDER BY started_at", (ticket["key"],))
        outcomes = [f"{r['step']} {r['outcome']}: {r['summary'][:500]}" for r in runs[-20:]]
        files = sorted({str(path) for r in runs[-20:]
                        for path in ((r.get("details") or {}).get("changed_files") or [])
                        if isinstance((r.get("details") or {}).get("changed_files") or [], list)})
        decisions = [str((r.get("details") or {}).get("decisions") or (r.get("details") or {}).get("decision"))[:250]
                     for r in runs[-20:] if (r.get("details") or {}).get("decisions") or (r.get("details") or {}).get("decision")]
        body = (f"Ticket: {ticket['key']} — {ticket['summary']}\nWhat was done: " +
                ("; ".join(outcomes) or "Ticket closed; no finished run summary was stored.") +
                f"\nWhy: {ticket['description'][:3000] or 'No ticket rationale was stored.'}" +
                f"\nDecisions: {'; '.join(decisions) or 'No explicit decisions were stored.'}" +
                f"\nFiles and areas touched: {', '.join(path[:120] for path in files[:40]) or 'not reported'}" +
                "\nFix: Completed ticket changes were accepted and the ticket reached Done." +
                "\nAvoid next time: Follow the recorded decisions and checks when similar work returns.")
        url = f"{jira_site.rstrip('/')}/browse/{ticket['key']}" if jira_site else ""
        try:
            item = save_item(db, project=ticket["project"], epic=_ticket_epic(db, ticket["key"], ticket.get("parent_key") or ""), kind="study_case",
                             title=_redact_sensitive(f"Completed ticket {ticket['key']}: {ticket['summary'][:150]}"), content=_redact_sensitive(body),
                             source_type="ticket", source_ref=ticket["key"], source_url=url,
                             ticket_key=ticket["key"], run_id=str(runs[-1]["id"]) if runs else "",
                             pr_url=next((r["pr_url"] for r in reversed(runs) if r["pr_url"]), ""),
                             approved=True, approved_by="AI Flow automatic capture")
            count += bool(item)
        except Invalid as exc:
            if "cannot be saved" not in str(exc):
                raise
    return int(count)


def resolve_incident_case(db, *, item_id: int, cause: str, resolution: str, project: str = "",
                          ticket_key: str = "", run_id: str = "") -> None:
    row = db.one("SELECT title,content FROM knowledge_items WHERE id=%s AND status<>'retired'", (item_id,))
    if not row:
        return
    content = _redact_sensitive(f"Situation: {row['title']}\nCause: {cause}\nFix: {resolution}\n"
                                "Avoid next time: Keep the incident trigger and recovery check in the operator runbook.")
    if sensitive_content(row["title"], content):
        return
    vector = _embedding(row["title"] + "\n" + content)
    db.run("UPDATE knowledge_items SET content=%s,embedding=%s::vector,updated_at=now() WHERE id=%s",
           (content, vector, item_id))


def _pack_item(row: dict) -> dict:
    return {"id": int(row["id"]), "kind": row["kind"], "title": row["title"], "content": row["content"],
            "project": row["project"], "scope": row["scope"], "epic": row["epic"], "status": row["status"],
            "source_type": row["source_type"], "source_ref": row["source_ref"], "source_url": row["source_url"],
            "ticket_key": row["ticket_key"], "run_id": row["run_id"], "pr_url": row["pr_url"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "approved": row["approved"], "approved_by": row["approved_by"],
            "handed_count": row["handed_count"], "last_handed_at": row["last_handed_at"],
            "repeated_mistake": row["repeated_mistake"]}


def _visible_sql() -> str:
    return "(scope='general' OR (scope='project' AND project=%s AND (epic='' OR epic=%s)))"


def flag_stale(db) -> int:
    """Mark old guidance and decisions superseded by same-title decisions for administrator review."""
    return db.run("UPDATE knowledge_items SET status='stale',updated_at=now() WHERE status='active' "
                  "AND kind IN ('convention','skill','guide','decision') "
                  "AND updated_at < now() - interval '180 days'")


def context_pack(db, *, project: str, ticket: str, epic: str = "", query: str = "", run_id: str = "") -> dict:
    """Build an isolated pack from approved stored knowledge and track exactly what a run received."""
    flag_stale(db)
    project = project.strip().upper()
    epic = epic.strip().upper()
    rows = db.all("SELECT * FROM knowledge_items WHERE " + _visible_sql() +
                  " AND approved=true AND status IN ('active','stale') ORDER BY created_at DESC,id DESC LIMIT 500",
                  (project, epic))
    if not query.strip():
        query = ticket
    allowed = {int(r["id"]): r for r in rows}
    # Search adds semantic study cases; scope is enforced again when candidates are selected.
    cases = (search_items(db, project=project, q=query, limit=8, include_retired=False, approved_only=True,
                          epic=epic, kinds=("study_case",))
             if any(r["kind"] == "study_case" for r in rows) else [])
    selected: list[dict] = []

    def add(row: dict | None) -> None:
        if row and int(row["id"]) in allowed and all(int(existing["id"]) != int(row["id"]) for existing in selected):
            selected.append(allowed[int(row["id"])])

    conventions = [r for r in rows if r["kind"] == "convention"]
    guides = [r for r in rows if r["kind"] in {"guide", "skill"}]
    decisions = [r for r in rows if r["kind"] == "decision"]
    ticket_history = [r for r in rows if r["ticket_key"] == ticket]
    epic_history = [r for r in rows if epic and r["epic"] == epic and r["ticket_key"] != ticket]
    for group in (conventions[:4], guides[:4], decisions[:5], ticket_history[:6], epic_history[:6]):
        for row in group:
            add(row)
    for hit in cases:
        add(allowed.get(int(hit["id"])))
    items = [_pack_item(row) for row in selected]
    if run_id:
        db.run("INSERT INTO knowledge_context_packs (run_id,project,ticket_key,epic,item_count) VALUES (%s,%s,%s,%s,%s) "
               "ON CONFLICT (run_id) DO UPDATE SET item_count=EXCLUDED.item_count",
               (run_id, project, ticket, epic, len(items)))
        for row in selected:
            inserted = db.run("INSERT INTO knowledge_deliveries (run_id,item_id,project,ticket_key) VALUES (%s,%s,%s,%s) "
                              "ON CONFLICT (run_id,item_id) DO NOTHING", (run_id, row["id"], project, ticket))
            if inserted:
                db.run("UPDATE knowledge_items SET handed_count=handed_count+1,last_handed_at=now() WHERE id=%s",
                       (row["id"],))
    return {"project": project, "ticket": ticket, "epic": epic, "overview": [i for i in items if i["kind"] == "convention"],
            "working_rules": [i for i in items if i["kind"] in {"convention", "guide", "skill"}],
            "latest_decisions": [i for i in items if i["kind"] == "decision"],
            "ticket_history": [i for i in items if i["ticket_key"] == ticket],
            "epic_history": [i for i in items if epic and i["epic"] == epic and i["ticket_key"] != ticket],
            "study_cases": [i for i in items if i["kind"] == "study_case"], "items": items}


def search_items(db, *, project: str = "", q: str = "", limit: int = 20, include_retired: bool = False,
                 approved_only: bool = False, epic: str = "", kinds: tuple[str, ...] = ()) -> list[dict]:
    """Hybrid full-text and local-vector search restricted to the selected project and shared general items."""
    limit = max(1, min(int(limit), 100))
    project, epic = project.strip().upper(), epic.strip().upper()
    base = "scope='general'" if not project else "(scope='general' OR (scope='project' AND project=%s AND (epic='' OR epic=%s)))"
    params: list[Any] = []
    if project:
        params.extend((project, epic))
    filters = [base]
    if not include_retired:
        filters.append("status<>'retired'")
    if approved_only:
        filters.append("approved=true")
    if kinds:
        filters.append("kind=ANY(%s)")
        params.append(list(kinds))
    where = " AND ".join(filters)
    if not q.strip():
        return db.all("SELECT * FROM knowledge_items WHERE " + where + " ORDER BY created_at DESC,id DESC LIMIT %s",
                      (*params, limit))
    vector = _embedding(q)
    if vector:
        sql = ("WITH scoped AS (SELECT k.*,ts_rank(k.tsv,websearch_to_tsquery('english',%s)) AS fts, "
               "k.embedding <=> %s::vector AS distance FROM knowledge_items k WHERE " + where + "), "
               "ranked AS (SELECT *, CASE WHEN fts>0 THEN row_number() OVER (ORDER BY fts DESC,created_at DESC,id DESC) END AS fr, "
               "CASE WHEN distance IS NOT NULL THEN row_number() OVER (ORDER BY distance,created_at DESC,id DESC) END AS vr "
               "FROM scoped), scored AS (SELECT *,coalesce(1.0/(60+fr),0)+coalesce(1.0/(60+vr),0) AS score FROM ranked) "
               "SELECT * FROM scored ORDER BY score DESC,created_at DESC,id DESC LIMIT %s")
        # Both window ranks include rows outside the matching FTS/vector set; score still fuses each available signal.
        return db.all(sql, (q, vector, *params, limit))
    return db.all("SELECT *,ts_rank(tsv,websearch_to_tsquery('english',%s)) AS score FROM knowledge_items WHERE " +
                  where + " AND tsv @@ websearch_to_tsquery('english',%s) ORDER BY score DESC,created_at DESC,id DESC LIMIT %s",
                  (q, *params, q, limit))


def dashboard(db, project: str = "", q: str = "", include_retired: bool = True) -> dict:
    flag_stale(db)
    project = project.strip().upper()
    visible_scope = "scope='general'" if not project else "(scope='general' OR (scope='project' AND project=%s))"
    scope_params = () if not project else (project,)
    counts = db.all("SELECT CASE WHEN scope='general' THEN 'general' ELSE project END AS project,kind,count(*)::int AS count "
                    "FROM knowledge_items WHERE " + visible_scope + " AND status<>'retired' GROUP BY 1,2 ORDER BY 1,2",
                    scope_params)
    newest = db.all("SELECT * FROM knowledge_items WHERE " + visible_scope +
                    " AND status<>'retired' ORDER BY created_at DESC,id DESC LIMIT 8", scope_params)
    most_used = db.all("SELECT * FROM knowledge_items WHERE " + visible_scope +
                       " AND status<>'retired' ORDER BY handed_count DESC,created_at DESC LIMIT 8", scope_params)
    deliveries = db.one("SELECT count(*)::int AS count FROM knowledge_context_packs WHERE project=%s OR project=''",
                        (project,)) or {"count": 0}
    repeated = db.one("SELECT count(*)::int AS count FROM knowledge_items WHERE repeated_mistake=true AND " + visible_scope,
                      scope_params) or {"count": 0}
    rework = db.all("SELECT date_trunc('week',started_at)::date::text AS week,count(*)::int AS rounds,"
                    "count(DISTINCT ticket_key)::int AS tickets,"
                    "round(count(*)::numeric / NULLIF(count(DISTINCT ticket_key),0),2) AS rounds_per_ticket "
                    "FROM runs WHERE (project=%s OR project='') AND step='verify' AND outcome='rework' "
                    "GROUP BY 1 ORDER BY 1 DESC LIMIT 12", (project,))
    visible = search_items(db, project=project, q=q, limit=100, include_retired=include_retired)
    return {"counts": counts, "newest": [_pack_item(r) for r in newest],
            "most_used": [_pack_item(r) for r in most_used], "items": [_pack_item(r) for r in visible],
            "effectiveness": {"runs_with_context_pack": deliveries["count"],
                              "known_mistakes_repeated": repeated["count"], "rework_by_week": rework}}


def moderate_item(db, item_id: int, *, action: str, actor: str = "", content: str = "", title: str = "",
                  duplicate_id: int | None = None, project: str = "", scope: str | None = None,
                  epic: str = "") -> dict:
    row = db.one("SELECT * FROM knowledge_items WHERE id=%s", (item_id,))
    if not row:
        raise Invalid("knowledge item not found")
    actor = actor.strip()[:200] or "administrator"
    if action == "approve":
        db.run("UPDATE knowledge_items SET approved=true,approved_by=%s,status='active',updated_at=now() WHERE id=%s",
               (actor, item_id))
    elif action == "retire":
        db.run("UPDATE knowledge_items SET status='retired',updated_at=now() WHERE id=%s", (item_id,))
    elif action == "stale":
        db.run("UPDATE knowledge_items SET status='stale',updated_at=now() WHERE id=%s", (item_id,))
    elif action == "correct":
        target_scope = scope or row["scope"]
        target_project = project if scope else row["project"]
        target_epic = epic if (scope or epic) else row["epic"]
        revised = save_item(db, project=target_project, epic=target_epic, scope=target_scope, kind=row["kind"],
                            title=title or row["title"], content=content, actor=actor, source_type="manual_revision",
                            source_ref=f"{item_id}:{row['updated_at'].isoformat()}", source_url=row["source_url"],
                            ticket_key=row["ticket_key"], run_id=row["run_id"], pr_url=row["pr_url"], approved=True,
                            approved_by=actor)
        db.run("UPDATE knowledge_items SET status='retired',updated_at=now() WHERE id=%s", (item_id,))
        return revised
    elif action == "merge":
        other = db.one("SELECT * FROM knowledge_items WHERE id=%s", (duplicate_id,)) if duplicate_id else None
        if not other or int(other["id"]) == item_id:
            raise Invalid("choose a different duplicate item")
        if (row["project"], row["scope"], row["epic"], row["kind"]) != \
                (other["project"], other["scope"], other["epic"], other["kind"]):
            raise Invalid("duplicates must have the same project, epic and kind")
        combined = (row["content"] + "\n\nMerged note: " + other["content"])[:30_000]
        if sensitive_content(row["title"], combined):
            raise Invalid("knowledge containing credentials or personal contact data cannot be saved")
        embedding = _embedding(row["title"] + "\n" + combined)
        db.run("UPDATE knowledge_items SET content=%s,embedding=%s::vector,handed_count=handed_count+%s, "
               "updated_at=now() WHERE id=%s", (combined, embedding, other["handed_count"], item_id))
        db.run("UPDATE knowledge_items SET status='retired',updated_at=now() WHERE id=%s", (other["id"],))
    else:
        raise Invalid("unknown knowledge action")
    return db.one("SELECT * FROM knowledge_items WHERE id=%s", (item_id,)) or {}


class Invalid(ValueError):
    """A resource has an invalid type, identity or body."""


def validate(kind: str, resource_id: str, title: str, content: str, actor: str) -> tuple[str, str, str, str, str]:
    kind = kind.strip()
    resource_id = resource_id.strip()
    title, actor = title.strip(), actor.strip()
    if kind not in KINDS:
        raise Invalid("unknown resource type")
    if kind == "policy":
        if resource_id != "default" and not PROJECT_RE.fullmatch(resource_id):
            raise Invalid("policy id must be 'default' or an uppercase project key")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise Invalid("policy content must be valid YAML") from e
        if not isinstance(parsed, dict):
            raise Invalid("policy content must be a YAML mapping")
    elif not ID_RE.fullmatch(resource_id):
        raise Invalid("id must be a lowercase slug")
    if not title or len(title) > 200:
        raise Invalid("title is required and must be at most 200 characters")
    if not content.strip() or len(content) > 100_000:
        raise Invalid("content is required and must be at most 100000 characters")
    if sensitive_content(title, content):
        raise Invalid("knowledge containing credentials or personal contact data cannot be saved")
    if not actor or len(actor) > 200:
        raise Invalid("editor name is required and must be at most 200 characters")
    return kind, resource_id, title, content, actor


def path_for(kind: str, resource_id: str) -> str:
    if kind == "policy":
        return "policy/default.yaml" if resource_id == "default" else f"policy/projects/{resource_id}.yaml"
    if kind == "skill":
        return f"skills/{resource_id}/SKILL.md"
    return f"{FOLDERS[kind]}/{resource_id}.md"


def _frontmatter(title: str, resource_id: str, archived: bool, kind: str) -> str:
    meta: dict[str, Any] = {"id": resource_id, "title": title, "archived": archived}
    if kind == "skill":
        meta = {"name": resource_id, "description": title, "archived": archived}
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def file_content(kind: str, resource_id: str, title: str, content: str, archived: bool) -> str:
    if kind == "policy":
        header = f"# aiflow4-title: {title.replace(chr(10), ' ')[:200]}\n"
        if archived:
            header += "# aiflow4-archived: true\n"
        return header + content.rstrip() + "\n"
    return _frontmatter(title, resource_id, archived, kind) + content.rstrip() + "\n"


def _parse_markdown(raw: str, resource_id: str, kind: str) -> tuple[str, str, bool]:
    title = resource_id.replace("-", " ").replace("_", " ").title()
    archived = False
    content = raw
    if raw.startswith("---\n"):
        _marker, rest = raw.split("\n---\n", 1) if "\n---\n" in raw else ("", raw)
        if rest != raw:
            try:
                meta = yaml.safe_load(_marker[4:]) or {}
            except yaml.YAMLError:
                meta = {}
            title = str(meta.get("title") or meta.get("description") or title)
            archived = bool(meta.get("archived", False))
            content = rest
    return title, content.lstrip("\n").rstrip(), archived


def import_local(db, root: str | Path) -> None:
    """Index existing managed files once so the UI starts with the checked-out control repo contents."""
    root = Path(root)
    files: list[tuple[str, str, str]] = []
    default = root / "policy/default.yaml"
    if default.is_file():
        files.append(("policy", "default", str(default.relative_to(root))))
    for p in sorted((root / "policy/projects").glob("*.yaml")) if (root / "policy/projects").is_dir() else []:
        files.append(("policy", p.stem, str(p.relative_to(root))))
    patterns = (("study_case", "study-cases/*.md"), ("knowledge", "knowledge/*.md"),
                ("skill", "skills/*/SKILL.md"), ("agent", "agents/*.md"), ("guide", "guides/*.md"))
    for kind, pattern in patterns:
        for p in sorted(root.glob(pattern)):
            rid = p.parent.name if kind == "skill" else p.stem
            files.append((kind, rid, str(p.relative_to(root))))
    for kind, rid, rel in files:
        if db.one("SELECT 1 FROM managed_resources WHERE resource_type=%s AND resource_id=%s", (kind, rid)):
            continue
        path = root / rel
        raw = path.read_text(encoding="utf-8")
        if kind == "policy":
            archived = "# aiflow4-archived: true" in raw.splitlines()[:3]
            content = "\n".join(line for line in raw.splitlines() if not line.startswith("# aiflow4-"))
            try:
                title_line = next((line for line in raw.splitlines() if line.startswith("# aiflow4-title: ")), "")
                title = title_line.partition(": ")[2] or ("Default policy" if rid == "default" else f"{rid} policy")
            except StopIteration:
                title = "Default policy" if rid == "default" else f"{rid} policy"
            content = content.strip()
        else:
            title, content, archived = _parse_markdown(raw, rid, kind)
        if sensitive_content(title, content):
            _log.warning("managed knowledge import skipped because it contains sensitive content")
            continue
        db.run("INSERT INTO managed_resources (resource_type,resource_id,title,content,archived,path,version,updated_by)"
               " VALUES (%s,%s,%s,%s,%s,%s,1,'aiflow-control') ON CONFLICT DO NOTHING",
               (kind, rid, title, content, archived, rel))
        db.run("INSERT INTO managed_resource_history (resource_type,resource_id,version,title,content,archived,actor,action)"
               " VALUES (%s,%s,1,%s,%s,%s,'aiflow-control','import') ON CONFLICT DO NOTHING",
               (kind, rid, title, content, archived))


def list_resources(db, include_archived: bool = True) -> list[dict]:
    where = "" if include_archived else " WHERE archived=false"
    return db.all("SELECT resource_type AS type,resource_id AS id,title,content,archived,path,version,updated_by,"
                  "updated_at,commit_sha FROM managed_resources" + where + " ORDER BY resource_type,resource_id")


def get_resource(db, kind: str, resource_id: str) -> dict | None:
    return db.one("SELECT resource_type AS type,resource_id AS id,title,content,archived,path,version,updated_by,"
                  "updated_at,commit_sha FROM managed_resources WHERE resource_type=%s AND resource_id=%s",
                  (kind, resource_id))


def history(db, kind: str, resource_id: str) -> list[dict]:
    return db.all("SELECT version,title,content,archived,actor,action,created_at,commit_sha,pr_number,pr_url,approved_by"
                  " FROM managed_resource_history WHERE resource_type=%s AND resource_id=%s ORDER BY version DESC",
                  (kind, resource_id))


def proposals(db, kind: str = "", resource_id: str = "") -> list[dict]:
    filters = " AND resource_type=%s AND resource_id=%s" if kind and resource_id else ""
    params = (kind, resource_id) if filters else ()
    return db.all("SELECT id AS proposal_id,resource_type AS type,resource_id AS id,title,content,archived,actor,action,branch,"
                  "pr_number,pr_url,status,created_at,base_title,base_content,base_archived,base_version "
                  "FROM managed_resource_proposals WHERE status='pending'" + filters + " ORDER BY created_at DESC", params)


def save_current(db, *, kind: str, resource_id: str, title: str, content: str, archived: bool, actor: str,
                 action: str, path: str, commit_sha: str = "", approved_by: str = "") -> dict:
    if sensitive_content(title, content):
        raise Invalid("knowledge containing credentials or personal contact data cannot be saved")
    previous = get_resource(db, kind, resource_id)
    version = int(previous["version"]) + 1 if previous else 1
    db.run("INSERT INTO managed_resources (resource_type,resource_id,title,content,archived,path,version,updated_by,commit_sha)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (resource_type,resource_id) DO UPDATE SET"
           " title=EXCLUDED.title,content=EXCLUDED.content,archived=EXCLUDED.archived,path=EXCLUDED.path,"
           " version=EXCLUDED.version,updated_by=EXCLUDED.updated_by,updated_at=now(),commit_sha=EXCLUDED.commit_sha",
           (kind, resource_id, title, content, archived, path, version, actor, commit_sha))
    db.run("INSERT INTO managed_resource_history (resource_type,resource_id,version,title,content,archived,actor,action,"
           "commit_sha,approved_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (kind, resource_id, version, title, content, archived, actor, action, commit_sha, approved_by))
    return get_resource(db, kind, resource_id) or {}
