"""The store against a real PostgreSQL database supplied by the test environment."""
import sqlite3
import unittest

try:
    import psycopg  # noqa: F401
except ImportError:
    psycopg = None

from aiflow.memory import guard
from aiflow.memory.scoped import Scoped
from aiflow.memory.store import NotFound, Store
from tests.memory_pg_support import MemoryTestDatabase, database_url

@unittest.skipIf(psycopg is None, "psycopg is not installed")
class StorePgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = MemoryTestDatabase(database_url())
        cls.store = cls.database.store

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "database"):
            cls.database.close()

    def setUp(self):
        self.store.run("truncate memory_feedback, memory_event, memory_entry cascade", fetch=False)

    def put(self, status="verified", **kw):
        base = dict(kind="case", scope="project", namespace="JOOservices", project="JAVM", repo="javmeta", title="Crawl retried forever on 403",
                    body="Retries had no backoff; use 60, 120 and 180 seconds delays and fail after the third block.", tags=["crawl"], created_by="Codex")
        return self.store.put({**base, **kw}, "test", status)

    def titles(self, query, **kw):
        return [i["title"] for i in self.store.search(query, **{"namespace": "JOOservices", "project": "JAVM", **kw})]

    def test_search_finds_by_words_and_ranks_the_project_first(self):
        self.put()
        self.put(title="Unrelated cache note", body="Flush only the test index.", project="JOOCR")
        self.assertEqual(self.titles("crawl job blocked retry backoff"), ["Crawl retried forever on 403"])
        self.assertEqual(self.titles("cache"), [])  # JOOCR is another project: invisible without asking for it
        self.assertEqual(self.titles("cache", project="JOOCR"), ["Unrelated cache note"])

    def test_the_same_content_confirms_instead_of_duplicating(self):
        first, created = self.put(status="fresh")
        again, created_again = self.put(status="fresh", created_by="Claude")
        self.assertEqual((first == again, created, created_again), (True, True, False))
        self.assertEqual(self.store.get(first)["confirmations"], 2)

    def test_fresh_entries_are_invisible_until_confirmed_then_the_curator_verifies(self):
        entry_id, _ = self.put(status="fresh")
        self.assertEqual(self.titles("crawl retry"), [])
        self.put(status="fresh", created_by="Claude")  # a second AI proposes the same
        self.assertEqual(self.store.curate()["verified"], 1)
        self.assertEqual(self.store.get(entry_id)["status"], "verified")
        self.assertEqual(self.titles("crawl retry"), ["Crawl retried forever on 403"])

    def test_other_companies_never_see_each_other(self):
        self.put()
        self.put(namespace="SGCarmart", project="CARS", title="SGCarmart crawl retry secret plan", body="crawl retry backoff of the other company")
        self.put(scope="global", namespace="", project="", title="Global crawl rule", body="Every crawl needs a retry limit.")
        seen = self.titles("crawl retry")
        self.assertEqual(sorted(seen), ["Crawl retried forever on 403", "Global crawl rule"])
        self.assertEqual(sorted(self.titles("crawl retry", namespace="SGCarmart", project="CARS")), ["Global crawl rule", "SGCarmart crawl retry secret plan"])
        other = self.store.search("secret plan", namespace="SGCarmart", project="CARS")[0]["id"]
        with self.assertRaises(NotFound):
            self.store.get(other, ["JOOservices"])

    def test_lifecycle_and_feedback_move_an_entry(self):
        entry_id, _ = self.put(status="fresh")
        self.store.verify(entry_id, "test")
        for ticket in ("A-1", "A-2", "A-3"):
            self.store.feedback(entry_id, "test", ticket, True)
        self.assertEqual(self.store.curate()["promoted"], 1)
        self.assertEqual(self.store.get(entry_id)["status"], "promoted")
        self.store.decay(entry_id, "test", "superseded")
        self.assertEqual(self.titles("crawl retry"), [])
        with self.assertRaises(NotFound):
            self.store.promote(entry_id, "test")

    def test_unhelpful_and_stale_entries_decay(self):
        bad, _ = self.put()
        for ticket in ("A-1", "A-2", "A-3"):
            self.store.feedback(bad, "test", ticket, False)
        old, _ = self.put(status="fresh", title="Old unconfirmed idea", body="Nobody confirmed this idea ever.")
        self.store.run("update memory_entry set created_at = now() - interval '40 days' where id = %s", (old,), fetch=False)
        done = self.store.curate()
        self.assertEqual((done["decayed_unhelpful"], done["decayed_stale_fresh"]), (1, 1))

    def test_superseding_decays_the_old_entry(self):
        old, _ = self.put()
        new, _ = self.put(title="Better rule for crawl retries", body="Use exponential backoff capped at three minutes.", supersedes=old)
        self.assertEqual(self.store.get(old)["status"], "decayed")
        self.assertEqual(self.titles("crawl retries backoff"), ["Better rule for crawl retries"])

    def test_nothing_with_a_secret_gets_in_and_the_database_agrees_with_the_scope_rules(self):
        with self.assertRaises(guard.Rejected):
            self.put(body="the password = hunter2hunter2 works")
        with self.assertRaises(Exception):
            self.store.run("insert into memory_entry (kind, scope, title, body, content_hash) values ('fact', 'project', 'abc', 'def', 'x')", fetch=False)

    def test_touch_counts_the_use_and_events_are_kept(self):
        entry_id, _ = self.put()
        self.store.search("crawl retry", namespace="JOOservices", project="JAVM", touch=True)
        self.assertEqual(self.store.get(entry_id)["use_count"], 1)
        self.assertGreaterEqual(self.store.stats()["events"], 1)

    def test_the_text_search_cannot_be_used_for_injection(self):
        self.put()
        self.assertEqual(self.titles("'; drop table memory_entry; --"), [])
        self.assertEqual(self.store.stats()["entries"][0]["count"], 1)

    def test_scoped_entries_projects_and_devices_match_the_sqlite_workspace_rules(self):
        reference = sqlite3.connect(":memory:")
        reference.executescript("""
            create table note(scope text, namespace text, project text, title text, content text, status text);
            create table project(namespace text, name text, description text, stable integer);
            create table device(namespace text, name text, role text, status text, notes text);
        """)
        scoped = Scoped(self.store)
        rows = [("global", "", "", "Global advice", "shared advice"),
                ("namespace", "JOOservices", "", "Company advice", "shared advice"),
                ("project", "JOOservices", "JAIF", "Project advice", "shared advice"),
                ("namespace", "OtherCo", "", "Other company advice", "shared advice")]
        ids = []
        for scope, namespace, project, title, content in rows:
            entry_id = scoped.add(title, content, namespace=namespace or None, project=project or None, global_scope=scope == "global")
            ids.append(entry_id)
            reference.execute("insert into note values (?, ?, ?, ?, ?, 'fresh')", (scope, namespace, project, title, content))
        reference.commit()
        expected = [row[0] for row in reference.execute(
            "select title from note where scope = 'global' or (namespace = ? and (scope = 'namespace' or (scope = 'project' and project = ?))) order by rowid",
            ("JOOservices", "JAIF"))]
        found = scoped.search("advice", namespace="JOOservices", project="JAIF")
        self.assertEqual(sorted(item["title"] for item in found), sorted(expected))
        self.assertNotIn("Other company advice", [item["title"] for item in found])

        scoped.status(ids[2], "verified", namespace="JOOservices", project="JAIF")
        reference.execute("update note set status = 'verified' where title = 'Project advice'")
        self.assertEqual([item["title"] for item in scoped.list(namespace="JOOservices", project="JAIF", status="verified")],
                         [row[0] for row in reference.execute("select title from note where status = 'verified'")])

        scoped.project_update("JOOservices", "JAIF", description="AI Flow project", stable=True)
        reference.execute("insert into project values ('JOOservices', 'JAIF', 'AI Flow project', 1)")
        project = scoped.projects("JOOservices")[0]
        self.assertEqual((project["name"], project["description"], project["stable"]),
                         reference.execute("select name, description, stable from project").fetchone())
        device_id = scoped.device_add("JOOservices", "build-node", role="worker", notes="shared device")
        reference.execute("insert into device values ('JOOservices', 'build-node', 'worker', 'active', 'shared device')")
        scoped.device_update(device_id, "JOOservices", status="maintenance")
        reference.execute("update device set status = 'maintenance' where name = 'build-node'")
        device = scoped.device_list("JOOservices")[0]
        self.assertEqual((device["name"], device["role"], device["status"], device["notes"]),
                         reference.execute("select name, role, status, notes from device").fetchone())
        reference.close()
