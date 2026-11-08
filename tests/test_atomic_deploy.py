"""Tests for atomic code and configuration deployment."""
import importlib.machinery
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import call, patch

from aiflow.config import ConfigError, check_configuration_version
from aiflow.supervisor import main


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("aiflow_deploy_command", str(ROOT / "bin" / "aiflow-deploy"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
DEPLOY_COMMAND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY_COMMAND)


def git(*arguments: str) -> str:
    result = subprocess.run(["git", *arguments], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def repository(root: Path, name: str) -> tuple[Path, Path, Path]:
    bare, seed, checkout = root / f"{name}.git", root / f"{name}-seed", root / name
    git("init", "--bare", "--initial-branch=main", str(bare))
    git("clone", str(bare), str(seed))
    git("-C", str(seed), "config", "user.email", "test@example.invalid")
    git("-C", str(seed), "config", "user.name", "Test")
    (seed / "revision").write_text("initial")
    git("-C", str(seed), "add", "revision")
    git("-C", str(seed), "commit", "-m", "initial")
    git("-C", str(seed), "push", "-u", "origin", "main")
    git("clone", str(bare), str(checkout))
    git("-C", str(checkout), "config", "user.email", "test@example.invalid")
    git("-C", str(checkout), "config", "user.name", "Test")
    return bare, seed, checkout


def add_revision(seed: Path, content: str, message: str) -> str:
    (seed / "revision").write_text(content)
    git("-C", str(seed), "commit", "-am", message)
    git("-C", str(seed), "push")
    return git("-C", str(seed), "rev-parse", "HEAD")


class ExistingRestartBehaviorTests(unittest.TestCase):
    def test_restart_requests_workers_to_leave_after_their_current_step(self):
        from types import SimpleNamespace

        config = SimpleNamespace(redis_host="redis", redis_port=6379, redis_db=5, redis_prefix="aiworkflow:",
                                 redis_password="", redis_user="")
        with patch("aiflow.config.load", return_value=config), patch("aiflow.supervisor.Redis") as redis_factory, \
                patch("aiflow.supervisor.time.time", return_value=123), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["restart"]), 0)

        redis_factory.return_value.set.assert_called_once_with("restart", "123", 7 * 24 * 3600)
        self.assertEqual(output.getvalue(), "restart requested for all workers\n")


class AtomicDeployTests(unittest.TestCase):
    def test_configuration_version_rejects_a_newer_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, config = root / "code", root / "config"
            code.mkdir()
            (code / "pyproject.toml").write_text('[project]\nversion = "4.0.0a1"\n')
            (config / "aiflow").mkdir(parents=True)
            (config / "aiflow.toml").write_text("# aiflow-version: 4.0.0a1\n[machines.wsl]\nworkers=1\n")
            (config / "aiflow" / "JAIF.toml").write_text(
                "# aiflow-version: 4.0.0a2\n[project]\njira_key='JAIF'\nrepo='ai-flow'\n")

            with self.assertRaisesRegex(ConfigError, "newer than code version"):
                check_configuration_version(config, code)

    def test_candidate_code_commit_is_pinned_even_if_remote_advances(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, seed, checkout = repository(root, "code")
            candidate = add_revision(seed, "candidate", "candidate")
            newest = add_revision(seed, "newest", "newest")
            worktree = root / "candidate-tree"

            previous, staged = DEPLOY_COMMAND._prepare_repo(checkout, worktree, candidate)

            self.assertNotEqual(previous, candidate)
            self.assertEqual(staged, candidate)
            self.assertNotEqual(staged, newest)
            self.assertEqual((worktree / "revision").read_text(), "candidate")
            DEPLOY_COMMAND._git(checkout, "worktree", "remove", "--force", str(worktree))

    def test_prepare_rejects_a_checkout_ahead_of_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, checkout = repository(root, "code")
            candidate = DEPLOY_COMMAND._git(checkout, "rev-parse", "HEAD")
            (checkout / "revision").write_text("unpushed")
            git("-C", str(checkout), "commit", "-am", "unpushed")

            with self.assertRaisesRegex(RuntimeError, "ahead of or diverged"):
                DEPLOY_COMMAND._prepare_repo(checkout, root / "candidate-tree", candidate)

    def test_candidate_check_preserves_live_workspace_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_tree = Path(temporary)
            version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
            (config_tree / "aiflow.toml").write_text(f"# aiflow-version: {version}\n[machines.wsl]\nworkers=1\n")
            live_root = Path(temporary) / "live-workspace"
            with patch.object(DEPLOY_COMMAND, "_run") as run:
                DEPLOY_COMMAND._check_candidate(ROOT, config_tree, live_root)

        self.assertEqual(run.call_args.args[0], [str(ROOT / "bin" / "aiflow-work"), "check"])
        self.assertEqual(run.call_args.kwargs["env"]["AIFLOW_CONFIG_DIR"], str(config_tree))
        self.assertEqual(run.call_args.kwargs["env"]["AIFLOW_ROOT"], str(live_root))

    def test_version_preflight_error_has_safe_actionable_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_tree = Path(temporary)
            (config_tree / "aiflow.toml").write_text(
                "# aiflow-version: 999.0.0\n[machines.wsl]\nworkers=1\n")
            with self.assertRaises(DEPLOY_COMMAND.ConfigurationVersionMismatch) as raised:
                DEPLOY_COMMAND._check_candidate(ROOT, config_tree, Path("/live"))

        detail = DEPLOY_COMMAND._preflight_detail(json.dumps(raised.exception.as_json()))
        self.assertIn(json.dumps(str(config_tree / "aiflow.toml")), detail)
        self.assertIn("999.0.0", detail)
        self.assertIn(tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"], detail)
        self.assertEqual(DEPLOY_COMMAND._preflight_detail("arbitrary child output"), "preflight failed")

    def test_target_lock_serializes_deployments(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(DEPLOY_COMMAND.tempfile, "gettempdir",
                                                                      return_value=temporary):
            lock = DEPLOY_COMMAND._acquire_lock("alpha", "first")
            with self.assertRaisesRegex(RuntimeError, "another deployment"):
                DEPLOY_COMMAND._acquire_lock("alpha", "second")
            DEPLOY_COMMAND._release_lock("alpha", "first")
            self.assertFalse(lock.exists())

    def test_rollback_refuses_to_discard_changes_made_after_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, seed, checkout = repository(root, "code")
            previous = DEPLOY_COMMAND._git(checkout, "rev-parse", "HEAD")
            candidate = add_revision(seed, "candidate", "candidate")
            DEPLOY_COMMAND._git(checkout, "fetch", "origin")
            DEPLOY_COMMAND._activate_repo(checkout, previous, candidate)
            operator_file = checkout / "operator-edit"
            operator_file.write_text("keep me")

            with self.assertRaisesRegex(RuntimeError, "changes were left intact"):
                DEPLOY_COMMAND._rollback_repository(checkout, previous, candidate)

            self.assertEqual(DEPLOY_COMMAND._git(checkout, "rev-parse", "HEAD"), candidate)
            self.assertEqual(operator_file.read_text(), "keep me")

    def test_restart_uses_deployed_configuration_for_services_and_workers(self):
        config = Path("/deployment/config")
        code = Path("/deployment/ai-flow")
        root = Path("/deployment/workspaces")
        calls = []
        with patch.object(DEPLOY_COMMAND, "platform") as host, \
                patch.object(DEPLOY_COMMAND, "_run", side_effect=lambda command, **kwargs: calls.append((command, kwargs))):
            host.system.return_value = "Linux"
            DEPLOY_COMMAND._restart_services(code, config, root)
            DEPLOY_COMMAND._restart_workers(code, config, root)

        self.assertEqual(calls[0][0], ["systemctl", "--user", "set-environment", f"AIFLOW_CONFIG_DIR={config}"])
        self.assertEqual(calls[1][0], [str(code / "bin" / "install-wsl-service"), "--control"])
        self.assertEqual(calls[1][1]["env"]["AIFLOW_CONFIG_DIR"], str(config))
        self.assertEqual(calls[2][0][-1], "aiflow-control.service")
        self.assertEqual(calls[3][0], [str(code / "bin" / "aiflow-work"), "restart"])
        self.assertEqual(calls[3][1]["env"]["AIFLOW_CONFIG_DIR"], str(config))

    def test_failed_apply_retains_stage_until_abort_restores_service_state(self):
        deployment_id = "test-rollback-service-state"
        target = "rollback-test"
        stage = Path(tempfile.gettempdir()) / f"aiflow-deploy-{deployment_id}"
        lock = DEPLOY_COMMAND._acquire_lock(target, deployment_id)
        stage.mkdir(mode=0o700)
        record = {"target": target, "lock": str(lock), "code": "/missing/code", "config": "/missing/config",
                  "code_before": "old-code", "code_after": "new-code", "config_before": "old-config",
                  "config_after": "new-config", "live_root": "/live", "inventory": [], "service_state": None}
        DEPLOY_COMMAND._write_record(stage, record)
        try:
            with patch.object(DEPLOY_COMMAND, "platform") as host, \
                    patch.object(DEPLOY_COMMAND, "_activate_repo"), \
                    patch.object(DEPLOY_COMMAND, "_capture_service_state", return_value={"snapshot": True}), \
                    patch.object(DEPLOY_COMMAND, "_restart_services", side_effect=RuntimeError("restart failed")), \
                    patch.object(DEPLOY_COMMAND, "_rollback_repository"), \
                    patch.object(DEPLOY_COMMAND, "_restore_service_state") as restore:
                host.system.return_value = "Linux"
                with self.assertRaisesRegex(RuntimeError, "restart failed"):
                    DEPLOY_COMMAND._finish_target(deployment_id, "apply", target)
                self.assertTrue(stage.exists())
                DEPLOY_COMMAND._finish_target(deployment_id, "abort", target)

            restore.assert_called_once_with({"snapshot": True})
            self.assertFalse(stage.exists())
            self.assertFalse(lock.exists())
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            DEPLOY_COMMAND._release_lock(target, deployment_id)

    def test_service_snapshot_restores_unit_files_and_previous_manager_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            unit_dir = home / ".config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "aiflow-control.service").write_text("old control unit")
            (unit_dir / "aiflow-health.timer").write_text("old health timer")
            stage = root / "stage"
            stage.mkdir()

            def systemctl(command, **_kwargs):
                arguments = command[2:]
                output = {
                    ("is-active", "aiflow-control.service"): ("active", 0),
                    ("is-enabled", "aiflow-control.service"): ("enabled", 0),
                    ("is-active", "aiflow-health.timer"): ("inactive", 3),
                    ("is-enabled", "aiflow-health.timer"): ("disabled", 1),
                    ("show-environment",): ("AIFLOW_CONFIG_DIR=/old/config", 0),
                }[tuple(arguments)]
                return subprocess.CompletedProcess(command, output[1], stdout=output[0], stderr="")

            with patch.object(Path, "home", return_value=home), \
                    patch.object(DEPLOY_COMMAND.subprocess, "run", side_effect=systemctl):
                snapshot = DEPLOY_COMMAND._capture_service_state(stage)

            (unit_dir / "aiflow-control.service").write_text("candidate control unit")
            (unit_dir / "aiflow-new.service").write_text("candidate new unit")
            calls = []
            with patch.object(DEPLOY_COMMAND, "_run", side_effect=lambda command: calls.append(command)):
                DEPLOY_COMMAND._restore_service_state(snapshot)

            self.assertEqual((unit_dir / "aiflow-control.service").read_text(), "old control unit")
            self.assertEqual((unit_dir / "aiflow-health.timer").read_text(), "old health timer")
            self.assertFalse((unit_dir / "aiflow-new.service").exists())
            self.assertIn(["systemctl", "--user", "set-environment", "AIFLOW_CONFIG_DIR=/old/config"], calls)
            self.assertIn(["systemctl", "--user", "start", "aiflow-control.service"], calls)
            self.assertIn(["systemctl", "--user", "stop", "aiflow-health.timer"], calls)

    def test_candidate_inventory_additions_are_staged_with_the_same_commit(self):
        replies = {
            "alpha": subprocess.CompletedProcess(["ssh"], 0, stdout=b'{"inventory":["alpha","beta"]}'),
            "beta": subprocess.CompletedProcess(["ssh"], 0, stdout=b'{"inventory":["alpha","beta","gamma"]}'),
            "gamma": subprocess.CompletedProcess(["ssh"], 0, stdout=b'{"inventory":["alpha","beta","gamma"]}'),
        }
        with patch.object(DEPLOY_COMMAND, "_ssh", side_effect=lambda target, *_args: replies[target]) as ssh:
            failed, staged, all_targets = DEPLOY_COMMAND._stage_all(("alpha",), b"script", "id", "commit-sha")

        self.assertEqual(failed, {})
        self.assertEqual(staged, {"alpha", "beta", "gamma"})
        self.assertEqual(all_targets, ("alpha", "beta", "gamma"))
        self.assertEqual([call.args[0] for call in ssh.call_args_list], ["alpha", "beta", "gamma"])
        self.assertTrue(all(call.args[4] == "commit-sha" for call in ssh.call_args_list))

    def test_preflight_failure_reports_file_and_version_without_child_output(self):
        payload = {"kind": "configuration-version", "file": "/config/aiflow/JAIF.toml",
                   "configuration_version": "4.2.0", "code_version": "4.1.0"}
        detail = DEPLOY_COMMAND._preflight_detail(json.dumps(payload))
        self.assertEqual(detail, 'preflight failed: configuration file "/config/aiflow/JAIF.toml" requires code version '
                                 '4.2.0 (candidate code is 4.1.0)')
        self.assertEqual(DEPLOY_COMMAND._preflight_detail('{"kind":"configuration-version","file":"secret"}'),
                         "preflight failed")

    def test_fleet_activation_failure_aborts_all_staged_targets(self):
        calls = []

        def ssh(target, _script, mode, *_args):
            calls.append((target, mode))
            code = 7 if (target, mode) == ("beta", "apply") else 0
            return subprocess.CompletedProcess(["ssh"], code)

        with patch.object(DEPLOY_COMMAND, "_ssh", side_effect=ssh), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = DEPLOY_COMMAND._apply_all(("alpha", "beta"), b"script", "id", "candidate")

        self.assertEqual(result, 1)
        self.assertEqual(calls, [("alpha", "apply"), ("beta", "apply"), ("alpha", "abort"), ("beta", "abort")])
        self.assertIn("alpha: rolled back after fleet activation failure", output.getvalue())
        self.assertIn("beta: failed (activation exit 7)", output.getvalue())

    def test_fleet_report_says_when_operator_checkout_changes_were_preserved(self):
        calls = []

        def ssh(target, _script, mode, *_args):
            calls.append((target, mode))
            if mode == "apply" and target == "beta":
                return subprocess.CompletedProcess(["ssh"], 7)
            if mode == "abort" and target == "alpha":
                return subprocess.CompletedProcess(["ssh"], 1,
                                                   stderr=b"deployment failed: changes were left intact")
            return subprocess.CompletedProcess(["ssh"], 0)

        with patch.object(DEPLOY_COMMAND, "_ssh", side_effect=ssh), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            DEPLOY_COMMAND._apply_all(("alpha", "beta"), b"script", "id", "candidate")

        self.assertIn("alpha: rollback failed (exit 1; checkout changes were left intact during rollback)",
                      output.getvalue())

    def test_worker_restart_failure_aborts_activated_fleet(self):
        calls = []

        def ssh(target, _script, mode, *_args):
            calls.append((target, mode))
            code = 9 if mode == "restart-workers" else 0
            return subprocess.CompletedProcess(["ssh"], code)

        with patch.object(DEPLOY_COMMAND, "_ssh", side_effect=ssh), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = DEPLOY_COMMAND._apply_all(("alpha", "beta"), b"script", "id", "candidate")

        self.assertEqual(result, 1)
        self.assertEqual(calls, [("alpha", "apply"), ("beta", "apply"), ("alpha", "restart-workers"),
                                 ("alpha", "abort"), ("beta", "abort")])
        self.assertIn("rolled back after worker restart exit 9", output.getvalue())

    def test_successful_deploy_finalizes_only_after_worker_restart(self):
        calls = []

        def ssh(target, _script, mode, *_args):
            calls.append((target, mode))
            return subprocess.CompletedProcess(["ssh"], 0)

        with patch.object(DEPLOY_COMMAND, "_ssh", side_effect=ssh), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = DEPLOY_COMMAND._apply_all(("alpha", "beta"), b"script", "id", "candidate")

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("alpha", "apply"), ("beta", "apply"), ("alpha", "restart-workers"),
                                 ("alpha", "finalize"), ("beta", "finalize")])
        self.assertEqual(output.getvalue(), "alpha: deployed\nbeta: deployed\n")

    def test_remote_command_dispatches_to_remote_handler(self):
        with patch.object(DEPLOY_COMMAND, "_remote_main", return_value=7) as remote:
            self.assertEqual(DEPLOY_COMMAND.main(["--remote", "stage", "id", "/code", "/config", "sha", "alpha"]), 7)

        remote.assert_called_once_with(["stage", "id", "/code", "/config", "sha", "alpha"])

    def test_atomic_deploy_documentation_allows_different_checkout_paths(self):
        documentation = (ROOT / "docs" / "v4" / "atomic-deploy.md").read_text()
        self.assertIn("Local and target checkout paths may differ", documentation)
        self.assertIn("AIFLOW_DEPLOY_REMOTE_CONFIG_DIR", documentation)


if __name__ == "__main__":
    unittest.main()
