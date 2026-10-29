"""Supervisor status tracking for master-rescue time accounting."""
import time
import unittest
from unittest import mock

from tests.test_supervisor import make, stamp


class CandidateStatusTests(unittest.TestCase):
    def test_rework_tickets_come_first(self):
        sup = make()
        sup.jira.search = lambda jql, limit=50: [{"key": "A-1", "status": "To Do", "labels": []},
                                                  {"key": "A-2", "status": "To Do", "labels": ["rework"]},
                                                  {"key": "A-3", "status": "To Do", "labels": []}]
        self.assertEqual([issue["key"] for issue in sup.candidates()], ["A-2", "A-1", "A-3"])

    def test_owner_move_into_blocked_updates_status_clock_and_work_total(self):
        sup = make()
        now = time.time()
        sup.jira.found = [{"key": "JAIF-92", "status": "Blocked", "updated": stamp(0), "labels": []}]
        queries = []
        sup.jira.search = lambda jql, limit=50: queries.append(jql) or sup.jira.found
        sup.redis.set_persistent("status:JAIF-92", "In Progress")
        sup.redis.set_persistent("statussince:JAIF-92", str(now - 25 * 60))
        sup.redis.set_persistent("worked:JAIF-92", "5")

        with mock.patch("aiflow.supervisor.time.time", return_value=now):
            self.assertEqual(sup.candidates(), [])

        self.assertIn('"Blocked"', queries[0])
        self.assertEqual(sup.redis.get("status:JAIF-92"), "Blocked")
        self.assertEqual(float(sup.redis.get("statussince:JAIF-92")), now)
        self.assertAlmostEqual(float(sup.redis.get("worked:JAIF-92")), 30, delta=0.01)
