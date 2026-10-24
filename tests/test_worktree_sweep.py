import unittest
from unittest import mock

from aiflow.gitops import GitError
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
        cleanup_done_worktree(redis, repo, "JAIF-9", "JAIF-9-fix", False, None, messages.append)
        cleanup_done_worktree(redis, repo, "JAIF-9", "JAIF-9-fix", False, None, messages.append)
        cleanup_done_worktree(redis, repo, "JAIF-9", "JAIF-9-fix", True, None, messages.append)

        self.assertEqual(repo.calls, [("JAIF-9", False), ("JAIF-9", True)])
        self.assertEqual(sum("keeping the worktree and branch" in message for message in messages), 1)
        self.assertIn("not fully merged into develop; refusing cleanup", messages[0])
        self.assertNotIn("sweep-cleanup:JAIF-9:JAIF-9-fix", redis.values)

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
            cleanup_done_worktree(redis, repo, "JAIF-9", "JAIF-9-fix", False, None, messages.append)

        repo.cleanup.assert_called_once_with("JAIF-9", merged_pr=False)
        self.assertEqual(len(messages), 1)
        self.assertIn("permission denied", messages[0])


if __name__ == "__main__":
    unittest.main()
