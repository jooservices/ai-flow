import unittest

from test_supervisor import FakeRedis, make, stamp


class FairTickTests(unittest.TestCase):
    def test_one_worker_serves_both_projects_alternately(self):
        sup = make(redis=FakeRedis())
        ready = [
            {"key": "JAVM-1", "status": "To Do", "updated": stamp(0), "labels": [], "parent": None},
            {"key": "JAVM-2", "status": "To Do", "updated": stamp(0), "labels": [], "parent": None},
            {"key": "JOOWT-9", "status": "To Do", "updated": stamp(0), "labels": [], "parent": None},
            {"key": "JOOWT-8", "status": "To Do", "updated": stamp(0), "labels": [], "parent": None},
        ]  # Jira returns each project's tickets in priority-descending, oldest-created order.
        sup.jira.search = lambda jql, limit=50: [] if jql.startswith("parent in") else list(ready)
        sup.jira.view = lambda key, full=False: {"key": key, "status": "To Do", "agent": None}
        sup.ticket_spec = lambda key: ("", "")
        sup.reserve_agent = lambda role, key, field_agent: ("Codex", None)
        picked = []

        def process(issue, view, role, agent, session_owner=None):
            picked.append(issue["key"])
            ready.remove(issue)  # This ticket advanced and is no longer ready for another pickup.
            return True

        sup.process = process
        for _ in range(4):
            self.assertTrue(sup.tick())
        self.assertEqual(picked, ["JAVM-1", "JOOWT-9", "JAVM-2", "JOOWT-8"])


if __name__ == "__main__":
    unittest.main()
