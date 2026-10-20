import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from aiflow.memory import cases, guard
from aiflow.memory.client import Memory, Unavailable
from aiflow.memory.service import Tokens, make_server
from aiflow.memory.store import NotFound
from tests.test_supervisor import FakeJira, FakeRedis, make


def entry(**kw):
    base = dict(kind="lesson", scope="project", namespace="JOOservices", project="JAVM", title="Retry with backoff", body="Wait 60 seconds before the second try.")
    return {**base, **kw}


class GuardTests(unittest.TestCase):
    def test_a_good_entry_is_cleaned(self):
        clean = guard.check_entry(entry(tags=["Crawl", "RETRY"], created_by="Codex"))
        self.assertEqual((clean["tags"], clean["namespace"], clean["project"]), (["crawl", "retry"], "JOOservices", "JAVM"))

    def test_scope_decides_which_names_are_needed(self):
        self.assertEqual(guard.check_entry(entry(scope="global"))["namespace"], "")
        self.assertEqual(guard.check_entry(entry(scope="namespace"))["project"], "")
        for bad in (entry(namespace=""), entry(project=""), entry(scope="ticket", project="")):
            with self.assertRaises(guard.Rejected):
                guard.check_entry(bad)

    def test_bad_shapes_are_rejected(self):
        for bad in (entry(kind="note"), entry(scope="planet"), entry(title="ab"), entry(body="x" * 9000), entry(tags="a"), entry(tags=["x"] * 13)):
            with self.assertRaises(guard.Rejected):
                guard.check_entry(bad)

    def test_secrets_are_refused_without_echoing_them(self):
        samples = ["password = hunter2hunter2", "token: abcdef123456", "-----BEGIN RSA PRIVATE KEY-----", "AKIAABCDEFGHIJKLMNOP",
                   "ghp_" + "a" * 30, "sk-" + "b" * 30, "postgres://root:s3cretpw@db/x", "Authorization: Bearer " + "c" * 30,
                   "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"]
        for text in samples:
            with self.assertRaises(guard.Rejected) as caught:
                guard.check_entry(entry(body=f"note {text} end"))
            self.assertNotIn(text, str(caught.exception))

    def test_the_hash_ignores_case_and_spacing_and_the_query_is_safe(self):
        self.assertEqual(guard.content_hash("fact", "A  b", "C d"), guard.content_hash("fact", "a b", "c   D"))
        self.assertEqual(guard.tsquery("Fix the CI timeout; DROP TABLE x -- 12 retry!"), "fix | timeout | drop | table | retry")
        self.assertEqual(guard.tsquery("'; select"), "select")


class CaseTests(unittest.TestCase):
    comments = [{"body": "plan"}, {"body": "## What's changed\n- backoff\n\n## Why changed\nsite blocks us\n\n## Study cases\nFlareSolverr 403 on the first call"}]

    def test_handover_becomes_a_verified_case(self):
        built = cases.build_case("JAVM-82", "Crawl retry", "JOOservices", "javmeta", "Bug", "Codex", "https://github.com/x/pull/1", self.comments, 2, "CI failed")
        self.assertEqual((built["kind"], built["status"], built["project"], built["jira_key"]), ("case", "verified", "JAVM", "JAVM-82"))
        for text in ("backoff", "site blocks us", "FlareSolverr 403", "Rework rounds: 2", "pull/1"):
            self.assertIn(text, built["body"])
        self.assertIn("codex", built["tags"])

    def test_without_a_handover_nothing_is_written(self):
        self.assertIsNone(cases.build_case("A-1", "s", "J", "r", "", "", "", [{"body": "just a note"}], 0))
        self.assertIsNone(cases.build_case("A-1", "s", "J", "r", "", "", "", [{"body": "## What's changed\nNone\n## Study cases\nNone"}], 0))

    def test_memory_lines_and_prompt_block(self):
        text = "done\nMEMORY: lesson | Mind the cache | Flushing the cache drops the locks\nMEMORY: bogus | x | y\nMEMORY: fact | A | B too short? no"
        self.assertEqual([k for k, _, _ in cases.lessons(text)], ["lesson", "fact"])
        self.assertIn("nothing recorded", cases.prompt_block([]))
        block = cases.prompt_block([dict(kind="case", title="T", project="JAVM", namespace="J", status="verified", id="12345678-aaaa", body="line\n  two")])
        self.assertIn("[case] T (JAVM, verified, id 12345678)", block)
        self.assertIn("line two", block)


class FakeStore:
    def __init__(self):
        self.calls, self.fail, self.entries = [], False, {}

    def guard(self):
        if self.fail:
            raise OSError("db down")

    def ping(self):
        self.guard()
        return True

    def search(self, query, namespace, project, repo, kinds, statuses, limit, touch, body_chars):
        self.guard()
        self.calls.append(("search", namespace, project, statuses))
        return [{"id": "1", "title": "t", "body": "b", "kind": "fact", "namespace": namespace}]

    def put(self, data, actor, status):
        self.guard()
        guard.check_entry(data)
        self.calls.append(("put", actor, status, data.get("namespace")))
        return "11111111-1111-1111-1111-111111111111", True

    def get(self, entry_id, namespaces=None):
        self.guard()
        if entry_id.startswith("0"):
            raise NotFound(entry_id)
        return {"id": entry_id, "namespace": "JOOservices"}

    def feedback(self, entry_id, actor, jira_key, helpful):
        self.calls.append(("feedback", helpful))

    def verify(self, entry_id, actor):
        self.calls.append(("verify", actor))

    def promote(self, entry_id, actor):
        self.calls.append(("promote", actor))

    def decay(self, entry_id, actor, reason):
        self.calls.append(("decay", reason))

    def curate(self, actor):
        self.calls.append(("curate", actor))
        return {"verified": 1}

    def stats(self):
        return {"entries": []}


ID = "22222222-2222-2222-2222-222222222222"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        tokens = Tokens([{"name": "ai", "token": "ai-token", "role": "client", "namespaces": ["JOOservices"]},
                         {"name": "sup", "token": "admin-token", "role": "admin", "namespaces": ["*"]}])
        self.server = make_server(self.store, tokens, log=lambda _m: None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, token="ai-token"):
        request = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_health_needs_no_token_and_everything_else_does(self):
        self.assertEqual(self.call("GET", "/health", token=None)[1], {"ok": True, "db": True})
        self.assertEqual(self.call("GET", "/v1/stats", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/v1/stats", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/v1/stats")[0], 200)

    def test_a_client_stays_inside_its_namespace(self):
        self.assertEqual(self.call("POST", "/v1/search", {"query": "x", "namespace": "JOOservices"})[0], 200)
        self.assertEqual(self.call("POST", "/v1/search", {"query": "x", "namespace": "SGCarmart"})[0], 403)
        self.assertEqual(self.call("POST", "/v1/entries", entry(namespace="SGCarmart"))[0], 403)
        self.assertEqual(self.call("POST", "/v1/search", {"query": "x", "namespace": "SGCarmart"}, token="admin-token")[0], 200)

    def test_a_client_cannot_see_unverified_entries_or_write_global_ones(self):
        self.call("POST", "/v1/search", {"query": "x", "namespace": "JOOservices", "statuses": ["fresh"]})
        self.assertIsNone(self.store.calls[-1][3])  # the status filter was ignored
        self.assertEqual(self.call("POST", "/v1/entries", entry(scope="global"))[0], 403)

    def test_a_client_proposes_fresh_and_only_an_admin_sets_the_status(self):
        self.assertEqual(self.call("POST", "/v1/entries", entry(status="promoted"))[0], 201)
        self.assertEqual(self.store.calls[-1], ("put", "ai", "fresh", "JOOservices"))
        self.call("POST", "/v1/entries", entry(status="verified"), token="admin-token")
        self.assertEqual(self.store.calls[-1], ("put", "sup", "verified", "JOOservices"))

    def test_only_an_admin_changes_the_life_of_an_entry(self):
        for action in ("verify", "promote", "decay"):
            self.assertEqual(self.call("POST", f"/v1/entries/{ID}/{action}", {})[0], 403)
            self.assertEqual(self.call("POST", f"/v1/entries/{ID}/{action}", {"reason": "old"}, "admin-token")[0], 200)
        self.assertEqual(self.call("POST", "/v1/curate", {})[0], 403)
        self.assertEqual(self.call("POST", "/v1/curate", {}, "admin-token")[1], {"verified": 1})
        self.assertEqual(self.call("POST", f"/v1/entries/{ID}/feedback", {"jira_key": "A-1", "helpful": True})[0], 200)

    def test_errors_have_the_right_codes(self):
        self.assertEqual(self.call("POST", "/v1/entries", entry(body="password = hunter2hunter2"))[0], 422)
        self.assertEqual(self.call("GET", "/v1/entries/00000000-0000-0000-0000-000000000000")[0], 404)
        self.assertEqual(self.call("GET", "/v1/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/v1/search", [1])[0], 400)
        self.store.fail = True
        self.assertEqual(self.call("GET", "/v1/entries/" + ID)[0], 503)
        self.assertEqual(self.call("GET", "/health", token=None)[1], {"ok": True, "db": False})


class ClientTests(unittest.TestCase):
    def memory(self, opener):
        self.tmp = tempfile.TemporaryDirectory()
        return Memory("http://m", "tok", Path(self.tmp.name) / "spool.jsonl", opener=opener)

    def test_a_dead_service_gives_no_answer_and_is_not_asked_again_at_once(self):
        calls = []

        def opener(request, timeout):
            calls.append(1)
            raise urllib.error.URLError("refused")
        memory = self.memory(opener)
        self.assertEqual(memory.search("x"), [])
        self.assertEqual(memory.search("y"), [])
        self.assertEqual(len(calls), 1)

    def test_a_proposal_is_kept_while_the_service_is_down_and_sent_later(self):
        state = {"up": False, "sent": []}

        class Response:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(self.data).encode()

        def opener(request, timeout):
            if not state["up"]:
                raise urllib.error.URLError("refused")
            state["sent"].append(json.loads(request.data)["title"])
            return Response({"id": "x", "created": True})
        memory = self.memory(opener)
        self.assertIsNone(memory.propose(entry(title="first one")))
        memory.down_until = 0
        self.assertIsNone(memory.propose(entry(title="second one")))
        self.assertEqual(len(memory.spool.read_text().splitlines()), 2)
        state["up"], memory.down_until = True, 0
        self.assertEqual(memory.flush(), 2)
        self.assertEqual(state["sent"], ["first one", "second one"])
        self.assertEqual(memory.spool.read_text(), "")

    def test_disabled_client_does_nothing(self):
        memory = Memory("", "")
        self.assertFalse(memory.enabled)
        self.assertEqual(memory.search("x"), [])
        self.assertIsNone(memory.propose(entry()))
        with self.assertRaises(Unavailable):
            memory.get("x")


class FakeMemory:
    enabled = True

    def __init__(self, items=()):
        self.items, self.proposed, self.feedbacks, self.searches = list(items), [], [], []

    def search(self, query, namespace="", project="", repo="", kinds=None, limit=6, touch=True):
        self.searches.append((query, namespace, project, repo))
        return self.items

    def propose(self, entry):
        self.proposed.append(entry)
        return "abc12345-0000"

    def feedback(self, entry_id, jira_key, helpful):
        self.feedbacks.append((entry_id, jira_key, helpful))


class SupervisorMemoryTests(unittest.TestCase):
    def build(self, items=()):
        sup = make()
        sup.memory = FakeMemory(items)
        sup.jira.comments = lambda key, last=6: CaseTests.comments
        sup.jira.view = lambda key, full=False: {"summary": "Crawl retry", "type": "Bug"}
        sup.jira.read_field = lambda key, field: None
        return sup

    def repo(self):
        from types import SimpleNamespace
        return SimpleNamespace(name="javmeta")

    def test_recall_searches_by_the_ticket_and_remembers_what_it_showed(self):
        item = dict(id="aaaaaaaa-1", kind="case", title="Old retry case", project="JAVM", namespace="JOOservices", status="verified", body="use backoff")
        sup = self.build([item])
        view = {"summary": "Fix retry storms", "description": "## AC\nretry waits 60 seconds\n## Scopes\nCrawlJob"}
        text = sup.recall("JAIF-5", view, self.repo())
        self.assertIn("Old retry case", text)
        self.assertIn("retry waits 60 seconds", sup.memory.searches[0][0])
        self.assertEqual(sup.memory.searches[0][1:], ("JOOservices", "JAIF", "javmeta"))
        self.assertEqual(json.loads(sup.redis.get("recalled:JAIF-5")), ["aaaaaaaa-1"])

    def test_without_a_memory_service_the_prompt_says_so(self):
        sup = self.build()
        sup.memory = type("Off", (), {"enabled": False})()
        self.assertIn("not configured", sup.recall("JAIF-5", {"summary": "x"}, self.repo()))

    def test_ai_lessons_are_proposed_as_entries_of_the_ticket(self):
        sup = self.build()
        sup.remember_lessons("JAIF-5", self.repo(), "Claude", "done\nMEMORY: lesson | Cache traps | Flushing drops the locks, avoid it")
        proposal = sup.memory.proposed[0]
        self.assertEqual((proposal["kind"], proposal["jira_key"], proposal["created_by"], proposal["namespace"]), ("lesson", "JAIF-5", "Claude", "JOOservices"))

    def test_done_writes_a_case_and_credits_the_recalled_entries_when_no_rework(self):
        sup = self.build()
        sup.redis.set("recalled:JAIF-5", json.dumps(["e1", "e2"]), 10)
        sup.remember_case("JAIF-5", self.repo(), {"url": "https://x/pull/9"})
        case = sup.memory.proposed[0]
        self.assertEqual((case["kind"], case["status"], case["jira_key"]), ("case", "verified", "JAIF-5"))
        self.assertEqual(sup.memory.feedbacks, [("e1", "JAIF-5", True), ("e2", "JAIF-5", True)])

    def test_rework_gives_no_feedback(self):
        sup = self.build()
        sup.redis.set("recalled:JAIF-5", json.dumps(["e1"]), 10)
        sup.redis.set("cycles:JAIF-5", "2", 10)
        sup.remember_case("JAIF-5", self.repo(), {})
        self.assertEqual(sup.memory.feedbacks, [])
        self.assertIn("Rework rounds: 2", sup.memory.proposed[0]["body"])

    def test_the_ai_gets_its_own_read_token_and_ticket_defaults(self):
        sup = self.build()
        self.assertEqual(sup.memory_env("JAIF-5", self.repo()), {})
        sup.cfg = sup.cfg.__class__(**{**sup.cfg.__dict__, "memory": {"AIFLOW_MEMORY_URL": "http://m", "AIFLOW_MEMORY_AI_TOKEN": "ai", "AIFLOW_MEMORY_TOKEN": "admin"}})
        env = sup.memory_env("JAIF-5", self.repo())
        self.assertEqual((env["AIFLOW_MEMORY_TOKEN"], env["AIFLOW_MEMORY_NAMESPACE"], env["AIFLOW_MEMORY_PROJECT"]), ("ai", "JOOservices", "JAIF"))
        self.assertNotIn("admin", env.values())

    def test_the_control_sends_the_spool_and_curates_every_half_hour(self):
        from aiflow.control import memory_tasks

        class M(FakeMemory):
            def flush(self):
                return 2

            def curate(self):
                return {"verified": 1}
        redis = FakeRedis()
        self.assertEqual(memory_tasks(M(), redis, "ctl"), {"sent": 2, "verified": 1})
        self.assertEqual(memory_tasks(M(), redis, "ctl"), {"sent": 2})  # curated a moment ago
