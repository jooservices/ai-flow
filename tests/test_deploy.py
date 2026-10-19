import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiflow import deploy, health
from aiflow.config import load
from aiflow.supervisor import Supervisor


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def enqueue_deploy(self, lock_name, pending_name, owner, ticket, ttl):
        with self.lock:
            if lock_name in self.data:
                current = self.data.get(pending_name, "")
                self.data[pending_name] = f"{current}\n{ticket}".strip("\n")
                return False
            self.data[lock_name] = owner
            return True

    def finish_deploy(self, lock_name, pending_name, owner, ttl):
        with self.lock:
            if self.data.get(lock_name) != owner:
                return -1
            pending = self.data.pop(pending_name, "")
            if pending:
                return pending
            self.data.pop(lock_name, None)
            return ""

    def set(self, name, value, ttl):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)

    def get(self, name):
        return self.data.get(name)

    def scan(self, pattern):
        import fnmatch
        return [name for name in self.data if fnmatch.fnmatch(name, pattern)]


class FakeJira:
    def __init__(self):
        self.comments = []
        self.transitions = []
        self.lock = threading.Lock()

    def comment(self, key, body):
        with self.lock:
            self.comments.append((key, body))

    def search(self, jql, limit=50):
        return []

    def transition(self, key, status):
        self.transitions.append((key, status))
        return True


class DeployRunnerTests(unittest.TestCase):
    def test_success_records_output_and_returns_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, logs = Path(tmp) / "main", Path(tmp) / "logs"
            cwd.mkdir()
            calls = []

            def runner(command, **options):
                calls.append((command, options))
                return subprocess.CompletedProcess(command, 0, stdout="deployed", stderr="ready")

            result = deploy.run("./deploy.sh", cwd, 7, logs, runner)

            self.assertTrue(result.succeeded)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual((calls[0][1]["cwd"], calls[0][1]["timeout"]), (cwd, 420))
            self.assertEqual(result.log_path.read_text(), "deployed\nready\n")
            self.assertIn("deployed", result.tail)

    def test_failure_is_reported_with_short_output_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"

            def runner(command, **options):
                return subprocess.CompletedProcess(command, 7, stdout="first\nlast failure", stderr="")

            result = deploy.run("./deploy.sh", Path(tmp), 2, logs, runner)
            summary = deploy.comment(result, 2, str(result.log_path))

            self.assertFalse(result.succeeded)
            self.assertEqual(result.exit_code, 7)
            self.assertIn("exit code 7", summary)
            self.assertIn("last failure", summary)

    def test_timeout_is_reported_and_environment_secret_is_redacted(self):
        secret = "deploy-secret-value"
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"

            def runner(command, **options):
                raise subprocess.TimeoutExpired(command, 60, output=f"started with {secret}", stderr="waiting")

            result = deploy.run("./deploy.sh", Path(tmp), 1, logs, runner, {"DEPLOY_TOKEN": secret})
            summary = deploy.comment(result, 1, str(result.log_path))
            log = result.log_path.read_text()

            self.assertTrue(result.timed_out)
            self.assertIn("timed out after 1 minute", summary)
            self.assertNotIn(secret, summary)
            self.assertNotIn(secret, log)
            self.assertIn("[REDACTED]", summary)


class DeploySupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "config"
        (config / "aiflow").mkdir(parents=True)
        (config / "aiflow.toml").write_text("[defaults]\n")
        (config / "aiflow" / "jaif.toml").write_text(
            '[project]\njira_key = "JAIF"\nrepo = "ai-flow"\ndeploy_command = "./deploy.sh"\n'
            'deploy_timeout_minutes = 1\n'
        )
        self.cfg = load({"AIFLOW_MACHINE": "test"}, config_dir=config)
        self.redis = FakeRedis()
        self.jira = FakeJira()
        self.logs = self.root / "logs"
        self.repo = SimpleNamespace(path=self.root / "main-clone")
        self.repo.path.mkdir()
        self.project = self.cfg.project("JAIF-1")

    def tearDown(self):
        self.tmp.cleanup()

    def supervisor(self, runner):
        return Supervisor(self.cfg, self.jira, self.redis, command_runner=runner, deploy_log_dir=self.logs)

    def test_missing_command_does_nothing(self):
        called = []
        supervisor = self.supervisor(lambda *args, **kwargs: called.append(args))
        project = SimpleNamespace(**{**self.project.__dict__, "deploy_command": ""})

        supervisor.deploy_after_merge("JAIF-1", self.repo, "develop", project)

        self.assertEqual(called, [])
        self.assertEqual(self.jira.comments, [])

    def test_after_merge_runs_the_configured_hook(self):
        called = []

        def runner(command, **options):
            called.append((command, options["cwd"]))
            return subprocess.CompletedProcess(command, 0, "deployed", "")

        self.supervisor(runner).after_merge("JAIF-15", self.repo, "develop")

        self.assertEqual(called, [("./deploy.sh", self.repo.path)])
        self.assertEqual([key for key, _ in self.jira.comments], ["JAIF-15"])

    def test_hotfix_and_release_branches_are_skipped_unless_all_is_set(self):
        called = []

        def runner(command, **options):
            called.append(command)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        supervisor = self.supervisor(runner)
        supervisor.deploy_after_merge("JAIF-1", self.repo, "master", self.project)
        self.assertEqual(called, [])

        project = SimpleNamespace(**{**self.project.__dict__, "deploy_on": "all"})
        supervisor.deploy_after_merge("JAIF-1", self.repo, "master", project)
        self.assertEqual(called, ["./deploy.sh"])

    def test_hotfix_and_release_are_skipped_even_when_the_base_is_master(self):
        called = []

        def runner(command, **options):
            called.append(command)
            return subprocess.CompletedProcess(command, 0, "ok", "")

        supervisor = self.supervisor(runner)
        project = SimpleNamespace(**{**self.project.__dict__, "base": "master", "hotfix_base": "master"})
        self.redis.data["ishotfix:JAIF-11"] = "1"
        supervisor.deploy_after_merge("JAIF-11", self.repo, "master", project)
        self.redis.data["isrelease:JAIF-12"] = "1"
        supervisor.deploy_after_merge("JAIF-12", self.repo, "master", project)

        self.assertEqual(called, [])

    def test_two_merges_serialize_and_run_newest_state_once(self):
        started = threading.Event()
        finish_first = threading.Event()
        running = 0
        max_running = 0
        calls = []
        call_lock = threading.Lock()

        def runner(command, **options):
            nonlocal running, max_running
            with call_lock:
                number = len(calls)
                calls.append(command)
                running += 1
                max_running = max(max_running, running)
            if number == 0:
                started.set()
                self.assertTrue(finish_first.wait(3))
            with call_lock:
                running -= 1
            return subprocess.CompletedProcess(command, 0, stdout=f"run {number}", stderr="")

        supervisor = self.supervisor(runner)
        first = threading.Thread(target=supervisor.deploy_after_merge,
                                 args=("JAIF-1", self.repo, "develop", self.project))
        first.start()
        self.assertTrue(started.wait(3))
        supervisor.deploy_after_merge("JAIF-2", self.repo, "develop", self.project)
        finish_first.set()
        first.join(3)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertEqual(max_running, 1)
        self.assertEqual({key for key, _ in self.jira.comments}, {"JAIF-1", "JAIF-2"})
        self.assertTrue(all("exit code 0" in body for _, body in self.jira.comments))
        self.assertEqual(len(list(self.logs.glob("deploy-*.log"))), 2)

    def test_failed_hook_is_visible_to_health_and_not_ticket_blocking(self):
        def runner(command, **options):
            return subprocess.CompletedProcess(command, 2, stdout="deploy failed", stderr="")

        self.supervisor(runner).deploy_after_merge("JAIF-9", self.repo, "develop", self.project)
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertEqual({key for key, _ in self.jira.comments}, {"JAIF-9"})
        self.assertIn("exit code 2", self.jira.comments[0][1])
        self.assertEqual(self.jira.transitions, [])

    def test_timeout_is_visible_to_health_and_does_not_change_ticket_state(self):
        def runner(command, **options):
            raise subprocess.TimeoutExpired(command, 60, output="deployment still running")

        self.supervisor(runner).deploy_after_merge("JAIF-13", self.repo, "develop", self.project)
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertIn("timed out after 1 minute", self.jira.comments[0][1])
        self.assertEqual(self.jira.transitions, [])

    def test_success_clears_a_previous_deploy_failure(self):
        self.redis.set("deploy-failure:JAIF", '{"ticket":"JAIF-1","outcome":"exit code 2"}', 3600)

        def runner(command, **options):
            return subprocess.CompletedProcess(command, 0, "deployed", "")

        self.supervisor(runner).deploy_after_merge("JAIF-14", self.repo, "develop", self.project)

        self.assertIsNone(self.redis.get("deploy-failure:JAIF"))

    def test_jira_comment_does_not_include_environment_secret(self):
        secret = "top-secret-deploy-token"

        def runner(command, **options):
            return subprocess.CompletedProcess(command, 0, stdout=f"used {os.environ['DEPLOY_TOKEN']}", stderr="")

        with patch.dict(os.environ, {"DEPLOY_TOKEN": secret}):
            self.supervisor(runner).deploy_after_merge("JAIF-10", self.repo, "develop", self.project)

        self.assertEqual(len(self.jira.comments), 1)
        self.assertNotIn(secret, self.jira.comments[0][1])
        self.assertIn("[REDACTED]", self.jira.comments[0][1])


if __name__ == "__main__":
    unittest.main()
