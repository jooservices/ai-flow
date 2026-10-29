"""Worker pickup and execution deadlines for the legacy operator's master rescues."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiflow import agents, supervisor_execution
from aiflow.jira import JiraError
from aiflow.rescue import Rescue
from aiflow.supervisor import STAGES
from tests.test_supervisor import FakeRedis, make, stamp


class MasterRescuePickupTests(unittest.TestCase):
    def record(self, **extra):
        value = dict(state="active", started=1000, budget=30, model="opus:max",
                     label_pending=False, handover_audit_pending=False)
        value.update(extra)
        return value

    def issue(self, **extra):
        return {"key": "JAIF-9", "status": "Handover", "updated": stamp(1), "labels": ["master-rescue"], **extra}

    def test_handover_stage_dispatches_only_an_active_record_to_a_reserved_claude_worker(self):
        redis = FakeRedis()
        sup = make(redis=redis, AIFLOW_AGENT_ORDER="Codex")
        issue = self.issue()
        sup.jira.found = [issue]
        sup.jira.status = "Handover"
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record(started=time.time())))
        redis.set_persistent("probe:test:Claude", "ok")
        sup.process = mock.Mock(return_value=True)

        with mock.patch("aiflow.supervisor.agents.available", return_value=["Claude"]):
            self.assertTrue(sup.tick())

        self.assertEqual(STAGES["Handover"], "master_rescue")
        args = sup.process.call_args.args
        self.assertEqual((args[0]["key"], args[2], args[3]), ("JAIF-9", "master_rescue", "Claude"))
        self.assertEqual(redis.session_count("test", "Claude"), 0)
        self.assertIsNone(redis.get("lease:JAIF-9"))

    def test_handover_candidates_require_the_rescue_label_and_completed_audit(self):
        sup = make()
        cases = (
            (self.issue(labels=[]), self.record()),
            (self.issue(), None),
            (self.issue(), self.record(handover_audit_pending=True)),
            (self.issue(), self.record(started=time.time() - 2000)),
        )
        for issue, record in cases:
            with self.subTest(issue=issue, record=record):
                if record:
                    sup.redis.set_persistent(f"rescue:{issue['key']}", json.dumps(record))
                else:
                    sup.redis.delete(f"rescue:{issue['key']}")
                self.assertFalse(sup.eligible(issue))

    def test_rescue_developer_uses_saved_model_and_absolute_deadline(self):
        sup = make()
        record = self.record(started=1000, budget=30)
        repo = mock.Mock()
        repo.ensure_worktree.return_value = (Path(tempfile.gettempdir()), "JAIF-9-rescue")
        sup.jira.agent = "Claude"
        sup.label_workspace = mock.Mock()
        sup.prepare_ticket_database = mock.Mock(return_value=True)
        sup.execute = mock.Mock(return_value=("lost-lease", "", None, False))
        with tempfile.TemporaryDirectory() as directory:
            sup.replies_file = lambda key: Path(directory) / "replies.json"
            view = {"summary": "Fix a routine task", "agent_field": "Claude"}
            issue = {"key": "JAIF-9", "status": "Handover", "labels": ["master-rescue"]}
            self.assertTrue(sup.develop("JAIF-9", issue, view, repo, "develop", "Claude",
                                        session_owner="session", rescue_record=record))

        options = sup.execute.call_args.kwargs
        self.assertEqual(options["model_override"], ("opus", "max"))
        self.assertEqual(options["rescue_deadline"], 1000 + 30 * 60)

    def test_executor_recalculates_budget_after_delayed_setup(self):
        sup = make()
        sup.redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        repo = mock.Mock()
        repo.name = "ai-flow"
        clock = {"now": 1000.0}

        def prepare_environment(*_args):
            clock["now"] += 25
            return {}

        with tempfile.TemporaryDirectory() as directory:
            sup.replies_file = lambda key: Path(directory) / "replies.json"
            for name, value in (("recall", ""), ("memory_env", {}), ("rework_text", ""), ("pr_text", ""),
                                ("change_type", ""), ("remember_lessons", None), ("log_usage", None)):
                setattr(sup, name, mock.Mock(return_value=value))
            sup.jira.comments = lambda key, last=6: []
            with mock.patch("aiflow.supervisor.time.time", side_effect=lambda: clock["now"]), \
                    mock.patch("aiflow.supervisor.agents.build_command", return_value=(["claude"], None)) as build, \
                    mock.patch("aiflow.supervisor.agents.run_env", side_effect=prepare_environment), \
                    mock.patch("aiflow.supervisor.runner.run", return_value=(0, "ok")) as run, \
                    mock.patch("aiflow.supervisor.usage.report", return_value={}), \
                    mock.patch("aiflow.supervisor.agents.final_text", return_value="RESULT: BLOCKED: missing fixture"), \
                    mock.patch("aiflow.supervisor.agents.verdict", return_value=("BLOCKED", "missing fixture")):
                result = sup.execute("developer", "Claude", "JAIF-9", {"summary": "Routine fix"},
                                     Path(directory), "JAIF-9-fix", "develop", repo,
                                     model_override=("opus", "max"), rescue_deadline=1060)

        self.assertEqual(build.call_args.args[5:7], ("opus", "max"))
        self.assertEqual(run.call_args.kwargs["max_seconds"], 35)
        self.assertEqual(result[2], ("BLOCKED", "missing fixture"))

    def test_fast_finish_after_owner_routing_cannot_start_a_rescue_handoff(self):
        redis = FakeRedis()
        sup = make(redis=redis)
        redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        sup.label_workspace = mock.Mock()
        sup.prepare_ticket_database = mock.Mock(return_value=True)
        sup.finish_master_attempt = mock.Mock(return_value=True)
        repo = mock.Mock()
        repo.ensure_worktree.return_value = (Path(tempfile.gettempdir()), "JAIF-9-rescue")

        def finish_after_owner_route(*_args, **_kwargs):
            redis.delete("lease:JAIF-9")
            return "ok", "private run output", ("DONE", "finished"), False

        sup.execute = mock.Mock(side_effect=finish_after_owner_route)
        with tempfile.TemporaryDirectory() as directory:
            sup.replies_file = lambda key: Path(directory) / "replies.json"
            issue = self.issue()
            view = {"summary": "Routine task", "agent_field": "Claude", "agent": "Claude"}
            self.assertTrue(sup.develop("JAIF-9", issue, view, repo, "develop", "Claude",
                                        session_owner="session", rescue_record=self.record()))

        sup.finish_master_attempt.assert_not_called()
        self.assertIsNone(redis.get("lease:JAIF-9"))

    def test_executor_ignores_a_fast_result_when_owner_routing_deleted_its_lease(self):
        sup = make()
        sup.redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        repo = mock.Mock()
        repo.name = "ai-flow"
        logs, events = [], []

        def finish_after_owner_route(*_args, **_kwargs):
            sup.redis.delete("lease:JAIF-9")
            return 0, "ok"

        with tempfile.TemporaryDirectory() as directory:
            sup.replies_file = lambda key: Path(directory) / "replies.json"
            sup.jira.comments = lambda key, last=6: []
            for name, value in (("recall", ""), ("memory_env", {}), ("rework_text", ""), ("pr_text", ""),
                                ("change_type", "")):
                setattr(sup, name, mock.Mock(return_value=value))
            with mock.patch("aiflow.supervisor.agents.build_command", return_value=(["claude"], None)), \
                    mock.patch("aiflow.supervisor.agents.run_env", return_value={}), \
                    mock.patch("aiflow.supervisor.runner.run", side_effect=finish_after_owner_route), \
                    mock.patch("aiflow.supervisor.usage.report") as report, \
                    mock.patch("aiflow.supervisor.agents.final_text") as final_text, \
                    mock.patch("aiflow.supervisor_execution.events.emit", side_effect=lambda *a, **kw: events.append((a, kw))):
                result = sup.execute("developer", "Claude", "JAIF-9", {"summary": "Routine fix"},
                                     Path(directory), "JAIF-9-fix", "develop", repo)

        self.assertEqual(result, ("lost-lease", "", None, False))
        report.assert_not_called()
        final_text.assert_not_called()
        self.assertEqual(events, [])

    def test_heartbeat_does_not_push_a_branch_after_lease_loss(self):
        sup = make()
        repo = mock.Mock()
        repo.name = "ai-flow"
        beat = supervisor_execution.heartbeat(sup, "developer", "JAIF-9", "Claude", Path("."), "branch", repo,
                                              None, None, 600, lambda message: None)

        self.assertFalse(beat())
        repo.push.assert_not_called()

    def test_rescue_output_is_omitted_from_logs_events_and_jira_comments(self):
        secret = "token=CANARY_SECRET_92_RESCUE_OUTPUT"
        sup = make(redis=FakeRedis())
        sup.redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        repo = mock.Mock()
        repo.name = "ai-flow"
        logs, events, comments = [], [], []
        sup.log_usage = mock.Mock()
        sup.remember_lessons = mock.Mock()

        def log_journal(message):
            logs.append(message)
            supervisor_execution.events.emit("log", message=message)

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch("aiflow.supervisor_execution.usage.report", return_value={}), \
                mock.patch("aiflow.supervisor_execution.agents.final_text", return_value=secret), \
                mock.patch("aiflow.supervisor_execution.agents.verdict", return_value=("DONE", secret)), \
                mock.patch("aiflow.supervisor_execution.agents.looks_like_quota", return_value=False), \
                mock.patch("aiflow.supervisor_execution.events.emit", side_effect=lambda *a, **kw: events.append((a, kw))):
            result = supervisor_execution.finish(sup, "developer", "Claude", "JAIF-9", repo, Path(directory),
                                                 Path(directory) / "run.log", Path(directory) / "run.result",
                                                 "opus", "max", time.monotonic(), 0, "ok", log_journal,
                                                 rescue_run=True)

        self.assertEqual(result[2], ("DONE", secret))
        self.assertNotIn(secret, "\n".join(logs))
        self.assertNotIn(secret, repr(events))
        run_event = next(fields for args, fields in events if args[0] == "ai.run")
        self.assertEqual(run_event["detail"], "")
        sup.remember_lessons.assert_not_called()

        sup.redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        sup.jira.comments = lambda key, last=3: []
        sup.jira.comment = lambda key, body: comments.append(body)
        sup.read_replies = mock.Mock(return_value=[])
        sup.change_type = mock.Mock(return_value="")
        sup.require_human_for_deployment = mock.Mock()
        repo.ticket_env_committed.return_value = False
        repo.commit_subjects.return_value = []
        sup.finish_development("JAIF-9", {"summary": "Routine task"}, repo, "develop", "Claude",
                               Path(directory), "branch", secret, rescue_run=True)
        failed_events = []
        with mock.patch("aiflow.supervisor.events.emit",
                        side_effect=lambda *args, **fields: failed_events.append((args, fields))):
            sup.failed("JAIF-9", "developer", "Claude", secret, secret, output_allowed=False)
        self.assertNotIn(secret, "\n".join(comments))
        self.assertNotIn(secret, repr(failed_events))
        self.assertTrue(any("sending to review" in body for body in comments))
        self.assertTrue(any("without a usable result" in body for body in comments))

    def test_finish_development_rechecks_ownership_immediately_before_pushing(self):
        sup = make(redis=FakeRedis())
        sup.renew_ticket_ownership = mock.Mock(side_effect=(True, False))
        sup.read_replies = mock.Mock(return_value=[])
        sup.change_type = mock.Mock(return_value="")
        sup.require_human_for_deployment = mock.Mock()
        repo = mock.Mock()
        repo.ticket_env_committed.return_value = False
        repo.commit_subjects.return_value = []

        result = sup.finish_development("JAIF-9", {"summary": "Routine task"}, repo, "develop", "Claude",
                                        Path("."), "branch", "private output")

        self.assertIsNone(result)
        repo.push.assert_not_called()

    def test_executor_does_not_launch_after_setup_uses_the_remaining_budget(self):
        sup = make()
        repo = mock.Mock()
        repo.name = "ai-flow"
        clock = {"now": 1000.0}

        def prepare_environment(*_args):
            clock["now"] += 61
            return {}

        with tempfile.TemporaryDirectory() as directory:
            sup.replies_file = lambda key: Path(directory) / "replies.json"
            for name, value in (("recall", ""), ("memory_env", {}), ("rework_text", ""), ("pr_text", ""),
                                ("change_type", ""), ("remember_lessons", None), ("log_usage", None)):
                setattr(sup, name, mock.Mock(return_value=value))
            sup.jira.comments = lambda key, last=6: []
            with mock.patch("aiflow.supervisor.time.time", side_effect=lambda: clock["now"]), \
                    mock.patch("aiflow.supervisor.agents.build_command", return_value=(["claude"], None)), \
                    mock.patch("aiflow.supervisor.agents.run_env", side_effect=prepare_environment), \
                    mock.patch("aiflow.supervisor.runner.run") as run:
                result = sup.execute("developer", "Claude", "JAIF-9", {"summary": "Routine fix"},
                                     Path(directory), "JAIF-9-fix", "develop", repo,
                                     rescue_deadline=1060)

        run.assert_not_called()
        self.assertEqual(result[0], "rescue budget expired during worker setup")

    def test_worker_run_findings_reach_the_flow_doctor(self):
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira.status = "Handover"
        redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record(stuck="pre-rescue audit", findings=["pre-rescue audit"])))
        repo = mock.Mock()
        repo.ahead.return_value = 0

        self.assertTrue(sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch",
                                                  "Claude found that the fixture lacks the expected event.", "ok",
                                                  ("BLOCKED", "the fixture lacks the expected event"), False))

        doctor = next(body for _, body in sup.jira.comments if body.startswith("AI Flow doctor"))
        record = json.loads(redis.get("rescue:JAIF-9"))
        self.assertIn("Findings: the fixture lacks the expected event", doctor)
        self.assertEqual(record["doctor_findings"], "the fixture lacks the expected event")
        self.assertNotIn("pre-rescue audit", doctor)
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "To Do"))

    def test_worker_run_findings_do_not_copy_sensitive_values_to_the_doctor(self):
        secret = "CANARY_SECRET_92_WORKER"
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira.status = "Handover"
        redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
        repo = mock.Mock()
        repo.ahead.return_value = 0

        sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch", "",
                                  "ok", ("BLOCKED", f"fixture missing; token={secret}"), False)

        doctor = next(body for _, body in sup.jira.comments if body.startswith("AI Flow doctor"))
        record = json.loads(redis.get("rescue:JAIF-9"))
        self.assertNotIn(secret, doctor)
        self.assertNotIn(secret, record["doctor_findings"])

    def test_committed_env_safety_block_is_not_cleared_by_the_flow_doctor(self):
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira.status = "Handover"
        redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
        repo = mock.Mock()
        repo.ahead.return_value = 1
        repo.ticket_env_committed.return_value = True
        jira_view = sup.jira.view

        def block_transition(key, status):
            sup.jira.moves.append((key, status))
            sup.jira.status = status
            return True

        sup.jira.transition = mock.Mock(side_effect=block_transition)
        sup.jira.view = mock.Mock(side_effect=(JiraError("unavailable"), JiraError("unavailable")))

        with mock.patch("aiflow.config.Settings.project", return_value=SimpleNamespace(database="ticket-db")):
            handled = sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch",
                                                "committed .env", "ok", ("DONE", "done"), False)

        blocked = json.loads(redis.get("blocked:JAIF-9"))
        rescue = json.loads(redis.get("rescue:JAIF-9"))
        self.assertTrue(handled)
        self.assertIn(".env was committed", blocked["reason"])
        self.assertEqual(rescue["state"], "completing")
        sup.jira.view = jira_view
        Rescue(sup).check_rescues()  # a later pass sees Blocked while the record still says completing
        rescue = json.loads(redis.get("rescue:JAIF-9"))
        self.assertEqual(rescue["state"], "safety_blocked")
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "Blocked"))
        self.assertFalse(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))

    def test_unignored_env_safety_block_is_not_cleared_during_rescue_preparation(self):
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira.status = "Handover"
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
        sup.project_database = mock.Mock()
        sup.project_database.env_is_ignored.return_value = False

        with mock.patch("aiflow.config.Settings.project", return_value=SimpleNamespace(database="ticket-db")):
            self.assertFalse(sup.prepare_ticket_database("JAIF-9", Path(".")))
        self.assertTrue(sup.end_master_rescue("JAIF-9", False, "database preparation failed"))

        blocked = json.loads(redis.get("blocked:JAIF-9"))
        rescue = json.loads(redis.get("rescue:JAIF-9"))
        self.assertIn(".env is not ignored", blocked["reason"])
        self.assertEqual(rescue["state"], "safety_blocked")
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "Blocked"))
        self.assertFalse(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))

    def test_accepted_development_handoff_with_lost_response_finishes_the_rescue(self):
        for status in ("In Review", "Ready to merge"):
            with self.subTest(status=status):
                redis = FakeRedis()
                sup = make(redis=redis)
                sup.jira.status = "Handover"
                redis.set("lease:JAIF-9", sup.cfg.owner, 60)
                redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
                repo = mock.Mock()
                repo.ahead.return_value = 1

                def accepted_but_unacknowledged(*_args, **_kwargs):
                    sup.jira.status = status
                    return False

                with mock.patch.object(sup, "finish_development", side_effect=accepted_but_unacknowledged):
                    handled = sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch",
                                                        "finished", "ok", ("DONE", "done"), False)

                record = json.loads(redis.get("rescue:JAIF-9"))
                self.assertTrue(handled)
                self.assertEqual(record["state"], "finished")
                self.assertFalse(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))

    def test_unreadable_completion_status_stays_pending_for_reconciliation(self):
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira.status = "Handover"
        redis.set("lease:JAIF-9", sup.cfg.owner, 60)
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
        repo = mock.Mock()
        repo.ahead.return_value = 1
        sup.jira.view = mock.Mock(side_effect=JiraError("unavailable"))

        with mock.patch.object(sup, "finish_development", return_value=False):
            handled = sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch",
                                                "finished", "ok", ("DONE", "done"), False)

        record = json.loads(redis.get("rescue:JAIF-9"))
        self.assertTrue(handled)
        self.assertEqual(record["state"], "completing")
        self.assertFalse(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))
