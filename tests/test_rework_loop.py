"""Rework loop limit (JAIF-93): rounds counted in cycles:<KEY>, reasons per round, per-project override, reset on unblock."""
import json
import tempfile
import unittest
from pathlib import Path

from aiflow.config import load
from aiflow.supervisor import Supervisor, find_incidents
from tests.test_supervisor import FakeJira, FakeRedis, FakeRepo, make


class LoopLimitTests(unittest.TestCase):
    def test_third_rework_is_allowed_and_fourth_blocks_with_reasons_for_rescue(self):
        sup = make()
        self.assertEqual(sup.cfg.loop_limit, 3)
        for round_number in range(1, 4):
            sup.send_back("JAIF-9", FakeRepo(), f"failed check {round_number}", f"reason {round_number}", [])
        self.assertEqual(sup.jira.moves, [("JAIF-9", "To Do")] * 3)
        self.assertEqual(sup.redis.get("cycles:JAIF-9"), "3")

        sup.send_back("JAIF-9", FakeRepo(), "failed check 4", "reason 4", [])

        blocked = json.loads(sup.redis.get("blocked:JAIF-9"))
        self.assertEqual((blocked["kind"], blocked["rework_rounds"]), ("Rework", 4))
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "Blocked"))
        block_comment = sup.jira.comments[-1][1]
        for round_number in range(1, 5):
            self.assertIn(f"failed check {round_number}", block_comment)
            self.assertIn(f"reason {round_number}", block_comment)

        incidents = find_incidents({
            "now": 1000,
            "thresholds": {"blocked_minutes": 30, "stuck_minutes": 120, "ci_wait_minutes": 120, "rework_limit": 3},
            "tickets": [{"key": "JAIF-9", "status": "Blocked", "status_since": 0, "labels": [], "blocked_kind": blocked["kind"],
                         "rework_rounds": blocked["rework_rounds"], "loop_limit": 3}],
            "heartbeats": {}, "stale_seconds": 300, "role_pools": {},
        })
        self.assertEqual([incident.id for incident in incidents], ["rework:JAIF-9"])

    def test_loop_limit_can_be_overridden_per_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "aiflow").mkdir()
            (base / "aiflow.toml").write_text("[defaults]\n")
            (base / "aiflow" / "a.toml").write_text('[project]\njira_key = "JAIF"\nrepo = "ai-flow"\n')
            (base / "aiflow" / "b.toml").write_text('[project]\njira_key = "JOOCX"\nrepo = "crawlerx"\nloop_limit = 1\n')
            cfg = load({"AIFLOW_ROOT": tempfile.gettempdir(), "AIFLOW_MACHINE": "test"}, config_dir=base)
        self.assertEqual((cfg.loop_limit_for("JAIF-9"), cfg.loop_limit_for("JOOCX-9")), (3, 1))
        sup = Supervisor(cfg, FakeJira(), FakeRedis())
        for round_number in range(1, 3):
            sup.send_back("JOOCX-9", FakeRepo(), f"project failure {round_number}", "details", [])
        self.assertEqual(sup.jira.moves, [("JOOCX-9", "To Do"), ("JOOCX-9", "Blocked")])
        self.assertEqual(json.loads(sup.redis.get("blocked:JOOCX-9"))["rework_rounds"], 2)

    def test_rounds_counted_before_history_existed_are_backfilled_in_the_blocked_comment(self):
        sup = make()
        sup.redis.data.update({"cycles:JAIF-9": "3", "rework:JAIF-9": "Rework needed (round 3): old third failure"})
        sup.send_back("JAIF-9", FakeRepo(), "failed check 4", "reason 4", [])
        self.assertEqual(sup.jira.moves[-1], ("JAIF-9", "Blocked"))
        block_comment = sup.jira.comments[-1][1]
        for round_number in (1, 2, 3, 4):
            self.assertIn(f"Round {round_number}:", block_comment)
        self.assertIn("old third failure", block_comment)
        self.assertIn("not recorded", block_comment)
        self.assertIn("failed check 4", block_comment)

    def test_feedback_item_summaries_are_in_round_history_and_final_blocked_comment(self):
        sup = make()
        for round_number in range(1, 5):
            sup.send_back("JAIF-9", FakeRepo(), f"failed check {round_number}", "", [{
                "id": f"review:R{round_number}", "kind": "review", "who": "reviewer", "bot": False, "where": "review",
                "text": f"add the missing validation for round {round_number}",
            }])

        history = json.loads(sup.redis.get("rework-history:JAIF-9"))
        blocked_comment = sup.jira.comments[-1][1]
        self.assertEqual(len(history), 4)
        for round_number, entry in enumerate(history, start=1):
            feedback_text = f"add the missing validation for round {round_number}"
            self.assertIn(feedback_text, entry["reason"])
            self.assertIn(f"Round {round_number}: failed check {round_number}", blocked_comment)
            self.assertIn(feedback_text, blocked_comment)

    def test_owner_unblock_resets_rework_rounds_and_history(self):
        sup = make()
        sup.redis.data.update({"cycles:K-1": "4", "rework-history:K-1": '[{"round": 4, "reason": "old failure"}]'})
        sup.block("K-1", "loop limit exceeded", kind="Rework")
        self.assertIsNone(sup.redis.get("cycles:K-1"))
        self.assertIsNotNone(sup.redis.get("rework-history:K-1"))  # retained while rescue can inspect the reasons

        sup.clear_block("K-1")
        sup.send_back("K-1", FakeRepo(), "new failure", "new reason", [])

        self.assertEqual(sup.redis.get("cycles:K-1"), "1")
        self.assertEqual(json.loads(sup.redis.get("rework-history:K-1")), [{"round": 1, "reason": "new failure\nnew reason"}])
        self.assertNotIn(("K-1", "Blocked"), sup.jira.moves[1:])
