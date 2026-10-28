import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiflow import agents, events, usage
from aiflow.events import Events
from aiflow.health import usage_summary
from tests.test_events import Sender
from tests.test_supervisor import FakeJira, FakeRedis, make, stamp

CLAUDE = json.dumps({"type": "result", "result": "RESULT: DONE", "num_turns": 21, "total_cost_usd": 0.19622, "usage": {
    "input_tokens": 28, "output_tokens": 4747, "cache_read_input_tokens": 480673, "cache_creation_input_tokens": 25157,
    "output_tokens_details": {"thinking_tokens": 359}}, "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 28}}})
CODEX_JSON = "\n".join(json.dumps(e) for e in (
    {"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
    {"type": "turn.completed", "usage": {"input_tokens": 16809, "cached_input_tokens": 1000, "cache_write_input_tokens": 0, "output_tokens": 17, "reasoning_output_tokens": 10}},
    {"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 100, "output_tokens": 3, "reasoning_output_tokens": 0}}))
OPENCODE = "\n".join(json.dumps(e) for e in (
    {"type": "step_start", "part": {"type": "step-start"}}, {"type": "text", "part": {"type": "text", "text": "RESULT: DONE"}},
    {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 16865, "input": 15071, "output": 2, "reasoning": 0, "cache": {"write": 5, "read": 1792}}, "cost": 0.5}},
    {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 100, "input": 90, "output": 10, "reasoning": 0, "cache": {"write": 0, "read": 0}}, "cost": 0.25}}))


class ParseTests(unittest.TestCase):
    def test_claude_result_event(self):
        got = usage.parse("Claude", "noise\n" + CLAUDE + "\n")
        self.assertEqual((got["input"], got["output"], got["cache_read"], got["cache_write"], got["reasoning"]), (28, 4747, 480673, 25157, 359))
        self.assertEqual((got["total"], got["cost_usd_list"], got["turns"], got["model"], got["usage_known"]), (28 + 4747 + 480673 + 25157, 0.19622, 21, "claude-sonnet-5-5", True))

    def test_codex_json_turns_are_added_and_cached_is_split_out(self):
        got = usage.parse("Codex", CODEX_JSON, model="gpt-6-luna")
        self.assertEqual((got["input"], got["cache_read"], got["output"], got["reasoning"], got["total"]), (16809 + 200 - 1100, 1100, 20, 10, 17009 + 20))
        self.assertEqual((got["cost_usd_list"], got["cost_known"], got["cost_model"]), (0.001612, True, "gpt-6-luna"))

    def test_codex_cost_estimate_separates_cache_writes_and_unknown_models_stay_unknown(self):
        text = json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 20, "cache_write_input_tokens": 10,
            "output_tokens": 5, "reasoning_output_tokens": 2}})
        got = usage.parse("Codex", text, model="gpt-6-luna")
        self.assertEqual((got["input"], got["cache_read"], got["cache_write"], got["output"], got["total"]), (70, 20, 10, 5, 105))
        self.assertEqual((got["cost_usd_list"], got["cost_known"]), (0.000011, True))
        unknown = usage.parse("Codex", text, model="unlisted-model")
        self.assertEqual((unknown["cost_usd_list"], unknown["cost_known"]), (None, False))

    def test_codex_plain_log_gives_the_total_only(self):
        got = usage.parse("Codex", "work\ntokens used\n64,945\n")
        self.assertEqual((got["total"], got["input"], got["usage_known"], got["cost_known"]), (64945, 0, True, False))

    def test_opencode_steps_are_added(self):
        got = usage.parse("OpenCode", OPENCODE)
        self.assertEqual((got["total"], got["input"], got["output"], got["cache_read"], got["cache_write"], got["cost_usd_list"], got["turns"]), (16965, 15161, 12, 1792, 5, 0.75, 2))

    def test_unknown_or_missing_reports_are_marked_not_guessed(self):
        self.assertEqual(usage.parse("Grok", "anything"), {"usage_known": False})
        self.assertEqual(usage.parse("Claude", "stopped before the result"), {"usage_known": False})
        self.assertEqual(usage.parse("Codex", "{broken json"), {"usage_known": False})
        self.assertEqual(usage.parse("Nobody", ""), {"usage_known": False})

    def test_malformed_codex_event_shapes_are_unknown(self):
        for payload in (None, [], "not usage", 7):
            with self.subTest(payload=payload):
                text = json.dumps({"type": "turn.completed", "usage": payload})
                self.assertEqual(usage.parse("Codex", text), {"usage_known": False})
        self.assertEqual(usage.parse("Codex", "[]"), {"usage_known": False})

    def test_report_emits_one_event_with_the_numbers_and_the_run(self):
        sink = Events("http://obs", "default", "s", "u", "p", {}, Sender(), thread=False)
        events._sink = sink
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "run.log"
                log.write_text(CLAUDE)
                got = usage.report("Claude", log, ticket="JAIF-5", role="developer", minutes=3.5)
            sink.flush()
        finally:
            events._sink = None
        row = sink.sender.calls[0][1][0]
        self.assertEqual((row["event"], row["ticket"], row["agent"], row["role"], row["output"], row["usage_known"]), ("ai.usage", "JAIF-5", "Claude", "developer", 4747, True))
        self.assertEqual(row["run_id"], "run")
        self.assertEqual(got["total"], row["total"])


class ReportWithTheRequestedModelTests(unittest.TestCase):
    """Regression: the supervisor passes `model=` and every parser returns a `model` too; both must reach the event, never raise."""

    def run_report(self, agent, text):
        sink = Events("http://obs", "default", "s", "u", "p", {}, Sender(), thread=False)
        events._sink = sink
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "run.log"
                log.write_text(text)
                usage.report(agent, log, ticket="JAIF-5", role="developer", model="sonnet", effort="", why="ok", code=0, minutes=1.0)
            sink.flush()
        finally:
            events._sink = None
        return sink.sender.calls[0][1][0]

    def test_claude_codex_opencode_and_unknown_logs_all_report(self):
        for agent, text, answered in (("Claude", CLAUDE, "claude-sonnet-5-5"), ("Codex", CODEX_JSON, ""), ("OpenCode", OPENCODE, ""), ("Grok", "x", None)):
            with self.subTest(agent):
                row = self.run_report(agent, text)
                self.assertEqual(row["model_requested"], "sonnet")
                if answered is not None:
                    self.assertEqual(row["model"], answered)

    def test_malformed_codex_usage_report_is_emitted_as_unknown(self):
        row = self.run_report("Codex", json.dumps({"type": "turn.completed", "usage": []}))
        self.assertEqual(row["event"], "ai.usage")
        self.assertFalse(row["usage_known"])


class CommandTests(unittest.TestCase):
    def test_codex_and_opencode_ask_for_json(self):
        codex, _ = agents.build_command("Codex", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"))
        self.assertIn("--json", codex)
        self.assertEqual(codex[-1], "-")  # the prompt still comes on stdin
        opencode, _ = agents.build_command("OpenCode", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"))
        self.assertEqual(opencode[:4], ["opencode", "run", "--format", "json"])

    def test_the_final_text_of_opencode_comes_from_its_text_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "l.log"
            log.write_text(OPENCODE)
            self.assertEqual(agents.final_text("OpenCode", log, Path(tmp) / "none"), "RESULT: DONE")
            self.assertEqual(agents.verdict(agents.final_text("OpenCode", log, Path(tmp) / "none")), ("DONE", ""))

    def test_opencode_plain_text_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "l.log"
            log.write_text("some text\nRESULT: DONE\n")
            self.assertIn("RESULT: DONE", agents.final_text("OpenCode", log, Path(tmp) / "none"))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.sink = Events("http://obs", "default", "s", "u", "p", {}, Sender(), thread=False)
        events._sink = self.sink
        self.sup = make()

    def tearDown(self):
        events._sink = None

    def rows(self, name="ticket.status"):
        self.sink.flush()
        return [r for _, rows, _ in self.sink.sender.calls for r in rows if r["event"] == name]

    def test_a_move_by_the_supervisor_records_from_to_and_the_time_in_the_old_status(self):
        self.sup.record_status("JAIF-1", "To Do", "observed")
        self.sup.redis.set("statussince:JAIF-1", str(__import__("time").time() - 120), 10)
        self.sup.record_status("JAIF-1", "In Progress", "supervisor")
        first, second = self.rows()
        self.assertEqual((first["from_status"] if "from_status" in first else "", first["to_status"]), ("", "To Do"))
        self.assertEqual((second["from_status"], second["to_status"], second["source"]), ("To Do", "In Progress", "supervisor"))
        self.assertAlmostEqual(second["minutes_in_from"], 2.0, delta=0.1)

    def test_the_same_status_is_not_reported_twice(self):
        self.sup.record_status("JAIF-1", "To Do", "observed")
        self.sup.record_status("JAIF-1", "To Do", "observed")
        self.assertEqual(len(self.rows()), 1)

    def test_status_and_start_time_are_stored_without_expiry(self):
        with mock.patch.object(self.sup.redis, "set_persistent", wraps=self.sup.redis.set_persistent) as persistent, \
                mock.patch.object(self.sup.redis, "set", wraps=self.sup.redis.set) as expiring:
            self.sup.record_status("JAIF-7", "In Progress", "supervisor")
        self.assertEqual([call.args[0] for call in persistent.call_args_list], ["status:JAIF-7", "statussince:JAIF-7"])
        expiring.assert_not_called()

    def test_a_move_the_owner_made_is_noticed_once_across_workers(self):
        issue = {"key": "JAIF-2", "status": "To Do", "updated": stamp(0), "labels": []}
        self.sup.observe([issue])
        other = make(redis=self.sup.redis)
        other.observe([issue])  # a second worker sees the same
        self.assertEqual([(r["ticket"], r["to_status"], r["source"]) for r in self.rows()], [("JAIF-2", "To Do", "observed")])
        issue["status"] = "Blocked"
        other.observe([issue])
        self.sup.observe([issue])
        self.assertEqual([r["to_status"] for r in self.rows()], ["To Do", "Blocked"])

    def test_a_stale_search_result_is_not_a_status_change(self):
        self.sup.record_status("JAIF-3", "In Review", "supervisor")  # we just moved it
        stale = {"key": "JAIF-3", "status": "In Progress", "updated": stamp(3600), "labels": []}  # the index still shows the old status
        self.sup.observe([stale])
        self.assertEqual([r["to_status"] for r in self.rows()], ["In Review"])

    def test_the_jira_transition_hook_feeds_the_record(self):
        from aiflow.jira import Jira
        jira = Jira(Path("/tmp"))
        jira._run = lambda *a: "{}"
        sup = make()
        sup.jira = jira
        jira.on_transition = lambda key, to: sup.record_status(key, to, "supervisor")
        jira.transition("JAIF-4", "Done")
        self.assertEqual([r["to_status"] for r in self.rows()], ["Done"])


class SpoolAndSummaryTests(unittest.TestCase):
    def test_delivery_failure_spools_rows_before_shutdown_and_a_new_process_replays_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "events.jsonl"
            down = Events("http://obs", "default", "s", "u", "p", {}, Sender(fail=99), thread=False, spool=spool)
            down.emit("a")
            down.emit("b")
            self.assertFalse(down.flush())
            self.assertEqual(len(spool.read_text().splitlines()), 2)
            down.close()
            up = Sender()
            fresh = Events("http://obs", "default", "s", "u", "p", {}, up, thread=False, spool=spool)
            fresh.flush()
            self.assertEqual([r["event"] for r in up.rows()], ["a", "b"])
            self.assertFalse(spool.exists())

    def test_closing_persists_unsent_rows_without_waiting_for_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "events.jsonl"
            sender = Sender(fail=99)
            sink = Events("http://obs", "default", "s", "u", "p", {}, sender, thread=False, spool=spool)
            sink.emit("pending")
            sink.close()
            self.assertEqual([json.loads(line)["event"] for line in spool.read_text().splitlines()], ["pending"])
            self.assertEqual(sender.calls, [])

    def test_the_daily_summary_shows_tokens_and_list_price(self):
        redis = FakeRedis()
        redis.set("usage:2026-10-19:JAIF-1:developer:Codex:1", json.dumps({"key": "JAIF-1", "role": "developer", "ai": "Codex", "minutes": 10, "why": "ok", "tokens": 120000, "cost": 0}), 10)
        redis.set("usage:2026-10-19:JAIF-2:qa:Claude:1", json.dumps({"key": "JAIF-2", "role": "qa", "ai": "Claude", "minutes": 4, "why": "ok", "tokens": 500000, "cost": 0.4}), 10)
        text = usage_summary(redis, "2026-10-19")
        self.assertIn("Codex developer: 1 run(s), 10 min, 0 not clean, 120,000 tokens", text)
        self.assertIn("500,000 tokens (list price $0.40)", text)
