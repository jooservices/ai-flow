"""Ollama embeddings for the shared memory service, using the v4 PostgreSQL test database."""
from __future__ import annotations

import json
import threading
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import psycopg
import pytest

from aiflow.memory.embed import DIMENSIONS, Embedder, literal
from aiflow.memory.scoped import Scoped
from aiflow.memory.service import Tokens, fill_vectors, make_server
from aiflow.memory.store import Store


def vector_for(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    lowered = text.lower()
    for axis, aliases in enumerate((("car", "automobile", "vehicle"), ("cache", "memoized"))):
        if any(alias in lowered for alias in aliases):
            vector[axis] = 1.0
    if not any(vector):
        vector[-1] = 1.0
    return vector


class FakeOllama:
    def __init__(self) -> None:
        self.available = True
        self.requests: list[dict[str, Any]] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state.requests.append({"path": self.path, **payload})
                if not state.available:
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps({"embeddings": [vector_for(text) for text in payload["input"]]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def memory_vector_store(db, test_database_url):
    ollama = FakeOllama()
    store = Store(lambda: psycopg.connect(test_database_url), Embedder(ollama.url, "fake-embedding", timeout=2))
    store.migrate()
    store.run("truncate memory_feedback, memory_event, memory_entry cascade", fetch=False)
    yield store, ollama
    ollama.close()


def put(store: Store, title: str, body: str, namespace: str = "JOOservices", status: str = "verified") -> str:
    entry_id, _created = store.put({"kind": "fact", "scope": "namespace", "namespace": namespace, "project": "", "repo": "",
                                    "title": title, "body": body, "tags": []}, "test", status)
    return entry_id


@contextmanager
def memory_api(store: Store, log=lambda _message: None):
    tokens = Tokens([{"name": "test", "token": "test-token", "role": "client", "namespaces": ["JOOservices"]}])
    server = make_server(store, tokens, log=log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def post_json(base: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=4) as response:
        return response.status, json.loads(response.read())


def test_schema_is_idempotent_and_has_1024d_cosine_index(memory_vector_store):
    store, _ollama = memory_vector_store
    store.migrate()
    store.migrate()
    extension = store.run("select 1 from pg_extension where extname = 'vector'")
    columns = store.run("select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
                        "where a.attrelid = 'memory_entry'::regclass and a.attname = 'embedding'")
    names = {row[0] for row in store.run("select column_name from information_schema.columns where table_name = 'memory_entry'")}
    indexes = {row[0]: row[1] for row in store.run("select indexname, indexdef from pg_indexes where tablename = 'memory_entry'")}
    assert extension and columns == [("vector(1024)",)]
    assert {"embedding", "embedding_model", "embedded_at"} <= names
    assert "hnsw" in indexes["memory_entry_embedding"] and "vector_cosine_ops" in indexes["memory_entry_embedding"]


def test_embedder_reads_url_and_model_from_service_environment(memory_vector_store):
    _store, ollama = memory_vector_store
    embedder = Embedder.from_env({"AIFLOW_MEMORY_EMBED_URL": ollama.url + "/", "AIFLOW_MEMORY_EMBED_MODEL": "configured-model"})
    assert embedder is not None
    assert embedder.embed(["a car"])
    assert (ollama.requests[-1]["path"], ollama.requests[-1]["model"]) == ("/api/embed", "configured-model")


def test_backfill_batches_missing_and_old_model_vectors_including_decayed_entries(memory_vector_store):
    store, ollama = memory_vector_store
    ids = [put(store, f"History {i}", f"A car note {i}") for i in range(3)]
    store.decay(ids[-1], "test", "historical entry")
    store.run("update memory_entry set embedding = %s::vector, embedding_model = 'old-model' where id = %s",
              (literal(vector_for("car")), ids[0]), fetch=False)

    assert store.embed_pending(batch=2) == 2
    assert store.embed_pending(batch=2) == 1
    assert store.embed_pending(batch=2) == 0
    rows = store.run("select embedding_model, embedded_at from memory_entry order by id")
    assert len(rows) == 3 and all(model == "fake-embedding" and embedded_at is not None for model, embedded_at in rows)
    assert sorted(len(call["input"]) for call in ollama.requests) == [1, 2]


def test_http_search_combines_keyword_and_meaning_without_cross_namespace_hits(memory_vector_store):
    store, _ollama = memory_vector_store
    meaning_id = put(store, "Parking note", "The car park is beside the building.")
    keyword_id = put(store, "Vehicle guide", "An automobile needs a permit.")
    put(store, "Other company parking", "A car is kept in a garage.", namespace="OtherCo")
    store.embed_pending()

    with memory_api(store) as base:
        status, body = post_json(base, "/v1/search", {"query": "automobile", "namespace": "JOOservices", "limit": 10})
    assert status == 200
    assert {item["id"] for item in body["items"]} == {meaning_id, keyword_id}
    assert not any(item["namespace"] == "OtherCo" for item in body["items"])
    assert not store.run("select 1 from memory_entry where id = %s and tsv @@ to_tsquery('simple', 'automobile')", (meaning_id,))


def test_scoped_notes_search_uses_vectors_inside_existing_scope_filters(memory_vector_store):
    store, _ollama = memory_vector_store
    scoped = Scoped(store)
    target = scoped.add("Parking note", "The car park is beside the building.", namespace="JOOservices")
    other = scoped.add("Other company parking", "A car is kept in a garage.", namespace="OtherCo")
    store.embed_pending()

    results = scoped.search("automobile", namespace="JOOservices")
    assert [item["id"] for item in results] == [target]
    assert [item["id"] for item in scoped.search("automobile", namespace="OtherCo")] == [other]


def test_outage_keeps_keyword_search_and_writes_working_then_retries(memory_vector_store):
    store, ollama = memory_vector_store
    existing = put(store, "Car parking", "Find the car near the entrance.")
    store.embed_pending()
    ollama.available = False

    with memory_api(store) as base:
        status, keyword = post_json(base, "/v1/search", {"query": "car", "namespace": "JOOservices"})
        status2, no_semantic = post_json(base, "/v1/search", {"query": "automobile", "namespace": "JOOservices"})
        status3, write = post_json(base, "/v1/entries", {"kind": "fact", "scope": "namespace", "namespace": "JOOservices",
                                                            "title": "New cache note", "body": "Cache the short-lived result."})
    assert status == status2 == 200 and existing in {item["id"] for item in keyword["items"]}
    assert no_semantic["items"] == []
    assert status3 == 201 and write["created"]

    class StopAfterRetry:
        checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > 2

    class RestoreOllamaAfterWait:
        def wait(self, _seconds: int) -> None:
            ollama.available = True

        def clear(self) -> None:
            return None

    fill_vectors(store, StopAfterRetry(), RestoreOllamaAfterWait())
    embedded = store.run("select embedding is not null, embedding_model from memory_entry where id = %s", (write["id"],))
    assert embedded == [(True, "fake-embedding")]


def test_write_endpoint_never_calls_ollama_synchronously(memory_vector_store):
    store, ollama = memory_vector_store
    with memory_api(store) as base:
        status, result = post_json(base, "/v1/entries", {"kind": "fact", "scope": "namespace", "namespace": "JOOservices",
                                                          "title": "A fresh note", "body": "Stored without waiting."})
    assert status == 201 and result["created"]
    assert ollama.requests == []


def test_search_and_background_error_logs_never_include_exception_secrets(memory_vector_store):
    from urllib.error import HTTPError

    store, _ollama = memory_vector_store
    secret = "token=embedding-secret-value"
    logs: list[str] = []

    def fail_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError(secret)

    store.search = fail_search  # type: ignore[method-assign]
    with memory_api(store, logs.append) as base:
        with pytest.raises(HTTPError) as caught:
            post_json(base, "/v1/search", {"query": "private query", "namespace": "JOOservices"})
    assert caught.value.code == 503
    assert logs == ["memory service: RuntimeError"]
    assert secret not in " ".join(logs)

    class StopAfterOneWait:
        checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > 1

    class WakeOnce:
        def wait(self, _seconds: int) -> None:
            return None

        def clear(self) -> None:
            return None

    store.embed_pending = lambda: (_ for _ in ()).throw(RuntimeError(secret))  # type: ignore[method-assign]
    background_logs: list[str] = []
    fill_vectors(store, StopAfterOneWait(), WakeOnce(), background_logs.append)
    assert background_logs == ["memory vectors: RuntimeError"]
    assert secret not in " ".join(background_logs)


def test_startup_does_not_log_the_embedding_url(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from aiflow.memory import service
    from aiflow import config

    sensitive_url = "http://user:embedding-secret@ollama.example/path"
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps([{"name": "test", "token": "test-token", "role": "client", "namespaces": ["*"]}]))

    class FakeServer:
        server_address = ("127.0.0.1", 8750)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

    class FakeThread:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(service, "os", SimpleNamespace(environ={"AIFLOW_MEMORY_TOKENS_FILE": str(token_file)}))
    monkeypatch.setattr(config, "read_env_file", lambda _path: {})
    monkeypatch.setattr(service.Embedder, "from_env", classmethod(lambda _cls, _env: Embedder(sensitive_url, "fake-model")))
    monkeypatch.setattr(service, "connector", lambda _env: lambda: None)
    monkeypatch.setattr(service.Store, "migrate", lambda _self: None)
    monkeypatch.setattr(service, "make_server", lambda *_args, **_kwargs: FakeServer())
    monkeypatch.setattr(service.threading, "Thread", FakeThread)

    assert service.main() == 0
    output = capsys.readouterr().out
    assert "fake-model" in output
    assert sensitive_url not in output and "embedding-secret" not in output
