import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiflow.gitops import GitError
from aiflow.supervisor import Supervisor
from aiflow.worktree_sweep import cleanup_done_worktree, resolve_merged_pull_request


class PullRequestResolutionTests(unittest.TestCase):
    @mock.patch("aiflow.worktree_sweep.GitHub")
    def test_uses_the_known_merged_pr_when_head_lookup_misses(self, github_type):
        gh = github_type.return_value
        gh.find.return_value = None
        known_pr = mock.Mock(return_value={"state": "MERGED", "headRefName": "JAIF-9-fix"})

        merged, error = resolve_merged_pull_request(
            mock.Mock(github_slug=lambda: "o/r"), "JAIF-9", "JAIF-9-fix", known_pr
        )

        self.assertTrue(merged)
        self.assertIsNone(error)
        gh.find.assert_called_once_with("JAIF-9-fix")
        known_pr.assert_called_once_with("JAIF-9", gh)

    @mock.patch("aiflow.worktree_sweep.GitHub")
    def test_ignores_a_known_pr_for_a_different_branch(self, github_type):
        github_type.return_value.find.return_value = None
        known_pr = mock.Mock(return_value={"state": "MERGED", "headRefName": "JAIF-9-other"})

        merged, _ = resolve_merged_pull_request(
            mock.Mock(github_slug=lambda: "o/r"), "JAIF-9", "JAIF-9-fix", known_pr
        )

        self.assertFalse(merged)


class CleanupStateTests(unittest.TestCase):
    def test_ancestry_refusal_is_logged_once_and_retried_after_merge(self):
        class MemoryRedis:
            values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ttl):
                self.values[key] = value

            def delete(self, key):
                self.values.pop(key, None)

        class SweepRepo:
            def __init__(self):
                self.calls = []

            def cleanup(self, key, merged_pr=False):
                self.calls.append((key, merged_pr))
                if not merged_pr:
                    raise GitError("Branch JAIF-9-fix is not fully merged into develop; refusing cleanup")

        redis, repo = MemoryRedis(), SweepRepo()
        messages = []
        cleanup_done_worktree(redis, repo, "machine-a", "JAIF-9", "JAIF-9-fix", False, None, messages.append)
        cleanup_done_worktree(redis, repo, "machine-a", "JAIF-9", "JAIF-9-fix", False, None, messages.append)
        cleanup_done_worktree(redis, repo, "machine-a", "JAIF-9", "JAIF-9-fix", True, None, messages.append)

        self.assertEqual(repo.calls, [("JAIF-9", False), ("JAIF-9", True)])
        self.assertEqual(sum("keeping the worktree and branch" in message for message in messages), 1)
        self.assertIn("not fully merged into develop; refusing cleanup", messages[0])
        self.assertNotIn("sweep-cleanup:machine-a:JAIF-9:JAIF-9-fix", redis.values)

    def test_cleanup_state_is_independent_for_two_machines(self):
        class SharedRedis:
            def __init__(self):
                self.values = {}

            def get(self, key):
                return self.values.get(key)

            def set(self, key, value, ttl):
                self.values[key] = value

            def delete(self, key):
                self.values.pop(key, None)

        class SweepRepo:
            def __init__(self, fail):
                self.fail = fail
                self.calls = []

            def cleanup(self, key, merged_pr=False):
                self.calls.append((key, merged_pr))
                if self.fail:
                    raise OSError("permission denied")

        redis = SharedRedis()
        failed_repo, healthy_repo = SweepRepo(True), SweepRepo(False)
        messages = []
        cleanup_done_worktree(redis, failed_repo, "machine-a", "JAIF-9", "JAIF-9-fix", False, None, messages.append)
        cleanup_done_worktree(redis, healthy_repo, "machine-b", "JAIF-9", "JAIF-9-fix", False, None, messages.append)

        self.assertEqual(failed_repo.calls, [("JAIF-9", False)])
        self.assertEqual(healthy_repo.calls, [("JAIF-9", False)])
        self.assertEqual(redis.values, {"sweep-cleanup:machine-a:JAIF-9:JAIF-9-fix": "stopped"})
        self.assertEqual(len(messages), 2)

    def test_other_cleanup_failures_stop_until_operator_intervention(self):
        class MemoryRedis:
            value = None

            def get(self, key):
                return self.value

            def set(self, key, value, ttl):
                self.value = value

            def delete(self, key):
                self.value = None

        redis = MemoryRedis()
        repo = mock.Mock()
        repo.cleanup.side_effect = OSError("permission denied")
        messages = []
        for _ in range(2):
            cleanup_done_worktree(redis, repo, "machine-a", "JAIF-9", "JAIF-9-fix", False, None, messages.append)

        repo.cleanup.assert_called_once_with("JAIF-9", merged_pr=False)
        self.assertEqual(len(messages), 1)
        self.assertIn("permission denied", messages[0])


class SupervisorSweepIntegrationTests(unittest.TestCase):
    def test_declined_ticket_sweep_deletes_only_this_machines_cleanup_marker(self):
        class MemoryRedis:
            def __init__(self):
                self.values = {}

            def acquire(self, key, owner, ttl):
                if key in self.values:
                    return False
                self.values[key] = owner
                return True

            def get(self, key):
                return self.values.get(key)

            def delete(self, key):
                self.values.pop(key, None)

        class SweepRepo:
            name = "ai-flow"

            def exists(self):
                return True

            def worktrees(self):
                return [(Path("/worktrees/JAIF-9"), "JAIF-9-fix")]

            def remove_worktree(self, path):
                self.removed = path

        redis = MemoryRedis()
        local_marker = "sweep-cleanup:test:JAIF-9:JAIF-9-fix"
        other_marker = "sweep-cleanup:other:JAIF-9:JAIF-9-fix"
        redis.values.update({local_marker: "stopped", other_marker: "stopped"})
        sup = Supervisor.__new__(Supervisor)
        sup.cfg = SimpleNamespace(
            machine="test", owner="owner", root=Path("/workspaces"),
            registry=SimpleNamespace(projects={"ai-flow": SimpleNamespace(repo="ai-flow", company="JOOservices")}),
        )
        sup.redis = redis
        sup.jira = SimpleNamespace(search=lambda *args, **kwargs: [{"key": "JAIF-9", "status": "Declined"}])
        sup.sweep_ticket_databases = lambda: None
        repo = SweepRepo()
        with mock.patch("aiflow.supervisor.Repo", return_value=repo):
            sup.sweep_worktrees()

        self.assertEqual(repo.removed, Path("/worktrees/JAIF-9"))
        self.assertNotIn(local_marker, redis.values)
        self.assertEqual(redis.get(other_marker), "stopped")


if __name__ == "__main__":
    unittest.main()
