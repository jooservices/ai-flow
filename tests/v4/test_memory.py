"""Memory indexing, hybrid retrieval, claim advice and dev/verify prompt sections."""
import uuid

from conftest import add_ticket
from test_api import join

from aiflow.v4.control import memory


def fail_embedding(monkeypatch):
    def fail(_texts):
        raise RuntimeError("local model unavailable")
    monkeypatch.setattr(memory, "embed_texts", fail)


def fake_embedding(monkeypatch, vectors=None):
    """Install a deterministic local-model stand-in and return the text batches it receives."""
    batches = []

    def embed(texts):
        batches.append(list(texts))
        return vectors(texts) if callable(vectors) else (vectors or [[1.0] + [0.0] * 383 for _ in texts])

    monkeypatch.setattr(memory, "embed_texts", embed)
    return batches


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
        "SELECT column_name, data_type, is_generated, udt_name FROM information_schema.columns WHERE table_name='memory'")}
    assert {"id", "project", "kind", "ref", "text", "created_at", "tsv", "embedding"} <= set(cols)
    assert cols["tsv"]["data_type"] == "tsvector" and cols["tsv"]["is_generated"] == "ALWAYS"
    assert cols["embedding"]["udt_name"] == "vector"
    idx = {r["indexname"]: r["indexdef"] for r in db.all("SELECT indexname, indexdef FROM pg_indexes WHERE tablename='memory'")}
    assert "gin (tsv)" in idx["memory_tsv"] and "UNIQUE" in idx["memory_kind_ref"]
    assert "hnsw" in idx["memory_embedding_hnsw"] and "vector_cosine_ops" in idx["memory_embedding_hnsw"]
    import psycopg
    import pytest
    with pytest.raises(psycopg.errors.CheckViolation):
        db.run("INSERT INTO memory (project, kind, ref, text) VALUES ('TESTWP','other','x','y')")


def test_migration_applies_when_memory_already_exists(db):
    db.run("INSERT INTO memory (project, kind, ref, ticket, text) VALUES ('TESTWP','ticket','old','old','existing text')")
    db.run("DELETE FROM schema_migrations WHERE version='005_memory_vectors'")
    assert "005_memory_vectors" in db.migrate()
    assert db.one("SELECT text, embedding FROM memory WHERE ref='old'") == {"text": "existing text", "embedding": None}


def test_local_embedder_uses_model_and_cache_without_embedding_endpoint_or_key(monkeypatch):
    import sys
    import types

    captured = {}

    class FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def embed(self, texts, batch_size):
            captured["batch_size"] = batch_size
            return [[0.0] * 384 for _ in texts]

    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_MODEL", "local-test-model")
    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR", "/tmp/aiflow-memory-models")
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeModel))
    monkeypatch.setattr(memory, "_EMBEDDER", None)
    monkeypatch.setattr(memory, "_EMBEDDER_KEY", None)
    assert memory.embed_texts(["test"])[0] == [0.0] * 384
    assert captured == {"model_name": "local-test-model", "cache_dir": "/tmp/aiflow-memory-models",
                        "threads": 2, "batch_size": 16}
    assert "endpoint" not in captured and "api_key" not in captured


def test_failed_embedder_load_is_retried_after_cooldown(monkeypatch):
    import pytest

    attempts = []
    clock = [100.0]

    def create(model_name, cache_dir):
        attempts.append((model_name, cache_dir))
        raise RuntimeError("model download unavailable")

    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_MODEL", "failing-test-model")
    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR", "/tmp/failing-model-cache")
    monkeypatch.setattr(memory, "_create_embedder", create)
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory, "_EMBEDDER", None)
    monkeypatch.setattr(memory, "_EMBEDDER_KEY", None)
    monkeypatch.setattr(memory, "_EMBEDDER_FAILURE_KEY", None)
    monkeypatch.setattr(memory, "_EMBEDDER_RETRY_AT", 0.0)

    with pytest.raises(RuntimeError, match="download unavailable"):
        memory.embed_texts(["first claim"])
    with pytest.raises(RuntimeError, match="cooldown"):
        memory.embed_texts(["second claim"])
    assert len(attempts) == 1

    clock[0] += memory.EMBEDDER_RETRY_SECONDS + 1
    with pytest.raises(RuntimeError, match="download unavailable"):
        memory.embed_texts(["claim after cooldown"])
    assert len(attempts) == 2


def test_index_is_idempotent_by_kind_and_ref(db, monkeypatch):
    batches = fake_embedding(monkeypatch)
    ticket(db, "TESTWP-1", "Login form with nonce", "Add a nonce to the login form.")
    dev = run(db, "TESTWP-1", "dev", "done", "Added the nonce field and a capability check.")
    rework = run(db, "TESTWP-1", "verify", "rework", "verify failed: ...", {"findings": "- login.php: nonce is never verified"})
    run(db, "TESTWP-1", "dev", "done", "still running", status="running")  # not finished yet
    run(db, "TESTWP-1", "dev", "failed", "")                               # finished without a summary
    assert memory.index(db) == {"ticket": 1, "run_summary": 2, "finding": 1}
    embedded_count = sum(len(batch) for batch in batches)
    assert embedded_count == 4
    assert all(r["embedding"] is not None for r in db.all("SELECT embedding FROM memory"))
    assert memory.index(db) == {"ticket": 0, "run_summary": 0, "finding": 0}  # nothing changed: nothing rewritten
    assert sum(len(batch) for batch in batches) == embedded_count  # unchanged entries are not embedded again
    rows = {(r["kind"], r["ref"]): r for r in db.all("SELECT * FROM memory")}
    assert set(rows) == {("ticket", "TESTWP-1"), ("run_summary", dev), ("run_summary", rework), ("finding", rework)}
    assert "nonce is never verified" in rows[("finding", rework)]["text"]
    assert all(r["ticket"] == "TESTWP-1" and r["project"] == "TESTWP" for r in rows.values())
    # a changed ticket rewrites its one row
    ticket(db, "TESTWP-1", "Login form with nonce and rate limit", "Add a nonce and a rate limit.")
    assert memory.index(db) == {"ticket": 1, "run_summary": 2, "finding": 1}
    # The run summaries and finding include the ticket summary in their indexed text, so those changed too.
    assert sum(len(batch) for batch in batches) == embedded_count + 4
    assert db.one("SELECT count(*) AS n FROM memory WHERE kind='ticket'")["n"] == 1
    assert "rate limit" in db.one("SELECT text FROM memory WHERE ref='TESTWP-1'")["text"]


def test_every_finished_outcome_is_a_run_summary(db, monkeypatch):
    fail_embedding(monkeypatch)
    ticket(db, "TESTWP-2", "Checkout page")
    outcomes = ("done", "blocked", "merged", "human", "failed", "refused", "timeout", "lost")
    refs = {run(db, "TESTWP-2", "dev", o, f"run ended {o}"): o for o in outcomes}
    run(db, "TESTWP-2", "dev", "", "no outcome recorded")
    assert memory.index(db)["run_summary"] == len(outcomes) + 1
    rows = {r["ref"]: r["text"] for r in db.all("SELECT ref, text FROM memory WHERE kind='run_summary'")}
    for ref, outcome in refs.items():
        assert f"dev {outcome}: run ended {outcome}" in rows[ref]
    assert any("dev finished: no outcome recorded" in t for t in rows.values())
    assert memory.search(db, "TESTWP", "merged")[0]["kind"] == "run_summary"


def test_search_ranks_and_snippets(client, ctx, monkeypatch):
    fail_embedding(monkeypatch)
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


def test_memory_endpoint_combines_full_text_only_and_vector_only_hits(client, ctx, monkeypatch):
    qvector = "[" + ",".join(["0"] * 383 + ["1"]) + "]"
    fts_vector = "[1," + ",".join(["0"] * 383) + "]"
    ctx.db.run("INSERT INTO memory (project, kind, ref, ticket, text, embedding)"
               " VALUES ('TESTWP','ticket','fts-only','TESTWP-80','cache key expires after a short interval',%s::vector)",
               (fts_vector,))
    ctx.db.run("INSERT INTO memory (project, kind, ref, ticket, text, embedding)"
               " VALUES ('TESTWP','ticket','vector-only','TESTWP-81','deployment history for the service',%s::vector)",
               (qvector,))
    monkeypatch.setattr(memory, "embed_texts", lambda _texts: [[0.0] * 383 + [1.0]])

    response = client.get("/v1/memory/search", params={"project": "TESTWP", "q": "cache", "limit": 10})
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert {hit["ref"] for hit in hits} == {"fts-only", "vector-only"}
    assert all(set(hit) == {"kind", "ref", "ticket", "project", "rank", "created_at", "snippet"} for hit in hits)


def test_vector_search_failure_falls_back_to_full_text(db, monkeypatch):
    fail_embedding(monkeypatch)
    ticket(db, "TESTWP-82", "Cache the profile response", "Cache its response for a short time.")
    memory.index(db)
    original_all = db.all

    def fail_vector(sql, params=None):
        if "<=>" in sql:
            raise RuntimeError("pgvector search unavailable")
        return original_all(sql, params)

    monkeypatch.setattr(db, "all", fail_vector)
    hits = memory.search(db, "TESTWP", "cache", 10)
    assert [hit["ref"] for hit in hits] == ["TESTWP-82"]


def test_claim_related_uses_vector_only_hits_and_preserves_scope(client, ctx, monkeypatch):
    fake_embedding(monkeypatch)
    ticket(ctx.db, "TESTWP-83", "Suspend profile after repeated authentication failures")
    ticket(ctx.db, "OTHER-83", "Suspend profile after repeated authentication failures")
    add_ticket(ctx.db, "TESTWP-84")
    ctx.db.run("UPDATE tickets SET summary='Prevent automated credential guessing' WHERE key='TESTWP-84'")
    memory.index(ctx.db)
    # The seed and query share no indexed lexemes, so a hit proves claim retrieval includes vectors.
    assert memory._full_text(ctx.db, memory.RELATED, "TESTWP", "Prevent automated credential guessing", 10,
                              "TESTWP-84") == []

    cc = join(client, "cc")
    response = client.post("/v1/work/claim", json={"ticket": "TESTWP-84", "account": "claude-01"}, headers=cc)
    assert response.status_code == 200
    related = response.json()["related"]
    assert [hit["ticket"] for hit in related] == ["TESTWP-83"]
    assert len(related) <= 3


def test_claim_related_falls_back_to_full_text_when_vector_search_fails(client, ctx, monkeypatch):
    fake_embedding(monkeypatch)
    ticket(ctx.db, "TESTWP-85", "Cache account settings by locale")
    add_ticket(ctx.db, "TESTWP-86")
    ctx.db.run("UPDATE tickets SET summary='Cache account expiration settings by locale' WHERE key='TESTWP-86'")
    memory.index(ctx.db)
    original_all = ctx.db.all

    def fail_vector(sql, params=None):
        if "<=>" in sql:
            raise RuntimeError("pgvector search unavailable")
        return original_all(sql, params)

    monkeypatch.setattr(ctx.db, "all", fail_vector)
    cc = join(client, "cc")
    response = client.post("/v1/work/claim", json={"ticket": "TESTWP-86", "account": "claude-01"}, headers=cc)
    assert response.status_code == 200
    assert [hit["ticket"] for hit in response.json()["related"]] == ["TESTWP-85"]


def test_real_local_model_retrieves_vietnamese_and_english_semantic_matches(db, monkeypatch, tmp_path_factory):
    cache_root = tmp_path_factory.getbasetemp().parent / "fastembed-memory-test-cache"
    monkeypatch.setenv("AIFLOW4_MEMORY_EMBEDDING_CACHE_DIR", str(cache_root / "models"))
    monkeypatch.setenv("HF_HOME", str(cache_root / "huggingface"))
    ticket(db, "TESTVN-1", "Tạm khóa hồ sơ khi xác thực sai liên tiếp nhiều lần.")
    ticket(db, "TESTEN-1", "Temporarily suspend the profile after repeated authentication failures.")
    assert memory.index(db) == {"ticket": 2, "run_summary": 0, "finding": 0}
    assert db.one("SELECT count(*) AS n FROM memory WHERE embedding IS NOT NULL")["n"] == 2

    vietnamese = memory.search(db, "TESTVN", "Chống kẻ xấu dò mật khẩu từ xa.", 5)
    english = memory.search(db, "TESTEN", "Reduce automated credential guessing.", 5)
    assert [hit["ref"] for hit in vietnamese] == ["TESTVN-1"]
    assert [hit["ref"] for hit in english] == ["TESTEN-1"]
    # Neither query has a full-text lexeme in the target entry; these are vector-only semantic matches.
    assert memory._full_text(db, memory.SEARCH, "TESTVN", "Chống kẻ xấu dò mật khẩu từ xa.", 5) == []
    assert memory._full_text(db, memory.SEARCH, "TESTEN", "Reduce automated credential guessing.", 5) == []


def test_claim_carries_related_hits_and_dev_prompt_shows_them(client, ctx, monkeypatch):
    fail_embedding(monkeypatch)
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


def test_verify_claim_carries_related_hits(client, ctx, monkeypatch):
    fail_embedding(monkeypatch)
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
    from aiflow.v4.worker.dev import Claimed
    from aiflow.v4.worker.verify import build_prompt
    c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token="t", account="claude-01", provider="claude",
                model="opus", effort="", ticket=r["ticket"], project=r["project"], identity=tuple(r["identity"]),
                related=r["related"])
    prompt = build_prompt(c, r["pr"])
    assert "Related past work" in prompt and "advice only, the ticket wins" in prompt
    assert "TESTWP-40" in prompt


def test_claim_without_related_words_or_memory_still_works(client, ctx, monkeypatch):
    fail_embedding(monkeypatch)
    add_ticket(ctx.db, "TESTWP-50")
    ctx.db.run("UPDATE tickets SET summary='the and of' WHERE key='TESTWP-50'")  # only stop words
    cc = join(client, "cc")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-50", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["related"] == []
    ctx.db.run("DROP TABLE memory")  # memory is advice: a broken memory never stops a claim
    add_ticket(ctx.db, "TESTWP-51")
    r = client.post("/v1/work/claim", json={"ticket": "TESTWP-51", "account": "claude-01"}, headers=cc).json()
    assert r["claimed"] and r["related"] == []
