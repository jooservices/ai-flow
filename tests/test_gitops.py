import unittest
from pathlib import Path
from unittest.mock import patch

from aiflow.gitops import GitError, Repo


class DeployBaseTests(unittest.TestCase):
    def fake_git(self, calls, state):
        def git(_path, *args, check=True):
            calls.append(args)
            if args == ("remote",):
                return "origin"
            if args[:1] == ("fetch",):
                return ""
            if args == ("rev-parse", "develop") or args == ("rev-parse", "origin/develop"):
                return "base-sha"
            if args == ("status", "--porcelain"):
                return state.get("status", "")
            if args == ("symbolic-ref", "--short", "-q", "HEAD"):
                return state["branch"]
            if args == ("checkout", "develop"):
                state["branch"] = "develop"
                return ""
            if args == ("rev-parse", "HEAD"):
                return "base-sha"
            raise AssertionError(f"unexpected git invocation: {args}")
        return git

    def test_deploy_checkout_uses_the_latest_clean_base(self):
        repo = Repo(Path("/workspaces"), "project")
        calls = []
        state = {"branch": "feature"}

        with patch("aiflow.gitops.git", side_effect=self.fake_git(calls, state)):
            repo.checkout_deploy_base("develop")

        self.assertEqual(state["branch"], "develop")
        self.assertIn(("checkout", "develop"), calls)
        self.assertEqual(calls.count(("fetch", "origin", "develop")), 1)

    def test_deploy_checkout_refuses_a_dirty_main_clone(self):
        repo = Repo(Path("/workspaces"), "project")
        calls = []
        state = {"branch": "develop", "status": " M deploy.sh"}

        with patch("aiflow.gitops.git", side_effect=self.fake_git(calls, state)):
            with self.assertRaises(GitError):
                repo.checkout_deploy_base("develop")

        self.assertNotIn(("checkout", "develop"), calls)


if __name__ == "__main__":
    unittest.main()
