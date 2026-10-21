"""Memory over past tickets, finished run summaries and verify findings.

The control-loops indexer embeds changed text locally with FastEmbed/ONNX. Search and related hits fuse
PostgreSQL full-text rank with pgvector cosine similarity. Memory is advice: embedding or vector-search failures
fall back to full-text search, and a failure in memory never stops a claim.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .db import DB

log = logging.getLogger("aiflow4.memory")
MAX_TEXT = 20_000
MAX_LIMIT = 50
VECTOR_DIMENSIONS = 384
DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_CACHE_DIR = "~/.cache/fastembed"
HEADLINE = 'StartSel=**, StopSel=**, MaxWords=35, MinWords=12, MaxFragments=2, FragmentDelimiter=" ... "'
RRF_K = 60
CANDIDATE_LIMIT = 100

INDEX_TICKETS = f"""
SELECT project, 'ticket' AS kind, key AS ref, key AS ticket,
       left(key || ': ' || summary || E'\\n\\n' || description, {MAX_TEXT}) AS text,
       coalesce(jira_created, synced_at) AS created_at
FROM tickets
"""

INDEX_RUN_SUMMARIES = f"""
SELECT r.project, 'run_summary' AS kind, r.id::text AS ref, r.ticket_key AS ticket,
       left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\n' || r.step || ' '
            || coalesce(nullif(r.outcome, ''), 'finished') || ': ' || r.summary, {MAX_TEXT}) AS text,
       coalesce(r.ended_at, r.started_at) AS created_at
FROM runs r LEFT JOIN tickets t ON t.key = r.ticket_key
WHERE r.status = 'finished' AND r.summary <> ''
"""

INDEX_FINDINGS = f"""
SELECT r.project, 'finding' AS kind, r.id::text AS ref, r.ticket_key AS ticket,
       left(r.ticket_key || ': ' || coalesce(t.summary, '') || E'\\nverify findings:\\n'
            || coalesce(nullif(r.details->>'findings', ''), r.summary), {MAX_TEXT}) AS text,
       coalesce(r.ended_at, r.started_at) AS created_at
FROM runs r LEFT JOIN tickets t ON t.key = r.ticket_key
WHERE r.step = 'verify' AND r.status = 'finished' AND r.outcome = 'rework'
  AND coalesce(nullif(r.details->>'findings', ''), r.summary) <> ''
"""

UPSERT = """
INSERT INTO memory (project, kind, ref, ticket, text, created_at, embedding)
VALUES (%(project)s, %(kind)s, %(ref)s, %(ticket)s, %(text)s, %(created_at)s, %(embedding)s::vector)
ON CONFLICT (kind, ref) DO UPDATE SET
    project = EXCLUDED.project,
    ticket = EXCLUDED.ticket,
    text = EXCLUDED.text,
    embedding = CASE WHEN memory.text IS DISTINCT FROM EXCLUDED.text THEN EXCLUDED.embedding
                     ELSE coalesce(memory.embedding, EXCLUDED.embedding) END
WHERE memory.text IS DISTINCT FROM EXCLUDED.text
   OR memory.project IS DISTINCT FROM EXCLUDED.project
   OR memory.ticket IS DISTINCT FROM EXCLUDED.ticket
   OR (memory.embedding IS NULL AND EXCLUDED.embedding IS NOT NULL)
"""

FULL_TEXT_HITS = """
SELECT m.kind, m.ref, m.ticket, m.project, m.created_at, ts_rank(m.tsv, q.query) AS rank,
       ts_headline('english', m.text, q.query, %(headline)s) AS snippet
FROM memory m CROSS JOIN q
WHERE m.tsv @@ q.query AND (%(project)s = '' OR m.project = %(project)s)
  AND (%(exclude)s = '' OR m.ticket <> %(exclude)s)
ORDER BY rank DESC, m.created_at DESC, m.id DESC
LIMIT %(limit)s
"""
SEARCH = "WITH q AS (SELECT websearch_to_tsquery('english', %(q)s) AS query)" + FULL_TEXT_HITS
RELATED = ("WITH q AS (SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery AS query"
           " FROM unnest(to_tsvector('english', %(q)s)))") + FULL_TEXT_HITS

HYBRID_TEMPLATE = """
WITH q AS (SELECT {query} AS query),
fts_scored AS (
    SELECT m.id, m.kind, m.ref, m.ticket, m.project, m.text, m.created_at,
           ts_rank(m.tsv, q.query) AS score,
           ts_headline('english', m.text, q.query, %(headline)s) AS snippet
    FROM memory m CROSS JOIN q
    WHERE m.tsv @@ q.query AND (%(project)s = '' OR m.project = %(project)s)
      AND (%(exclude)s = '' OR m.ticket <> %(exclude)s)
),
fts AS (
    SELECT *, row_number() OVER (ORDER BY score DESC, created_at DESC, id DESC) AS position
    FROM fts_scored
    ORDER BY score DESC, created_at DESC, id DESC
    LIMIT %(candidate_limit)s
),
vec AS (
    SELECT m.id, m.kind, m.ref, m.ticket, m.project, m.text, m.created_at,
           row_number() OVER (ORDER BY m.embedding <=> %(embedding)s::vector, m.created_at DESC, m.id DESC) AS position
    FROM memory m
    WHERE m.embedding IS NOT NULL AND (%(project)s = '' OR m.project = %(project)s)
      AND (%(exclude)s = '' OR m.ticket <> %(exclude)s)
    ORDER BY m.embedding <=> %(embedding)s::vector, m.created_at DESC, m.id DESC
    LIMIT %(candidate_limit)s
),
fused AS (
    SELECT coalesce(f.kind, v.kind) AS kind, coalesce(f.ref, v.ref) AS ref,
           coalesce(f.ticket, v.ticket) AS ticket, coalesce(f.project, v.project) AS project,
           coalesce(f.created_at, v.created_at) AS created_at,
           coalesce(1.0 / (%(rrf_k)s + f.position), 0)
               + coalesce(1.0 / (%(rrf_k)s + v.position), 0) AS rank,
           coalesce(f.snippet, ts_headline('english', v.text, q.query, %(headline)s), left(v.text, 250)) AS snippet,
           coalesce(f.id, v.id) AS id
    FROM fts f FULL OUTER JOIN vec v ON f.id = v.id CROSS JOIN q
)
SELECT kind, ref, ticket, project, created_at, rank, snippet
FROM fused
ORDER BY rank DESC, created_at DESC, id DESC
LIMIT %(limit)s
"""
HYBRID_SEARCH = HYBRID_TEMPLATE.format(query="websearch_to_tsquery('english', %(q)s)")
HYBRID_RELATED = HYBRID_TEMPLATE.format(
    query="(SELECT string_agg(quote_literal(lexeme), ' | ')::tsquery FROM unnest(to_tsvector('english', %(q)s)))")

_EMBEDDER: object | None = None
_EMBEDDER_KEY: tuple[str, str] | None = None
_EMBEDDER_LOCK = threading.RLock()


def _create_embedder(model_name: str, cache_dir: str):
    """Create FastEmbed without an API URL, key, or remote embedding client."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name, cache_dir=cache_dir)


def _get_embedder():
    global _EMBEDDER, _EMBEDDER_KEY
    model_name = os.getenv("AIFLOW4_MEMORY_EMBEDDING_MODEL", DEFAULT_MODEL)
    cache_dir = str(Path(os.getenv("AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR", DEFAULT_CACHE_DIR)).expanduser())
    key = (model_name, cache_dir)
    with _EMBEDDER_LOCK:
        if _EMBEDDER is None or _EMBEDDER_KEY != key:
            _EMBEDDER = _create_embedder(model_name, cache_dir)
            _EMBEDDER_KEY = key
        return _EMBEDDER


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate vectors locally with the configured ONNX model."""
    if not texts:
        return []
    with _EMBEDDER_LOCK:
        model = _get_embedder()
        vectors = list(model.embed(texts))
    return [[float(value) for value in vector] for vector in vectors]


def _vector_literal(vector: list[float]) -> str:
    if len(vector) != VECTOR_DIMENSIONS:
        raise ValueError(f"expected {VECTOR_DIMENSIONS}-dimension memory embedding, got {len(vector)}")
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _index_kind(db: DB, sql: str, kind: str) -> int:
    entries = db.all(sql)
    old_rows = db.all("SELECT ref, project, ticket, text, embedding IS NOT NULL AS embedded FROM memory WHERE kind=%s",
                      (kind,))
    old = {row["ref"]: row for row in old_rows}
    to_embed: list[dict] = []
    for row in entries:
        previous = old.get(row["ref"])
        if previous and previous["text"] == row["text"] and previous["embedded"]:
            continue
        to_embed.append(row)

    vectors: list[list[float] | None] = [None] * len(to_embed)
    if to_embed:
        try:
            generated = embed_texts([row["text"] for row in to_embed])
            if len(generated) != len(to_embed):
                raise ValueError("local embedding model returned the wrong number of vectors")
            checked: list[list[float]] = []
            for vector in generated:
                _vector_literal(vector)  # validate dimensions before opening a database transaction
                checked.append(vector)
            vectors = checked
        except Exception as e:  # noqa: BLE001 - memory remains useful through full-text search
            log.warning("local memory embedding failed for %s entries of kind %s: %s", len(to_embed), kind, e)

    vector_by_ref = {row["ref"]: vector for row, vector in zip(to_embed, vectors)}
    written = 0
    for row in entries:
        previous = old.get(row["ref"])
        text_changed = previous is None or previous["text"] != row["text"]
        needs_embedding = text_changed or previous is None or not previous["embedded"]
        metadata_changed = (previous is None or previous["project"] != row["project"]
                            or previous["ticket"] != row["ticket"])
        if not (needs_embedding or metadata_changed):
            continue
        vector = vector_by_ref.get(row["ref"]) if needs_embedding else None
        params = {**row, "embedding": _vector_literal(vector) if vector is not None else None}
        written += db.run(UPSERT, params)
    return written


def index(db: DB) -> dict[str, int]:
    """Index tickets, finished run summaries and verify findings, embedding changed text locally."""
    return {"ticket": _index_kind(db, INDEX_TICKETS, "ticket"),
            "run_summary": _index_kind(db, INDEX_RUN_SUMMARIES, "run_summary"),
            "finding": _index_kind(db, INDEX_FINDINGS, "finding")}


def _normalize_hits(rows: list[dict]) -> list[dict]:
    return [{"kind": r["kind"], "ref": r["ref"], "ticket": r["ticket"], "project": r["project"],
             "rank": round(float(r["rank"]), 6), "snippet": " ".join((r["snippet"] or "").split()),
             "created_at": r["created_at"].isoformat()} for r in rows]


def _params(project: str, q: str, limit: int, exclude: str = "") -> dict:
    return {"q": q, "project": project or "", "exclude": exclude or "", "limit": max(1, min(int(limit), MAX_LIMIT)),
            "headline": HEADLINE}


def _full_text(db: DB, sql: str, project: str, q: str, limit: int, exclude: str = "") -> list[dict]:
    if not q.strip():
        return []
    return _normalize_hits(db.all(sql, _params(project, q, limit, exclude)))


def _hybrid(db: DB, sql: str, project: str, q: str, limit: int, exclude: str = "") -> list[dict]:
    if not q.strip():
        return []
    scoped = {"project": project or "", "exclude": exclude or ""}
    available = db.one("SELECT EXISTS (SELECT 1 FROM memory WHERE embedding IS NOT NULL"
                       " AND (%(project)s = '' OR project = %(project)s)"
                       " AND (%(exclude)s = '' OR ticket <> %(exclude)s)) AS yes", scoped)
    if not available or not available["yes"]:
        fallback = SEARCH if sql == HYBRID_SEARCH else RELATED
        return _full_text(db, fallback, project, q, limit, exclude)
    query_vector = _vector_literal(embed_texts([q])[0])
    params = _params(project, q, limit, exclude)
    params.update({"embedding": query_vector, "candidate_limit": CANDIDATE_LIMIT, "rrf_k": RRF_K})
    return _normalize_hits(db.all(sql, params))


def search(db: DB, project: str, q: str, limit: int = 10) -> list[dict]:
    """Search with reciprocal-rank fusion of ts_rank and vector cosine similarity.

    If loading the local model, embedding the query, or executing the vector query fails, retain full-text hits.
    """
    try:
        return _hybrid(db, HYBRID_SEARCH, project, q, limit)
    except Exception as e:  # noqa: BLE001 - vector search is an enhancement over full text
        log.warning("hybrid memory search failed; using full-text search: %s", e)
        return _full_text(db, SEARCH, project, q, limit)


def related(db: DB, project: str, text: str, exclude_ticket: str = "", limit: int = 3) -> list[dict]:
    """Related entries in one project, excluding the current ticket; full text survives vector failures."""
    try:
        return _hybrid(db, HYBRID_RELATED, project, text, limit, exclude_ticket)
    except Exception as e:  # noqa: BLE001 - memory is advice and must not block a claim
        log.warning("hybrid related-memory lookup failed; using full-text search: %s", e)
        return _full_text(db, RELATED, project, text, limit, exclude_ticket)
