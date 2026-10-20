"""Search by meaning: the Ollama client against a fake server, and the store against a real PostgreSQL with pgvector (database
`ai_memory_test`, skipped when it is not there). Run: uv run --with pg8000 python -m unittest tests.test_memory_vector"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import pg8000.dbapi
except ImportError:
    pg8000 = None

from aiflow import config
from aiflow.memory.embed import DIMENSIONS, Embedder, literal
from aiflow.memory.scoped import Scoped
from aiflow.memory.store import Store

ROOT = Path(__file__).resolve().parents[4]
CONCEPTS = [("car", "automobile", "vehicle"), ("cache", "memoized"), ("bird", "sparrow")]  # words of one concept share one axis


def vector_of(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for axis, words in enumerate(CONCEPTS):
        if any(word in text.lower() for word in words):
            vector[axis] = 1.0
    if not any(vector):
        vector[DIMENSIONS - 1] = 1.0
    return vector


class FakeOllama(BaseHTTPRequestHandler):
    answer = "ok"  # ok | short | broken

    def do_POST(self):  # noqa: N802
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if FakeOllama.answer == "broken":
            self.send_response(500)
            self.end_headers()
            return
        vectors = [vector_of(t) for t in data["input"]]
        if FakeOllama.answer == "short":
            vectors = [v[:10] for v in vectors]
        raw = json.dumps({"model": data["model"], "embeddings": vectors}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class OllamaServer:
    def __enter__(self):
        FakeOllama.answer = "ok"
        self.server = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class EmbedderTests(unittest.TestCase):
    def test_vectors_and_every_failure_mode(self):
        with OllamaServer() as ollama:
            embedder = Embedder(ollama.url, "m", timeout=5)
            self.assertEqual(len(embedder.embed(["a car", "a bird"])), 2)
            self.assertEqual(embedder.embed([]), [])
            FakeOllama.answer = "short"
            self.assertIsNone(embedder.embed(["a car"]))  # wrong dimension is refused
            FakeOllama.answer = "broken"
            self.assertIsNone(embedder.embed(["a car"]))
        self.assertIsNone(Embedder("http://127.0.0.1:9", "m", timeout=1).embed(["a car"]))  # nothing listens

    def test_from_env(self):
        self.assertIsNone(Embedder.from_env({}))
        embedder = Embedder.from_env({"AIFLOW_MEMORY_EMBED_URL": "http://x:1/", "AIFLOW_MEMORY_EMBED_MIN_SIMILARITY": "oops"})
        self.assertEqual((embedder.url, embedder.model, embedder.min_similarity), ("http://x:1", "qwen3-embedding:0.6b", 0.45))

    def test_literal(self):
        self.assertEqual(literal([1.0, 0.25, 0.0]), "[1,0.25,0]")


def connect_factory():
    env = config.read_env_file(ROOT / ".env")
    if not env.get("WORKSPACES_POSTGRES_USER"):
        return None
    port = int(env.get("WORKSPACES_POSTGRES_PORT", "15435"))
    return lambda: pg8000.dbapi.connect(user=env["WORKSPACES_POSTGRES_USER"], password=env["WORKSPACES_POSTGRES_PASSWORD"], host="127.0.0.1",
                                        port=port, database="ai_memory_test")  # never any other database: the tests empty the tables


@unittest.skipIf(pg8000 is None, "pg8000 is not installed")
class VectorSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        connect = connect_factory()
        if connect is None:
            raise unittest.SkipTest("no root .env")
        cls.connect = staticmethod(connect)
        try:
            Store(connect).migrate()
        except Exception as error:
            raise unittest.SkipTest(f"no PostgreSQL database with pgvector: {type(error).__name__}")

    def setUp(self):
        self.ollama = OllamaServer().__enter__()
        self.addCleanup(self.ollama.__exit__)
        self.store = Store(self.connect, Embedder(self.ollama.url, "fake", timeout=5))
        self.store.run("truncate memory_feedback, memory_event, memory_entry cascade", fetch=False)

    def put(self, title, body, namespace="JOOservices", status="verified"):
        return self.store.put(dict(kind="fact", scope="namespace", namespace=namespace, project="", repo="", title=title, body=body, tags=[], created_by="t"), "t", status)[0]

    def titles(self, query, namespace="JOOservices"):
        return [i["title"] for i in self.store.search(query, namespace=namespace)]

    def test_meaning_finds_what_words_do_not(self):
        self.put("Driving", "Notes about the car park")
        self.put("Flying", "Notes about a sparrow")
        self.assertEqual(self.titles("automobile"), [])  # no vector yet: words only
        self.assertEqual(self.store.embed_pending(), 2)
        self.assertEqual(self.store.embed_pending(), 0)  # nothing left
        self.assertEqual(self.titles("automobile"), ["Driving"])  # no shared word, found by meaning
        self.assertEqual(self.titles("car park"), ["Driving"])  # words and meaning agree

    def test_another_company_is_never_found(self):
        self.put("Driving", "Notes about the car park", namespace="OtherCo")
        self.store.embed_pending()
        self.assertEqual(self.titles("automobile"), [])
        self.assertEqual(self.titles("automobile", namespace="OtherCo"), ["Driving"])

    def test_ollama_down_keeps_words_search_and_writes(self):
        self.put("Driving", "Notes about the car park")
        self.store.embed_pending()
        FakeOllama.answer = "broken"
        self.assertEqual(self.titles("car"), ["Driving"])  # words still work
        self.assertEqual(self.titles("automobile"), [])  # meaning is off, no error
        self.put("Caching", "Notes about the cache layer")  # a write is never refused
        self.assertEqual(self.store.embed_pending(), 0)
        FakeOllama.answer = "ok"
        self.assertEqual(self.store.embed_pending(), 1)

    def test_a_changed_model_is_filled_again(self):
        self.put("Driving", "Notes about the car park")
        self.store.embed_pending()
        self.store.embedder = Embedder(self.ollama.url, "newer", timeout=5)
        self.assertEqual(self.titles("automobile"), [])  # old vectors belong to another model
        self.assertEqual(self.store.embed_pending(), 1)
        self.assertEqual(self.titles("automobile"), ["Driving"])

    def test_scoped_notes_search_by_meaning(self):
        scoped = Scoped(self.store)
        scoped.add("Driving", "Notes about the car park", namespace="JOOservices")
        self.store.embed_pending()
        self.assertEqual([n["title"] for n in scoped.search("automobile", namespace="JOOservices")], ["Driving"])
        self.assertEqual(scoped.search("automobile", namespace="OtherCo"), [])


if __name__ == "__main__":
    unittest.main()
