"""Worker pickup and execution deadlines for the legacy operator's master rescues."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiflow import agents
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
        redis.set_persistent("rescue:JAIF-9", json.dumps(self.record()))
        repo = mock.Mock()
        repo.ahead.return_value = 0

        sup.finish_master_attempt("JAIF-9", {}, repo, "develop", "Claude", Path("."), "branch", "",
                                  "ok", ("BLOCKED", f"fixture missing; token={secret}"), False)

        doctor = next(body for _, body in sup.jira.comments if body.startswith("AI Flow doctor"))
        record = json.loads(redis.get("rescue:JAIF-9"))
        self.assertNotIn(secret, doctor)
        self.assertNotIn(secret, record["doctor_findings"])
