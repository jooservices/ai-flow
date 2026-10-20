import json
import tempfile
import unittest
from pathlib import Path

from aiflow import fleet, preflight, quota, reconcile, report


class ReportTests(unittest.TestCase):
    def test_same_cause_gets_the_same_signature(self):
        a = report.signature("Config", "Cannot get the branch on vm: worktree /home/x/JOOWT-281-a has 3 changes")
        b = report.signature("Config", "Cannot get the branch on vm: worktree /home/y/JAVM-9-b has 7 changes")
        self.assertEqual(a, b)
        self.assertNotEqual(a, report.signature("Quota", "Cannot get the branch on vm: worktree /home/x/JOOWT-281-a has 3 changes"))

    def test_words_with_digits_are_kept(self):
        self.assertIn("E2E", report.normalize("E2E runtime failed after 12 tries"))

    def test_groups_count_tickets_and_projects(self):
        rows = [{"kind": "Config", "reason": "failed 1", "ticket": "A-1", "project": "A"}, {"kind": "Config", "reason": "failed 2", "ticket": "B-2", "project": "B"}]
        groups = report.by_signature(rows)
        self.assertEqual((groups[0]["count"], groups[0]["tickets"], groups[0]["projects"]), (2, ["A-1", "B-2"], ["A", "B"]))

    def test_tickets_over_limits(self):
        rows = [{"ticket": "A-1", "minutes": 70}, {"ticket": "A-1", "minutes": 60}, {"ticket": "B-1", "minutes": 61}, {"ticket": "C-1", "minutes": 10}]
        found = {item["ticket"]: item["level"] for item in report.over_limits(rows, 60, 120)}
        self.assertEqual(found, {"A-1": "over-limit", "B-1": "warn"})

    def test_query_posts_to_the_search_endpoint(self):
        seen = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"hits": [{"x": 1}]}).encode()

        def opener(request, timeout):
            seen["url"], seen["auth"] = request.full_url, request.headers["Authorization"]
            return Response()

        self.assertEqual(report.query("http://h:1/", "org", "s", "u", "p", "select 1 from {stream}", 1, opener), [{"x": 1}])
        self.assertEqual(seen["url"], "http://h:1/api/org/_search")
        self.assertTrue(seen["auth"].startswith("Basic "))


class QuotaTests(unittest.TestCase):
    LINE = json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 97.0, "window_minutes": 300, "resets_at": 2000},
                                                   "secondary": {"used_percent": 20.0, "window_minutes": 10080, "resets_at": 9000}}}})

    def test_reads_the_newest_limits(self):
        limits = quota.last_limits(["noise", self.LINE, "{}"])
        self.assertEqual(limits["primary"]["used_percent"], 97.0)

    def test_a_window_that_reset_counts_as_unused(self):
        windows = quota.windows(quota.last_limits([self.LINE]), now=3000)
        self.assertEqual([w["left_percent"] for w in windows], [100.0, 80.0])
        self.assertTrue(windows[0]["window_already_reset"])

    def test_below_the_minimum_is_not_eligible(self):
        level = {"known": True, "windows": [{"left_percent": 4.0}, {"left_percent": 70.0}]}
        self.assertFalse(quota.eligibility("Codex", level)["eligible"])
        self.assertTrue(quota.eligibility("OpenCode", level)["eligible"])
        self.assertFalse(quota.eligibility("Claude", {"known": True, "windows": [{"left_percent": 19.9}]})["eligible"])

    def test_unknown_source_is_not_a_verdict(self):
        self.assertIsNone(quota.eligibility("Claude", {"known": False})["eligible"])

    def test_reads_a_session_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "rollout-1.jsonl").write_text("x\n" + self.LINE + "\n")
            level = quota.codex_level(Path(tmp), now=1000)
            self.assertTrue(level["known"])
            self.assertEqual(level["windows"][0]["left_percent"], 3.0)


class PreflightTests(unittest.TestCase):
    def test_lock_file_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tools.lock"
            path.write_text("# comment\ngit=2.53\nnode = 24.22\n")
            self.assertEqual(preflight.read_lock(path), {"git": "2.53", "node": "24.22"})

    def test_missing_configuration_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = preflight.run(Path(tmp), skip_ai=True, check_reach=lambda host, port: False)
            failed = {r["check"] for r in results if not r["ok"]}
            self.assertIn("config: aiflow.toml", failed)
            self.assertIn("Redis reachable", failed)


class ReconcileTests(unittest.TestCase):
    def test_keys_come_from_the_subject_end(self):
        self.assertEqual(reconcile.merged_keys(["feat(x): A thing (JAIF-5)", "Merge pull request #1 from a/JAIF-6-b", "docs: no key"]), {"JAIF-5"})

    def test_only_not_done_tickets_are_listed(self):
        issues = [{"key": "JAIF-5", "status": "Backlog", "summary": "a"}, {"key": "JAIF-6", "status": "Done"}, {"key": "JAIF-7", "status": "To Do"}]
        self.assertEqual([r["key"] for r in reconcile.mismatches({"JAIF-5", "JAIF-6"}, issues)], ["JAIF-5"])


class FleetTests(unittest.TestCase):
    def test_flags(self):
        ok = {"units_failed": "0", "control": "active", "errors_1h": "0", "disk_used_pct": "10", "commit": "abc"}
        self.assertEqual(fleet.flags(ok, "abc"), [])
        bad = {**ok, "units_failed": "1", "control": "failed", "errors_1h": "4", "commit": "old"}
        self.assertEqual(len(fleet.flags(bad, "abc")), 4)
        self.assertEqual(fleet.flags({}), ["no answer"])

    def test_host_spec(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("R", (), {"returncode": 0, "stdout": "host=h\nload=0.1\n"})()

        self.assertEqual(fleet.collect("a=local", runner)["host"], "h")
        fleet.collect("b=ssh:-p 22 u@h", runner)
        self.assertEqual(calls[1][:4], ["ssh", "-o", "BatchMode=yes", "-o"])
        with self.assertRaises(ValueError):
            fleet.collect("c=nonsense", runner)


if __name__ == "__main__":
    unittest.main()
