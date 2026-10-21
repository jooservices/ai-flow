"""Memory: migration, indexing from tickets and runs, ranked search, related hits in the claim and the dev prompt."""
import uuid

from conftest import add_ticket
from test_api import join

from aiflow.v4.control import memory


def ticket(db, key, summary, description="", status="Done", labels=()):
    db.run("INSERT INTO tickets (key, project, status, labels, summary, description) VALUES (%s,%s,%s,%s,%s,%s)"
           " ON CONFLICT (key) DO UPDATE SET summary=EXCLUDED.summary, description=EXCLUDED.description",
           (key, key.split("-")[0], status, list(labels), summary, description))


def run(db, key, step, outcome, summary, details=None, status="finished"):
    from aiflow.v4.control.db import J
    rid = str(uuid.uuid4())
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, outcome, summary, details,"
           " ended_at) VALUES (%s,%s,%s,%s,'codex-01','node1',1,%s,%s,%s,%s,now())",
           (rid, key, key.split("-")[0], step, status, outcome, summary, J(details or {})))
    return rid


def test_migration_creates_the_memory_table(db):
    cols = {r["column_name"]: r for r in db.all(
        "SELECT column_name, data_type, is_generated FROM information_schema.columns WHERE table_name='memory'")}
    assert {"id", "project", "kind", "ref", "text", "created_at", "tsv"} <= set(cols)
    assert cols["tsv"]["data_type"] == "tsvector" and cols["tsv"]["is_generated"] == "ALWAYS"
    idx = {r["indexname"]: r["indexdef"] for r in db.all("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='memory'")}
    assert "gin (tsv)" in idx["memory_tsv"] and "UNIQUE" in idx["memory_kind_ref"]
    import psycopg
    import pytest
    with pytest.raises(psycopg.errors.CheckViolation):
        db.run("INSERT INTO memory (project, kind, ref, text) VALUES ('TESTWP','other','x','y')")


def test_index_is_idempotent_by_kind_and_ref(db):
    ticket(db, "TESTWP-1", "Login form with nonce", "Add a nonce to the login form.")
    dev = run(db, "TESTWP-1", "dev", "done", "Added the nonce field and a capability check.")
    rework = run(db, "TESTWP-1", "verify", "rework", "verify failed: ...", {"findings": "- login.php: nonce is never verified"})
    run(db, "TESTWP-1", "dev", "failed", "worker error: boom")             # failures are not memory
    run(db, "TESTWP-1", "dev", "done", "still running", status="running")  # not finished yet
    assert memory.index(db) == {"ticket": 1, "run_summary": 1, "finding": 1}
    assert memory.index(db) == {"ticket": 0, "run_summary": 0, "finding": 0}  # nothing changed: nothing rewritten
    rows = {(r["kind"], r["ref"]): r for r in db.all("SELECT * FROM memory")}
    assert set(rows) == {("ticket", "TESTWP-1"), ("run_summary", dev), ("finding", rework)}
    assert "nonce is never verified" in rows[("finding", rework)]["text"]
    assert all(r["ticket"] == "TESTWP-1" and r["project"] == "TESTWP" for r in rows.values())
    # a changed ticket rewrites its one row
    ticket(db, "TESTWP-1", "Login form with nonce and rate limit", "Add a nonce and a rate limit.")
    assert memory.index(db)["ticket"] == 1
    assert db.one("SELECT count(*) AS n FROM memory WHERE kind='ticket'")["n"] == 1
    assert "rate limit" in db.one("SELECT text FROM memory WHERE ref='TESTWP-1'")["text"]


def test_search_ranks_and_snippets(client, ctx):
    db = ctx.db
    ticket(db, "TESTWP-1", "Cache the product list", "Cache the product list in Redis. Cache keys per locale; cache invalidation on save.")
    ticket(db, "TESTWP-2", "Footer links", "Add links to the footer; mention the cache page once.")
    ticket(db, "TESTWP-3", "Unrelated header logo", "Swap the logo.")
    ticket(db, "OTHER-1", "Cache everything", "Cache cache cache.")
    memory.index(db)
    r = client.get("/v1/memory/search", params={"project": "TESTWP", "q": "cache", "limit": 5})
    assert r.status_code == 200
    hits = r.json()["hits"]
    assert [h["ref"] for h in hits] == ["TESTWP-1", "TESTWP-2"]   # more matches rank first; other projects excluded
    assert hits[0]["rank"] > hits[1]["rank"] and hits[0]["kind"] == "ticket"
    assert "**Cache**" in hits[0]["snippet"]
    # websearch syntax: quoted phrase and exclusion
    assert [h["ref"] for h in client.get("/v1/memory/search", params={"project": "TESTWP", "q": '"product list" -footer'}
                                         ).json()["hits"]] == ["TESTWP-1"]
    # limit, all projects, empty query
    assert len(client.get("/v1/memory/search", params={"q": "cache", "limit": 1}).json()["hits"]) == 1
    assert {h["project"] for h in client.get("/v1/memory/search", params={"q": "cache"}).json()["hits"]} == {"TESTWP", "OTHER"}
    assert client.get("/v1/memory/search", params={"project": "TESTWP", "q": ""}).json()["hits"] == []


def test_claim_carries_related_hits_and_dev_prompt_shows_them(client, ctx):
    db = ctx.db
    ticket(db, "TESTWP-20", "Product cache in Redis", "Cache the product list.")
    run(db, "TESTWP-20", "dev", "done", "Cached the product list with a 10 minute TTL.")
    run(db, "TESTWP-20", "verify", "rework", "x", {"findings": "- cache.php: product cache not cleared on save"})
    ticket(db, "TESTWP-21", "Product cache warmup")
    ticket(db, "TESTWP-22", "Product page breadcrumbs")
    ticket(db, "OTHER-9", "Product cache elsewhere")
    add_ticket(db, "TESTWP-30")
    db.run("UPDATE tickets SET summary='Clear the product cache on save' WHERE key='TESTWP-30'")
    memory.index(db)
    cc = join(client, "cc")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-30", "account": "claude-01"}, headers=cc).json()
    rel = r["related"]
    assert 1 <= len(rel) <= 3
    assert all(h["project"] == "TESTWP" and h["ticket"] != "TESTWP-30" for h in rel)  # own ticket and other projects excluded
    assert rel[0]["ticket"] == "TESTWP-20" and rel[0]["snippet"]
    assert {"kind", "ref", "ticket", "snippet", "rank"} <= set(rel[0])

    from aiflow.v4.worker.dev import Claimed, build_prompt
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token="t", account="claude-01", provider="claude", model="opus",
                effort="", ticket=r["ticket"], project=r["project"], identity=tuple(r["identity"]), related=r["related"])
    prompt = build_prompt(c, "TESTWP-30-x")
    assert "Related past work" in prompt and "- TESTWP-20 (" in prompt
    c.related = []
    assert "Related past work" not in build_prompt(c, "TESTWP-30-x")


def test_verify_claim_carries_related_hits(client, ctx):
    db = ctx.db
    ticket(db, "TESTWP-40", "Newsletter signup form", "A signup form.")
    memory.index(db)
    add_ticket(db, "TESTWP-41", status="In Verify")
    db.run("UPDATE tickets SET summary='Newsletter signup validation' WHERE key='TESTWP-41'")
    db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, outcome, pr_url, branch)"
           " VALUES (%s,'TESTWP-41','TESTWP','dev','codex-01','node1',1,'finished','done',"
           "'https://github.com/jooservices/test-aiflow-wp/pull/7','TESTWP-41-x')", (str(uuid.uuid4()),))
    cc = join(client, "cc")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-41", "account": "claude-01", "step": "verify"}, headers=cc).json()
    assert r["claimed"] and r["pr"]["number"] == 7
    assert [h["ticket"] for h in r["related"]] == ["TESTWP-40"]


def test_claim_without_related_words_or_memory_still_works(client, ctx):
    add_ticket(ctx.db, "TESTWP-50")
    ctx.db.run("UPDATE tickets SET summary='the and of' WHERE key='TESTWP-50'")  # only stop words
    cc = join(client, "cc")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["related"] == []
    ctx.db.run("DROP TABLE memory")  # memory is advice: a broken memory never stops a claim
    add_ticket(ctx.db, "TESTWP-51")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-51", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["related"] == []
