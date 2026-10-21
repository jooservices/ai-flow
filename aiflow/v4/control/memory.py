"""Memory: full-text search over past tickets, finished run summaries and verify findings.

index()    upserts the memory table from tickets and runs (idempotent by kind and ref; a row is rewritten only
           when its text changed). control-loops calls it every minute.
search()   ranked hits for a user query (websearch_to_tsquery, ts_rank) with a snippet (ts_headline).
related()  hits that share any word with a text (OR of its lexemes), used for the claim of a dev or verify run.
Memory is advice: a failure here never stops a claim.
"""
from __future__ import annotations

from .db import DB

MAX_TEXT = 20_000
MAX_LIMIT = 50
HEADLINE = "StartSel=**, StopSel=**, MaxWords=35, MinWords=12, MaxFragments=2, FragmentDelimiter=\" ... \""

UPSERT = (" ON CONFLICT (kind, ref) DO UPDATE SET text=EXCLUDED.text, project=EXCLUDED.project, ticket=EXCLUDED.ticket"
          " WHERE memory.text IS DISTINCT FROM EXCLUDED.text OR memory.project <> EXCLUDED.project")

INDEX_TICKETS = f"""
INSERT INTO memory (project, kind, ref, ticket, text, created_at)
SELECT project, 'ticket', key, key, left(key || ': ' || summary || E'\\n\\n' || description, {MAX_TEXT}),
       coalesce(jira_created, synced_at)
FROM tickets
""" + UPSERT

INDEX_RUN_SUMMARIES = f"""
INSERT INTO memory (project, kind, ref, ticket, text, created_at)
SELECT r.project, 'run_summary', r.id::text, r.ticket_key,
       left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\n' || r.step || ' ' || coalesce(nullif(r.outcome, ''), 'finished')
            || ': ' || r.summary, {MAX_TEXT}),
       coalesce(r.ended_at, r.started_at)
FROM runs r LEFT JOIN tickets t ON t.key = r.ticket_key
WHERE r.status = 'finished' AND r.summary <> ''
""" + UPSERT

INDEX_FINDINGS = f"""
INSERT INTO memory (project, kind, ref, ticket, text, created_at)
SELECT r.project, 'finding', r.id::text, r.ticket_key,
       left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\nverify findings:\\n'
            || coalesce(nullif(r.details->>'findings', ''), r.summary), {MAX_TEXT}),
       coalesce(r.ended_at, r.started_at)
FROM runs r LEFT JOIN tickets t ON t.key = r.ticket_key
WHERE r.step = 'verify' AND r.status = 'finished' AND r.outcome = 'rework'
  AND coalesce(nullif(r.details->>'findings', ''), r.summary) <> ''
""" + UPSERT

HITS = """
SELECT m.kind, m.ref, m.ticket, m.project, m.created_at, ts_rank(m.tsv, q.query) AS rank,
       ts_headline('english', m.text, q.query, %(headline)s) AS snippet
FROM memory m, q
WHERE m.tsv @@ q.query AND (%(project)s = '' OR m.project = %(project)s) AND (%(exclude)s = '' OR m.ticket <> %(exclude)s)
ORDER BY rank DESC, m.created_at DESC, m.id DESC
LIMIT %(limit)s
"""
SEARCH = "WITH q AS (SELECT websearch_to_tsquery('english', %(q)s) AS query)" + HITS
# any shared word: OR of the lexemes of the text (a ticket summary rarely matches another with every word)
RELATED = ("WITH q AS (SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS query"
           " FROM unnest(to_tsvector('english', %(q)s)))") + HITS


def index(db: DB) -> dict[str, int]:
    """Upsert tickets, finished run summaries and verify findings; returns the rows written per kind."""
    return {"ticket": db.run(INDEX_TICKETS), "run_summary": db.run(INDEX_RUN_SUMMARIES),
            "finding": db.run(INDEX_FINDINGS)}


def _hits(db: DB, sql: str, project: str, q: str, limit: int, exclude: str = "") -> list[dict]:
    if not q.strip():
        return []
    limit = max(1, min(int(limit), MAX_LIMIT))
    rows = db.all(sql, {"q": q, "project": project or "", "exclude": exclude or "", "limit": limit,
                          "headline": HEADLINE})
    return [{"kind": r["kind"], "ref": r["ref"], "ticket": r["ticket"], "project": r["project"],
             "rank": round(float(r["rank"]), 6), "snippet": " ".join(r["snippet"].split()),
             "created_at": r["created_at"].isoformat()} for r in rows]


def search(db: DB, project: str, q: str, limit: int = 10) -> list[dict]:
    return _hits(db, SEARCH, project, q, limit)


def related(db: DB, project: str, text: str, exclude_ticket: str = "", limit: int = 3) -> list[dict]:
    """Past entries of the project sharing words with text, excluding the ticket's own entries."""
    return _hits(db, RELATED, project, text, limit, exclude_ticket)
