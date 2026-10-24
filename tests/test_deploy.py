import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiflow import deploy, health
from aiflow.config import load
from aiflow.gitops import GitError
from aiflow.supervisor import Supervisor


class FakeProcess:
    def __init__(self, stdout="", returncode=0, expire_count=0, communicate=None, sleep_on_timeout=False):
        self.stdout = stdout.encode()
        self.returncode = returncode
        self.expire_count = expire_count
        self.communicate_callback = communicate
        self.sleep_on_timeout = sleep_on_timeout
        self.pid = 12345

    def communicate(self, timeout=None):
        if self.communicate_callback:
            return self.communicate_callback(timeout)
        if self.expire_count:
            self.expire_count -= 1
            if self.sleep_on_timeout and timeout:
                time.sleep(timeout)
            raise subprocess.TimeoutExpired("fake deploy", timeout, output=self.stdout)
        return self.stdout, None

    def poll(self):
        return self.returncode if not self.expire_count else None


class FakeDeployRepo:
    def __init__(self, path):
        self.path = path
        self.refreshed = []
        self.checked_out = []
        self.refresh_error = None
        self.checkout_error = None

    def refresh_base(self, base):
        self.refreshed.append(base)
        if self.refresh_error:
            raise self.refresh_error

    def checkout_deploy_base(self, base):
        self.checked_out.append(base)
        if self.checkout_error:
            raise self.checkout_error


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.renewals = []

    def enqueue_deploy(self, lock_name, pending_name, active_name, seen_name, owner, ticket, payload, ttl, seen_ttl):
        with self.lock:
            if seen_name in self.data:
                return 1 if self.data.get(lock_name) == owner else 2
            self.data[seen_name] = ticket
            if lock_name in self.data:
                current = self.data.get(pending_name, "")
                self.data[pending_name] = f"{current}\n{payload}".strip("\n")
                return 0
            self.data[lock_name] = owner
            self.data[active_name] = payload
            return 1

    def finish_deploy(self, lock_name, pending_name, active_name, owner, ttl, active_batch):
        with self.lock:
            if self.data.get(lock_name) != owner:
                return -1
            active = self.data.get(active_name)
            if active and active != active_batch:
                return active
            self.data.pop(active_name, None)
            pending = self.data.pop(pending_name, "")
            if pending:
                self.data[active_name] = pending
                return pending
            self.data.pop(lock_name, None)
            return ""

    def set(self, name, value, ttl):
        self.data[name] = value

    def delete(self, name):
        self.data.pop(name, None)

    def renew(self, name, owner, ttl):
        with self.lock:
            self.renewals.append((name, owner, ttl))
            return self.data.get(name) == owner

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
    def test_default_runner_executes_the_command_in_the_main_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, logs = Path(tmp) / "main", Path(tmp) / "logs"
            cwd.mkdir()

            result = deploy.run("printf 'runner ready'", cwd, 1, logs)

            self.assertTrue(result.succeeded)
            self.assertEqual(result.log_path.read_text(), "runner ready\n")

    def test_success_records_output_and_returns_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd, logs = Path(tmp) / "main", Path(tmp) / "logs"
            cwd.mkdir()
            calls = []

            def runner(command, **options):
                calls.append((command, options))
                return FakeProcess("deployed\nready")

            result = deploy.run("./deploy.sh", cwd, 7, logs, runner)

            self.assertTrue(result.succeeded)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(calls[0][1]["cwd"], cwd)
            self.assertTrue(calls[0][1]["start_new_session"])
            self.assertEqual(result.log_path.read_text(), "deployed\nready\n")
            self.assertIn("deployed", result.tail)

    def test_failure_is_reported_with_short_output_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"

            def runner(command, **options):
                return FakeProcess("first\nlast failure", returncode=7)

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
                return FakeProcess(f"started with {secret}\nwaiting", expire_count=1, sleep_on_timeout=True)

            beats = []
            result = deploy.run("./deploy.sh", Path(tmp), 1, logs, runner, {"DEPLOY_TOKEN": secret},
                                heartbeat_seconds=0.01, heartbeat=lambda: beats.append(True) or True)
            summary = deploy.comment(result, 1, str(result.log_path))
            log = result.log_path.read_text()

            self.assertTrue(result.succeeded)
            self.assertEqual(beats, [True])
            self.assertNotIn(secret, summary)
            self.assertNotIn(secret, log)
            self.assertIn("[REDACTED]", summary)

    def test_timeout_terminates_the_process_group_and_redacts_its_output(self):
        secret = "timeout-secret"
        with tempfile.TemporaryDirectory() as tmp:
            process = FakeProcess(f"failed with {secret}", expire_count=5)
            sent_signals = []

            def kill_group(pid, sig):
                self.assertEqual(pid, process.pid)
                sent_signals.append(sig)
                if sig == signal.SIGTERM:
                    process.expire_count = 0
                    process.returncode = -signal.SIGTERM

            with patch("aiflow.deploy.time.monotonic", side_effect=[0, 0, 61]), \
                    patch("aiflow.deploy.os.killpg", side_effect=kill_group):
                result = deploy.run("./deploy.sh", Path(tmp), 1, Path(tmp) / "logs", lambda *_args, **_kwargs: process,
                                    {"DEPLOY_TOKEN": secret}, heartbeat_seconds=120)

            self.assertTrue(result.timed_out)
            self.assertNotIn(secret, result.log_path.read_text())
            self.assertIn("[REDACTED]", result.tail)
            self.assertEqual(sent_signals, [signal.SIGTERM, signal.SIGKILL])


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
        self.repo = FakeDeployRepo(self.root / "main-clone")
        self.repo.path.mkdir()
        self.project = self.cfg.project("JAIF-1")

    def tearDown(self):
        self.tmp.cleanup()

    def supervisor(self, runner):
        return Supervisor(self.cfg, self.jira, self.redis, process_factory=runner, deploy_log_dir=self.logs)

    def test_claiming_a_pending_batch_is_idempotent_after_a_lost_reply(self):
        lock_name, pending_name, active_name = "deploy-lock:JAIF", "deploy-pending:JAIF", "deploy-active:JAIF"
        self.redis.data[lock_name] = "owner"
        self.redis.data[active_name] = "first-event"
        self.redis.data[pending_name] = "second-event"

        claimed = self.redis.finish_deploy(lock_name, pending_name, active_name, "owner", 60, "first-event")
        retried = self.redis.finish_deploy(lock_name, pending_name, active_name, "owner", 60, "first-event")

        self.assertEqual(claimed, "second-event")
        self.assertEqual(retried, claimed)
        self.assertEqual(self.redis.data[active_name], "second-event")
        self.assertEqual(self.redis.data[lock_name], "owner")

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
            return FakeProcess("deployed")

        self.supervisor(runner).after_merge("JAIF-15", self.repo, "develop")

        self.assertEqual(called, [("./deploy.sh", self.repo.path)])
        self.assertEqual([key for key, _ in self.jira.comments], ["JAIF-15"])
        self.assertEqual(self.repo.checked_out, ["develop"])

    def test_hotfix_and_release_branches_are_skipped_unless_all_is_set(self):
        called = []

        def runner(command, **options):
            called.append(command)
            return FakeProcess("ok")

        supervisor = self.supervisor(runner)
        supervisor.deploy_after_merge("JAIF-1", self.repo, "master", self.project)
        self.assertEqual(called, [])

        project = SimpleNamespace(**{**self.project.__dict__, "deploy_on": "all"})
        supervisor.deploy_after_merge("JAIF-2", self.repo, "master", project)
        self.assertEqual(called, ["./deploy.sh"])

    def test_hotfix_and_release_are_skipped_even_when_the_base_is_master(self):
        called = []

        def runner(command, **options):
            called.append(command)
            return FakeProcess("ok")

        supervisor = self.supervisor(runner)
        project = SimpleNamespace(**{**self.project.__dict__, "base": "master", "hotfix_base": "master"})
        self.redis.data["ishotfix:JAIF-11"] = "1"
        supervisor.deploy_after_merge("JAIF-11", self.repo, "master", project)
        self.redis.data["isrelease:JAIF-12"] = "1"
        supervisor.deploy_after_merge("JAIF-12", self.repo, "master", project)

        self.assertEqual(called, [])

    def test_duplicate_merge_event_runs_only_once(self):
        calls = []

        def runner(command, **options):
            calls.append(command)
            return FakeProcess("ok")

        supervisor = self.supervisor(runner)
        supervisor.deploy_after_merge("JAIF-20", self.repo, "develop", self.project)
        supervisor.deploy_after_merge("JAIF-20", self.repo, "develop", self.project)

        self.assertEqual(calls, ["./deploy.sh"])
        self.assertEqual([key for key, _ in self.jira.comments], ["JAIF-20"])

    def test_long_deploy_renews_worker_heartbeat_and_project_lock(self):
        self.cfg = replace(self.cfg, heartbeat_seconds=0.01)

        def runner(command, **options):
            return FakeProcess("still running", expire_count=1, sleep_on_timeout=True)

        self.supervisor(runner).deploy_after_merge("JAIF-21", self.repo, "develop", self.project)

        self.assertTrue(self.redis.renewals)
        self.assertEqual(self.redis.renewals[0][0], "deploy-lock:JAIF")
        self.assertIsNotNone(self.redis.get("hb:test:1"))

    def test_dirty_main_clone_skips_command_and_reports_health(self):
        self.repo.checkout_error = GitError("dirty working tree")
        called = []

        def runner(command, **options):
            called.append(command)
            return FakeProcess("should not run")

        self.supervisor(runner).deploy_after_merge("JAIF-22", self.repo, "develop", self.project)
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertEqual(called, [])
        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertIn("could not be prepared", self.jira.comments[0][1])

    def test_unexpected_runner_error_is_reported_without_blocking_the_merge(self):
        def runner(command, **options):
            raise ValueError("runner failed")

        supervisor = self.supervisor(runner)
        supervisor.after_merge("JAIF-23", self.repo, "develop")
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertEqual(supervisor.jira.transitions, [])
        self.assertTrue(any("could not complete" in body for _, body in self.jira.comments))

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

            def communicate(_timeout):
                nonlocal running, max_running
                with call_lock:
                    running += 1
                    max_running = max(max_running, running)
                if number == 0:
                    started.set()
                    self.assertTrue(finish_first.wait(3))
                with call_lock:
                    running -= 1
                return f"run {number}".encode(), None

            return FakeProcess(communicate=communicate)

        supervisor = self.supervisor(runner)
        first = threading.Thread(target=supervisor.deploy_after_merge,
                                 args=("JAIF-1", self.repo, "develop", self.project))
        frozen_now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
        with patch("aiflow.deploy.datetime") as clock:
            clock.now.return_value = frozen_now
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

    def test_runner_error_does_not_strand_a_queued_merge(self):
        started = threading.Event()
        finish_first = threading.Event()
        calls = []

        def runner(command, **options):
            calls.append(command)
            if len(calls) == 1:
                started.set()
                self.assertTrue(finish_first.wait(3))
                raise ValueError("runner failed")
            return FakeProcess("recovered")

        supervisor = self.supervisor(runner)
        first = threading.Thread(target=supervisor.deploy_after_merge,
                                 args=("JAIF-24", self.repo, "develop", self.project))
        first.start()
        self.assertTrue(started.wait(3))
        supervisor.deploy_after_merge("JAIF-25", self.repo, "develop", self.project)
        finish_first.set()
        first.join(3)

        self.assertFalse(first.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.redis.get("deploy-lock:JAIF"))
        self.assertTrue(any("could not complete (ValueError)" in body for _, body in self.jira.comments))
        self.assertTrue(any("exit code 0" in body for _, body in self.jira.comments))

    def test_failed_hook_is_visible_to_health_and_not_ticket_blocking(self):
        def runner(command, **options):
            return FakeProcess("deploy failed", returncode=2)

        self.supervisor(runner).deploy_after_merge("JAIF-9", self.repo, "develop", self.project)
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertEqual({key for key, _ in self.jira.comments}, {"JAIF-9"})
        self.assertIn("exit code 2", self.jira.comments[0][1])
        self.assertEqual(self.jira.transitions, [])

    def test_timeout_is_visible_to_health_and_does_not_change_ticket_state(self):
        process = FakeProcess("deployment still running", expire_count=5)

        def runner(command, **options):
            return process

        def kill_group(_pid, sig):
            if sig == signal.SIGTERM:
                process.expire_count = 0
                process.returncode = -signal.SIGTERM

        supervisor = self.supervisor(runner)
        supervisor.cfg = replace(supervisor.cfg, heartbeat_seconds=120)
        with patch("aiflow.deploy.time.monotonic", side_effect=[0, 0, 61]), \
                patch("aiflow.deploy.os.killpg", side_effect=kill_group):
            supervisor.deploy_after_merge("JAIF-13", self.repo, "develop", self.project)
        issues = health.problems(self.cfg, self.redis, self.jira)

        self.assertIn("deploy-JAIF", {kind for kind, _ in issues})
        self.assertIn("timed out after 1 minute", self.jira.comments[0][1])
        self.assertEqual(self.jira.transitions, [])

    def test_success_clears_a_previous_deploy_failure(self):
        self.redis.set("deploy-failure:JAIF", '{"ticket":"JAIF-1","outcome":"exit code 2"}', 3600)

        def runner(command, **options):
            return FakeProcess("deployed")

        self.supervisor(runner).deploy_after_merge("JAIF-14", self.repo, "develop", self.project)

        self.assertIsNone(self.redis.get("deploy-failure:JAIF"))

    def test_jira_comment_does_not_include_environment_secret(self):
        secret = "top-secret-deploy-token"

        def runner(command, **options):
            return FakeProcess(f"used {os.environ['DEPLOY_TOKEN']}")

        with patch.dict(os.environ, {"DEPLOY_TOKEN": secret}):
            self.supervisor(runner).deploy_after_merge("JAIF-10", self.repo, "develop", self.project)

        self.assertEqual(len(self.jira.comments), 1)
        self.assertNotIn(secret, self.jira.comments[0][1])
        self.assertIn("[REDACTED]", self.jira.comments[0][1])


if __name__ == "__main__":
    unittest.main()


class LogNameTests(unittest.TestCase):
    def test_two_runs_in_the_same_clock_tick_write_two_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            frozen_now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
            with patch("aiflow.deploy.datetime") as clock:
                clock.now.return_value = frozen_now
                for _ in range(2):
                    deploy.run("true", Path(tmp), 1, logs)
            log_paths = list(logs.glob("deploy-*.log"))
            self.assertEqual(len(log_paths), 2)
            self.assertEqual(len({path.name for path in log_paths}), 2)
