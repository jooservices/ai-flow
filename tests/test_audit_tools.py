import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiflow import fleet, preflight, quota, reconcile, report


class ReportTests(unittest.TestCase):
    def test_same_cause_gets_the_same_signature(self):
        a = report.signature("Config", "Cannot get the branch on vm: worktree /home/x/JOOWT-281-a has 3 changes")
        b = report.signature("Config", "Cannot get the branch on vm: worktree /home/y/JAVM-9-b has 7 changes")
        self.assertEqual(a, b)
        self.assertNotEqual(a, report.signature("Quota", "Cannot get the branch on vm: worktree /home/x/JOOWT-281-a has 3 changes"))

    def test_words_with_digits_are_kept(self):
        self.assertIn("E2E", report.normalize("E2E runtime failed after 12 tries"))

    def test_http_status_codes_remain_distinct(self):
        self.assertIn("HTTP 403", report.normalize("request failed with HTTP 403"))
        self.assertNotEqual(report.signature("OpenObserve", "request failed with HTTP 403"),
                            report.signature("OpenObserve", "request failed with HTTP 404"))

    def test_http_error_status_codes_remain_distinct(self):
        samples = (
            ("HTTP Error", "HTTP Error 401: Unauthorized", "HTTP Error 503: Service Unavailable"),
            ("HTTP status", "HTTP status: 401 Unauthorized", "HTTP status: 503 Unavailable"),
            ("HTTP status code", "HTTP status code: 401 Unauthorized", "HTTP status code: 503 Unavailable"),
            ("HTTP/1.1", "HTTP/1.1 401 Unauthorized", "HTTP/1.1 503 Unavailable"),
        )
        for label, first, second in samples:
            with self.subTest(label=label):
                self.assertIn("401", report.normalize(first))
                self.assertNotEqual(report.signature("OpenObserve", first), report.signature("OpenObserve", second))

    def test_long_normalized_reasons_remain_distinct(self):
        prefix = "same cause " * 12
        self.assertNotEqual(report.signature("Config", prefix + "first"),
                            report.signature("Config", prefix + "second"))

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

    def test_a_window_that_reset_since_snapshot_is_unknown(self):
        windows = quota.windows(quota.last_limits([self.LINE]), now=3000)
        self.assertEqual([w["used_percent"] for w in windows], [None, 20.0])
        self.assertIsNone(windows[0]["left_percent"])
        self.assertTrue(windows[0]["window_already_reset"])
        result = quota.eligibility("Codex", {"known": True, "windows": windows})
        self.assertFalse(result["known"])
        self.assertIsNone(result["eligible"])

    def test_snapshot_after_reset_uses_the_reported_usage(self):
        windows = quota.windows(quota.last_limits([self.LINE]), now=3000, observed_at=2500)
        self.assertEqual(windows[0]["used_percent"], 97.0)
        self.assertFalse(windows[0]["window_already_reset"])

    def test_unrounded_remaining_quota_controls_eligibility(self):
        level = {"known": True, "windows": [{"used_percent": 95.04, "left_percent": 5.0}]}
        result = quota.eligibility("Codex", level)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["lowest_left"], 5.0)

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

    def test_uses_configured_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex-home"
            sessions = home / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "rollout-1.jsonl").write_text(self.LINE + "\n")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=True):
                level = quota.codex_level(now=1000)
        self.assertTrue(level["known"])


class PreflightTests(unittest.TestCase):
    def test_lock_file_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tools.lock"
            path.write_text("# comment\ngit=2.53\nnode = 24.22\n")
            self.assertEqual(preflight.read_lock(path), {"git": "2.53", "node": "24.22"})

    def test_missing_configuration_fails(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("AIFLOW_", "WORKSPACES_"))}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, clean, clear=True):
            results = preflight.run(Path(tmp), skip_ai=True, check_reach=lambda host, port: False)
            failed = {r["check"] for r in results if not r["ok"]}
            self.assertIn("config: aiflow.toml", failed)
            self.assertIn("Redis reachable", failed)

    def test_malformed_configs_are_reported_and_independent_checks_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "JOOservices" / "config"
            project_dir = config / "aiflow"
            project_dir.mkdir(parents=True)
            (config / "aiflow.toml").write_text("[runtime\n", encoding="utf-8")
            (project_dir / "JAIF.toml").write_text("[project\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AIFLOW_CONFIG_DIR": str(config)}, clear=True), \
                 mock.patch.object(preflight.shutil, "which", return_value=None):
                results = preflight.run(root, skip_ai=True, check_reach=lambda host, port: False)
        checks = {item["check"]: item for item in results}
        self.assertFalse(checks["config: aiflow.toml"]["ok"])
        self.assertFalse(checks["config: project files"]["ok"])
        self.assertIn("TOMLDecodeError", checks["config: aiflow.toml"]["detail"])
        self.assertIn("Redis reachable", checks)

    def test_unreadable_lock_is_reported_and_other_checks_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            (config / "aiflow.toml").write_text("[runtime]\n", encoding="utf-8")
            lock = root / "missing-tools.lock"
            with mock.patch.dict(os.environ, {"AIFLOW_CONFIG_DIR": str(config)}, clear=True), \
                 mock.patch.object(preflight.shutil, "which", return_value=None):
                results = preflight.run(root, skip_ai=True, lock=lock, check_reach=lambda host, port: False)
        checks = {item["check"]: item for item in results}
        self.assertFalse(checks["version lock file"]["ok"])
        self.assertIn("FileNotFoundError", checks["version lock file"]["detail"])
        self.assertIn("Redis reachable", checks)


class ReconcileTests(unittest.TestCase):
    def test_keys_come_from_commit_and_merge_subjects(self):
        self.assertEqual(reconcile.merged_keys(["feat(x): A thing (JAIF-5)",
                                                "Merge pull request #1 from a/JAIF-6-b",
                                                "docs: no key"]), {"JAIF-5", "JAIF-6"})

    def test_merged_pull_request_metadata_is_paginated_and_uses_jira_field(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == "git":
                return subprocess.CompletedProcess(command, 0, "git@github.com:jooservices/ai-flow.git", "")
            pages = [[{"merged_at": "2026-10-23T12:00:00Z", "body": "Jira: JAIF-353"}],
                     [{"merged_at": "2026-10-24T12:00:00Z", "body": None,
                       "head": {"ref": "JAIF-7-title"}}]]
            return subprocess.CompletedProcess(command, 0, json.dumps(pages), "")

        pull_requests = reconcile.merged_pull_requests(Path("/repo"), "origin/develop", runner=runner)
        self.assertEqual(reconcile.pull_request_keys(pull_requests), {"JAIF-353", "JAIF-7"})
        self.assertEqual(calls[1][1:3], ["api", "--paginate"])
        self.assertIn("--slurp", calls[1])
        self.assertIn("repos/jooservices/ai-flow/pulls?state=closed&base=develop&per_page=100", calls[1][-1])

    def test_base_must_be_a_branch_not_a_commit_revision(self):
        with self.assertRaises(ValueError):
            reconcile.github_base_branch("abc123456789")

    def test_custom_repository_requires_project_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                reconcile.main(["--repo", tmp])

    def test_invalid_project_key_is_not_silently_omitted(self):
        with self.assertRaises(SystemExit):
            reconcile.main(["--keys", "JAIF,JAVM_"])

    def test_only_not_done_tickets_are_listed(self):
        issues = [{"key": "JAIF-5", "status": "Backlog", "summary": "a"}, {"key": "JAIF-6", "status": "Done"}, {"key": "JAIF-7", "status": "To Do"}]
        self.assertEqual([r["key"] for r in reconcile.mismatches({"JAIF-5", "JAIF-6"}, issues)], ["JAIF-5"])

    def test_reads_jira_fields_shape(self):
        issue = {"key": "JAIF-5", "fields": {"status": {"name": "Backlog"}, "summary": "Nested summary"}}
        self.assertEqual(reconcile.mismatches({"JAIF-5"}, [issue]),
                         [{"key": "JAIF-5", "status": "Backlog", "summary": "Nested summary"}])

    def test_searches_merged_keys_in_bounded_batches(self):
        class FakeJira:
            def __init__(self): self.calls = []
            def search(self, jql, limit):
                self.calls.append((jql, limit))
                return []

        keys = {f"JAIF-{index}" for index in range(1, 206)}
        jira = FakeJira()
        self.assertEqual(reconcile.search_merged_issues(jira, keys, ["JAIF"]), [])
        self.assertEqual([limit for _, limit in jira.calls], [100, 100, 5])
        queried = [key for jql, _ in jira.calls for key in re.findall(r"JAIF-\d+", jql)]
        self.assertEqual(set(queried), keys)
        self.assertEqual(len(queried), len(keys))


class FleetTests(unittest.TestCase):
    def test_snapshot_uses_a_fixed_length_commit_hash(self):
        self.assertIn("rev-parse HEAD 2>/dev/null | cut -c1-12", fleet.SNAPSHOT)
        self.assertNotIn("--format=%h", fleet.SNAPSHOT)

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

    def test_consensus_ties_follow_host_order(self):
        self.assertEqual(fleet.consensus_commit(["a", "b", "b", "a"]), "a")
        self.assertEqual(fleet.consensus_commit(["b", "a", "a", "b"]), "b")


class ReviewFixTests(unittest.TestCase):
    def test_version_boundaries(self):
        self.assertTrue(preflight.version_matches("2.5", "git version 2.5.1"))
        self.assertFalse(preflight.version_matches("2.5", "git version 2.53.0"))
        self.assertFalse(preflight.version_matches("24.2", "v24.22.0"))
        self.assertTrue(preflight.version_matches("24.22", "v24.22.0"))

    def test_https_port_and_missing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            with mock.patch.object(preflight.common, "runtime", return_value={"obs_url": "https://obs.example", "memory_url": ""}), \
                 mock.patch.object(preflight.common, "projects", return_value=[{"jira_key": "AAA"}]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                results = preflight.run(Path(tmp), skip_ai=True, check_reach=lambda host, port: seen.append(port) or True)
            self.assertIn(443, seen)
            self.assertIn("project file has no `repo` key", [r["detail"] for r in results])

    def test_blocked_count_is_distinct_tickets(self):
        rows = [{"ticket": "A-1", "kind": "Config", "reason": "x"}, {"ticket": "A-1", "kind": "Config", "reason": "x"}]
        self.assertEqual(report.by_signature(rows)[0]["count"], 1)

    def test_warn_above_limit_still_reports_over_limit(self):
        found = report.over_limits([{"ticket": "A-1", "minutes": 130}], warn=200, limit=120)
        self.assertEqual(found[0]["level"], "over-limit")

    def test_query_pages_through_all_rows(self):
        pages = [[{"n": i} for i in range(report.LIMIT_ROWS)], [{"n": 1}]]
        class Reply:
            def __init__(self, hits): self.hits = hits
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return json.dumps({"hits": self.hits}).encode()
        replies = iter(pages)
        rows = report.query("http://x", "o", "s", "u", "p", "select 1", 1, opener=lambda req, timeout: Reply(next(replies)))
        self.assertEqual(len(rows), report.LIMIT_ROWS + 1)

    def test_other_codex_bucket_is_ignored(self):
        other = json.dumps({"payload": {"rate_limits": {"limit_id": "other", "primary": {"used_percent": 1}}}})
        mine = json.dumps({"payload": {"rate_limits": {"limit_id": "codex", "primary": {"used_percent": 50}}}})
        self.assertEqual(quota.last_limits([mine, other])["primary"]["used_percent"], 50)

    def test_newest_snapshot_wins_over_newest_file(self):
        def line(stamp, used):
            return json.dumps({"timestamp": stamp, "payload": {"rate_limits": {"primary": {"used_percent": used, "window_minutes": 300}}}})
        with tempfile.TemporaryDirectory() as tmp:
            old_file = Path(tmp) / "rollout-1.jsonl"; new_file = Path(tmp) / "rollout-2.jsonl"
            old_file.write_text(line("2026-10-20T10:00:00Z", 10) + "\n")
            new_file.write_text(line("2026-10-20T08:00:00Z", 90) + "\n")  # newer file, older snapshot
            os.utime(old_file, (1, 1)); os.utime(new_file, (2, 2))
            level = quota.codex_level(Path(tmp), now=1_792_500_000)
            self.assertEqual(level["windows"][0]["used_percent"], 10.0)

    def test_fleet_timeout_and_missing_checkout(self):
        def hang(*a, **k): raise subprocess.TimeoutExpired("ssh", 90)
        def cannot_start(*a, **k): raise FileNotFoundError("ssh")
        self.assertEqual(fleet.collect("vm=ssh:host", runner=hang), {"name": "vm"})
        self.assertEqual(fleet.collect("vm=ssh:host", runner=cannot_start), {"name": "vm"})
        self.assertIn("checkout not found", fleet.flags({"control": "active", "units_failed": "0"}))

    def test_base_cannot_be_an_option(self):
        with self.assertRaises(ValueError):
            reconcile.git_subjects(Path("."), "--output=/tmp/x")

    def test_preflight_malformed_ports_fail_without_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            reached = []
            with mock.patch.object(preflight.common, "runtime", return_value={
                    "obs_url": "https://obs.example:bad", "memory_url": "http://[::1"}), \
                 mock.patch.object(preflight.common, "projects", return_value=[]), \
                 mock.patch.object(preflight.shutil, "which", return_value=None), \
                 mock.patch.dict(os.environ, {"AIFLOW_REDIS_PORT": "not-a-port"}, clear=True):
                results = preflight.run(Path(tmp), skip_ai=True,
                                        check_reach=lambda host, port: reached.append((host, port)) or True)
        checks = {item["check"]: item for item in results}
        self.assertFalse(checks["Redis reachable"]["ok"])
        self.assertEqual(checks["Redis reachable"]["detail"], "invalid Redis port")
        self.assertFalse(checks["OpenObserve reachable"]["ok"])
        self.assertFalse(checks["memory service reachable"]["ok"])
        self.assertEqual(reached, [])

    def test_codex_secondary_only_window_is_known(self):
        line = json.dumps({"payload": {"rate_limits": {"secondary": {
            "used_percent": 70, "window_minutes": 10080, "resets_at": 9000}}}})
        limits = quota.last_limits([line])
        self.assertEqual([w["window"] for w in quota.windows(limits, now=1000)], ["10080 min"])

    def test_newest_snapshot_is_found_beyond_newest_eight_files(self):
        def line(stamp, used):
            return json.dumps({"timestamp": stamp, "payload": {"rate_limits": {
                "primary": {"used_percent": used, "window_minutes": 300}}}})

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in range(10):
                path = directory / f"rollout-{index}.jsonl"
                stamp, used = ("2026-10-20T10:00:00Z", 10) if index == 0 else ("2026-10-20T08:00:00Z", 80)
                path.write_text(line(stamp, used) + "\n")
                os.utime(path, (index + 1, index + 1))
            level = quota.codex_level(directory, now=1_792_500_000)
        self.assertEqual(level["windows"][0]["used_percent"], 10.0)

    def test_query_fails_instead_of_returning_truncated_rows(self):
        class Reply:
            def __init__(self, hits): self.hits = hits
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return json.dumps({"hits": self.hits}).encode()

        pages = iter([[{"n": 1}, {"n": 2}], [{"n": 3}, {"n": 4}]])
        with mock.patch.object(report, "LIMIT_ROWS", 2), mock.patch.object(report, "MAX_ROWS", 3):
            with self.assertRaises(report.QueryLimitExceeded):
                report.query("http://x", "o", "s", "u", "p", "select 1", 1,
                             opener=lambda req, timeout: Reply(next(pages)))


if __name__ == "__main__":
    unittest.main()
