import subprocess
import tempfile
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


class MergedPullRequestCleanupTests(unittest.TestCase):
    def test_confirmed_merged_pr_removes_worktree_without_ancestry_guard(self):
        repo = Repo(Path("/workspaces"), "project")
        worktree = Path("/worktrees/JAIF-9")
        with patch.object(repo, "find_worktree", return_value=(worktree, "JAIF-9-fix")), \
                patch.object(repo, "remove_worktree") as remove_worktree, \
                patch.object(repo, "remote_branch", return_value=None), \
                patch("aiflow.gitops.git") as run_git:
            repo.cleanup("JAIF-9", merged_pr=True)

        remove_worktree.assert_called_once_with(worktree)
        run_git.assert_called_once_with(repo.path, "branch", "-D", "JAIF-9-fix")

    def test_squash_merged_branch_is_removed_even_when_its_commits_are_not_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "JOOservices" / "projects" / "project"
            path.mkdir(parents=True)

            def run_git(*args):
                return subprocess.run(
                    ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
                ).stdout.strip()

            run_git("init", "-b", "develop")
            run_git("config", "user.name", "Test")
            run_git("config", "user.email", "test@example.invalid")
            (path / "base.txt").write_text("base\n", encoding="utf-8")
            run_git("add", "base.txt")
            run_git("commit", "-m", "base")
            run_git("checkout", "-b", "JAIF-9-fix")
            (path / "feature.txt").write_text("first\n", encoding="utf-8")
            run_git("add", "feature.txt")
            run_git("commit", "-m", "first feature commit")
            (path / "feature.txt").write_text("first\nsecond\n", encoding="utf-8")
            run_git("commit", "-am", "second feature commit")
            feature_head = run_git("rev-parse", "HEAD")

            run_git("checkout", "develop")
            (path / "feature.txt").write_text("first\nsecond\n", encoding="utf-8")
            run_git("add", "feature.txt")
            run_git("commit", "-m", "squash feature")
            develop = run_git("rev-parse", "develop")
            ancestry = subprocess.run(
                ["git", "-C", str(path), "merge-base", "--is-ancestor", feature_head, develop],
                capture_output=True,
            )
            self.assertNotEqual(ancestry.returncode, 0)

            worktree = root / "worktrees" / "JAIF-9"
            worktree.parent.mkdir()
            run_git("worktree", "add", str(worktree), "JAIF-9-fix")
            repo = Repo(root, "project")
            with patch.object(repo, "remove_worktree", side_effect=lambda target: run_git("worktree", "remove", str(target))):
                repo.cleanup("JAIF-9", remote=False, merged_pr=True)

            self.assertFalse(worktree.exists())
            branch = subprocess.run(
                ["git", "-C", str(path), "show-ref", "--verify", "--quiet", "refs/heads/JAIF-9-fix"],
                capture_output=True,
            )
            self.assertNotEqual(branch.returncode, 0)


if __name__ == "__main__":
    unittest.main()
