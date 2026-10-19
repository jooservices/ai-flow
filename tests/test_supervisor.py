import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiflow import agents
from aiflow.config import load
from aiflow.supervisor import Supervisor, ticket_field


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value, ttl):
        self.data[name] = value

    def acquire(self, name, owner, ttl):
        if name in self.data:
            return False
        self.data[name] = owner
        return True

    def release(self, name, owner):
        return self.data.pop(name, None) is not None

    def renew(self, name, owner, ttl):
        return self.data.get(name) == owner

    def incr(self, name, ttl):
        self.data[name] = str(int(self.data.get(name, 0)) + 1)
        return int(self.data[name])

    def delete(self, name):
        self.data.pop(name, None)


class FakeJira:
    def __init__(self, blockers=None):
        self._blockers = blockers or []
        self.comments = []

    def comment(self, key, body):
        self.comments.append((key, body))

    def search(self, jql, limit=50):
        return []

    def blockers(self, key):
        return self._blockers


def stamp(seconds_ago):
    value = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return value.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def make(blockers=None, redis=None):
    cfg = load({"AIFLOW_ROOT": tempfile.gettempdir(), "AIFLOW_MACHINE": "test"})
    return Supervisor(cfg, FakeJira(blockers), redis or FakeRedis())


class VerdictTests(unittest.TestCase):
    def test_last_verdict_wins(self):
        text = "RESULT: DONE\nmore work\nRESULT: BLOCKED: need a decision"
        self.assertEqual(agents.verdict(text), ("BLOCKED", "need a decision"))

    def test_fail_reason(self):
        self.assertEqual(agents.verdict("notes\nVERDICT: FAIL: tests missing"), ("FAIL", "tests missing"))

    def test_no_verdict(self):
        self.assertIsNone(agents.verdict("finished everything"))

    def test_quota_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "x.log"
            log.write_text("error: Claude usage limit reached, resets at 5pm")
            self.assertTrue(agents.looks_like_quota(log, ""))
            log.write_text("all fine")
            self.assertFalse(agents.looks_like_quota(log, "done"))


class AgentChoiceTests(unittest.TestCase):
    def test_other_than_developer(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "Codex"]), "Claude")

    def test_prefers_not_reviewer(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "OpenCode"], avoid=("Claude",)), "OpenCode")

    def test_falls_back_when_only_reviewer_left(self):
        self.assertEqual(agents.choose_other("Codex", ["Claude", "Codex"], avoid=("Claude",)), "Claude")

    def test_none_when_only_developer(self):
        self.assertIsNone(agents.choose_other("Codex", ["Codex"]))


class TicketTests(unittest.TestCase):
    def test_fields_from_flattened_description(self):
        text = "Goal do a thing. Repo: ai-flow Base: develop Acceptance"
        self.assertEqual(ticket_field(text, "Repo", "x"), "ai-flow")
        self.assertEqual(ticket_field(text, "Base", "x"), "develop")
        self.assertEqual(ticket_field("nothing", "Repo", "dflt"), "dflt")


class EligibilityTests(unittest.TestCase):
    def issue(self, status="To Do", age=1000, labels=None):
        return {"key": "JAIF-9", "status": status, "updated": stamp(age), "labels": labels or []}

    def test_to_do_waits_for_blockers(self):
        self.assertFalse(make([{"key": "JAIF-1", "status": "In Progress"}]).eligible(self.issue()))
        self.assertTrue(make([{"key": "JAIF-1", "status": "Done"}]).eligible(self.issue()))

    def test_lease_blocks_pickup(self):
        redis = FakeRedis()
        redis.acquire("lease:JAIF-9", "other", 60)
        self.assertFalse(make(redis=redis).eligible(self.issue()))

    def test_in_progress_needs_orphan_grace(self):
        self.assertFalse(make().eligible(self.issue("In Progress", age=10)))
        self.assertTrue(make().eligible(self.issue("In Progress", age=1000)))

    def test_stale_search_result_for_a_finished_ticket(self):
        self.assertFalse(make().eligible(self.issue("Done")))

    def test_hold_label(self):
        self.assertFalse(make().eligible(self.issue(labels=["aiflow-hold"])))


class WaitingNoteTests(unittest.TestCase):
    def test_note_is_written_once_after_the_wait(self):
        sup = make()
        sup.note_waiting("JAIF-9", "reviewer")
        self.assertEqual(sup.jira.comments, [])
        sup.redis.data["waiting:JAIF-9:reviewer"] = "1"  # waiting since 1970
        sup.note_waiting("JAIF-9", "reviewer")
        sup.note_waiting("JAIF-9", "reviewer")
        self.assertEqual(len(sup.jira.comments), 1)


class HealthTests(unittest.TestCase):
    def test_missing_heartbeat_is_a_problem(self):
        import time
        from aiflow import health
        sup = make()
        cfg = load({"AIFLOW_ROOT": tempfile.gettempdir(), "AIFLOW_EXPECTED_SLOTS": "wsl:1,wsl:2"})
        sup.redis.data["hb:wsl:1"] = str(int(time.time()))
        kinds = [kind for kind, _ in health.problems(cfg, sup.redis, sup.jira)]
        self.assertEqual(kinds, ["slot-wsl:2"])


class LeaseRaceTests(unittest.TestCase):
    def test_second_supervisor_cannot_take_the_lease(self):
        redis = FakeRedis()
        self.assertTrue(redis.acquire("lease:JAIF-9", "wsl:1", 60))
        self.assertFalse(redis.acquire("lease:JAIF-9", "mac:1", 60))


if __name__ == "__main__":
    unittest.main()
