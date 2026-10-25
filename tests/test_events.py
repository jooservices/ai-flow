import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiflow import events
from aiflow.events import Events


class Sender:
    def __init__(self, fail=0):
        self.calls = []
        self.fail = fail

    def __call__(self, url, body, auth):
        if self.fail:
            self.fail -= 1
            raise OSError("down")
        self.calls.append((url, json.loads(body), auth))

    def rows(self):
        return [row for _, rows, _ in self.calls for row in rows]


def make(sender=None, **kw):
    sender = sender or Sender()
    sink = Events("http://obs:5080/", "default", "aiflow_events", "user@x", "secret", {"machine": "wsl", "worker": "JAVM-2"}, sender, thread=False, **kw)
    return sink, sender


class EventTests(unittest.TestCase):
    def test_a_row_carries_the_base_fields_a_timestamp_and_the_event_name(self):
        sink, sender = make()
        sink.emit("ticket.pickup", ticket="JAVM-1", role="developer", rework=False, minutes=1.5)
        self.assertEqual(sender.calls, [])  # emit never waits for delivery
        self.assertTrue(sink.flush())
        url, rows, auth = sender.calls[0]
        self.assertEqual(url, "http://obs:5080/api/default/aiflow_events/_json")
        self.assertTrue(auth)
        row = rows[0]
        self.assertEqual((row["event"], row["machine"], row["worker"], row["ticket"], row["project"]), ("ticket.pickup", "wsl", "JAVM-2", "JAVM-1", "JAVM"))
        self.assertEqual((row["rework"], row["minutes"]), (False, 1.5))
        self.assertGreater(row["_timestamp"], 1_000_000_000_000_000)

    def test_the_ticket_is_found_in_a_log_message_and_none_values_are_left_out(self):
        sink, sender = make()
        sink.emit("log", message="JOOWT-12 [To Do] developer -> Codex", nothing=None)
        sink.flush()
        row = sender.rows()[0]
        self.assertEqual((row["ticket"], row["project"]), ("JOOWT-12", "JOOWT"))
        self.assertNotIn("nothing", row)

    def test_long_text_is_cut_and_nested_values_become_text(self):
        sink, sender = make()
        sink.emit("x", text="a" * 5000, items=[1, 2])
        sink.flush()
        row = sender.rows()[0]
        self.assertEqual(len(row["text"]), 2000)
        self.assertEqual(row["items"], "[1, 2]")

    def test_ai_run_id_is_derived_from_the_log_file(self):
        sink, sender = make()
        sink.emit("ai.run", log_file="/runs/JAIF-1-developer-123.jsonl")
        sink.flush()
        self.assertEqual(sender.rows()[0]["run_id"], "JAIF-1-developer-123")

    def test_batches_are_sent_in_order(self):
        sink, sender = make(batch=2)
        for n in range(5):
            sink.emit("n", n=n)
        sink.flush()
        self.assertEqual([len(rows) for _, rows, _ in sender.calls], [2, 2, 1])
        self.assertEqual([r["n"] for r in sender.rows()], [0, 1, 2, 3, 4])

    def test_emit_remains_responsive_while_the_background_sender_is_blocked(self):
        started, release = threading.Event(), threading.Event()

        def blocked_sender(*_):
            started.set()
            release.wait(2)

        with tempfile.TemporaryDirectory() as tmp:
            sink = Events("http://obs", "default", "s", "u", "p", {}, blocked_sender,
                          flush_seconds=10, thread=True, spool=Path(tmp) / "events.jsonl")
            try:
                sink.emit("first")
                self.assertTrue(started.wait(1))
                sink.emit("second")
                self.assertEqual(len(sink.queue), 1)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                sink.close()

    def test_a_failure_keeps_the_rows_for_the_next_try(self):
        sink, sender = make(Sender(fail=1))
        sink.emit("a")
        sink.emit("b")
        self.assertFalse(sink.flush())
        self.assertEqual(sender.rows(), [])
        self.assertTrue(sink.flush())
        self.assertEqual([r["event"] for r in sender.rows()], ["a", "b"])

    def test_the_queue_never_drops_events_when_delivery_is_unavailable(self):
        sink, sender = make(Sender(fail=1))
        for n in range(5):
            sink.emit("n", n=n)
        self.assertEqual(len(sink.queue), 5)
        self.assertFalse(sink.flush())
        sender.fail = 0
        sink.flush()
        self.assertEqual([r["n"] for r in sender.rows()], [0, 1, 2, 3, 4])

    def test_without_url_user_or_password_nothing_is_sent(self):
        for args in (("", "u", "p"), ("http://x", "", "p"), ("http://x", "u", "")):
            sender = Sender()
            sink = Events(args[0], "default", "s", args[1], args[2], {}, sender, thread=False)
            sink.emit("x")
            self.assertTrue(sink.flush())
            self.assertFalse(sink.enabled)
            self.assertEqual(sender.calls, [])

    def test_emit_never_raises(self):
        sink, _ = make()
        sink.emit("x", bad=object())
        sink.queue = None  # broken on purpose
        sink.emit("y")  # must not raise

    def test_the_module_function_is_a_no_op_until_configured_and_then_sends(self):
        events._sink = None
        events.emit("x")
        sent = Sender()
        with mock.patch.object(events, "post", sent):
            sink = events.configure({"AIFLOW_OBS_URL": "http://obs", "AIFLOW_OBS_USER": "u", "AIFLOW_OBS_PASSWORD": "p", "AIFLOW_OBS_STREAM": "dev"},
                                    "wsl", "control")
            sink.sender = sent
            events.emit("control.start")
            sink.flush()
            events.close()
        self.assertEqual(sent.rows()[0]["worker"], "control")
        self.assertTrue(sent.calls[0][0].endswith("/api/default/dev/_json"))
        events._sink = None


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.sink, self.sender = make()
        events._sink = self.sink

    def tearDown(self):
        events._sink = None

    def names(self):
        self.sink.flush()
        return [r["event"] for r in self.sender.rows()]

    def test_the_journal_line_is_also_an_event(self):
        from aiflow.supervisor import log
        log("JAIF-9 something happened")
        self.sink.flush()
        row = self.sender.rows()[0]
        self.assertEqual((row["event"], row["ticket"]), ("log", "JAIF-9"))

    def test_jira_writes_emit(self):
        from aiflow.jira import Jira
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".tmp").mkdir()
            jira = Jira(root)
            jira._run = lambda *a: "{}"
            self.assertTrue(jira.transition("JAIF-1", "Done"))
            jira.comment("JAIF-1", "hello")
            jira.link("JAIF-1", "JAIF-2")
            jira.set_agent("JAIF-1", "Claude")
        self.assertEqual(self.names(), ["jira.transition", "jira.comment", "jira.link", "jira.agent"])

    def test_a_failed_transition_is_an_event_too(self):
        from aiflow.jira import Jira, JiraError
        from pathlib import Path
        jira = Jira(Path("/tmp"))

        def boom(*a):
            raise JiraError("no such transition")
        jira._run = boom
        self.assertFalse(jira.transition("JAIF-1", "Nowhere"))
        self.sink.flush()
        row = self.sender.rows()[0]
        self.assertEqual((row["ok"], row["to"]), (False, "Nowhere"))

    def test_github_writes_emit(self):
        from aiflow.github import GitHub
        gh = GitHub("o/r")
        gh._gh = lambda *a, **k: "[]"
        gh.ready(5)
        gh.merge(5, "abcdef0123456789")
        gh.update_branch(5, "abcdef0123456789")
        gh.comment(5, "hi")
        self.assertEqual(self.names(), ["github.pr_ready", "github.merge", "github.update_branch", "github.pr_comment"])
