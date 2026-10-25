import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiflow import health
from aiflow.config import load as load_settings
from aiflow.control import Control
from aiflow.supervisor import Supervisor, main
from tests.test_supervisor import FakeJira, FakeRedis


class FakeBackend:
    def __init__(self, workers):
        self.present = set(workers)
        self.active = set(workers)

    def instances(self):
        return set(self.present), set(self.active)

    def enable(self, worker):
        self.present.add(worker)
        self.active.add(worker)

    def disable(self, worker):
        self.present.discard(worker)


class TicketJira(FakeJira):
    def __init__(self, issue, timeline):
        super().__init__()
        self.issue, self.timeline = issue, timeline
        self.found = [issue]

    def search(self, jql, limit=50):
        return [] if jql.startswith("parent in ") else self.found

    def view(self, key, full=False):
        return {"key": key, "status": "In Progress", "agent": "Codex", "agent_field": "Codex",
                "summary": "Finish the current worker step", "description": "Acceptance criteria"}

    def read_field(self, key, field):
        return None

    def comments(self, key, limit=50):
        return []

    def set_workspace(self, *args):
        pass

    def transition(self, key, status):
        self.moves.append((key, status))
        if status == "In Review":
            self.timeline.append("ticket.completed")
        return True


class FakeRepo:
    def __init__(self, root, name, company):
        self.root, self.name = root, name

    def exists(self):
        return True

    def has_remote(self):
        return True

    def ensure_worktree(self, key, summary, start):
        return self.root / "worktree", f"{key}-worker"

    def adopt_remote(self, workdir, branch):
        pass

    def ahead(self, workdir, base):
        return 1

    def commit_subjects(self, workdir, base):
        return []

    def push(self, workdir, branch, lease=False):
        pass


class WorkerScalingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "aiflow").mkdir()
        self.config_dir = self.root
        self.write_workers(2)
        for key in ("JAVM", "JOOWT"):
            (self.root / "aiflow" / f"{key}.toml").write_text(
                f'[project]\njira_key = "{key}"\nrepo = "{key.lower()}"\nworkers = 0\n')
        self.redis = FakeRedis()

    def tearDown(self):
        self.tmp.cleanup()

    def write_workers(self, count):
        (self.root / "aiflow.toml").write_text(f"[machines.wsl]\nworkers = {count}\nmax_workers = 4\n")

    def load(self, worker_id=None):
        cfg = load_settings({"AIFLOW_MACHINE": "wsl", "AIFLOW_POLL_SECONDS": "0"},
                            worker_id=worker_id, config_dir=self.config_dir)
        return replace(cfg, root=self.root)

    def test_configuration_check_reports_configured_and_running_counts(self):
        cfg = self.load()
        with mock.patch("aiflow.config.load", return_value=cfg), \
             mock.patch("aiflow.health.running_worker_count", return_value="2"), \
             mock.patch("aiflow.supervisor.operator_status.check_line", return_value="operator: test"), \
             mock.patch("builtins.print") as output:
            self.assertEqual(main(["check"]), 0)
        self.assertIn("2 configured worker(s)", output.call_args_list[0].args[0])
        self.assertIn("2 running", output.call_args_list[0].args[0])

    def test_graceful_worker_exit_clears_heartbeat_from_health_count(self):
        cfg = self.load()
        cfg = replace(cfg, expected_slots=("wsl:1", "wsl:2"), heartbeat_stale_seconds=300)
        now = int(time.time())
        for worker in ("1", "2", "3"):
            self.redis.set(f"hb:wsl:{worker}", str(now), 300)
        retired_cfg = replace(cfg, slot=3)
        from aiflow.worker_scaling import clear_worker_heartbeat

        clear_worker_heartbeat(self.redis, retired_cfg)
        self.assertEqual(health.worker_counts(cfg, self.redis, now), (2, 2))

    def test_two_four_two_drill_completes_real_ticket_path_before_worker_exit(self):
        backend = FakeBackend(("1", "2"))
        controller = Control(backend, self.redis, self.load, self.config_dir)
        controller.cycle()
        self.write_workers(4)
        up, _ = controller.cycle()
        self.assertEqual(up.start, ["3", "4"])
        self.redis.set("restart", str(int(time.time()) - 30), 3600)

        timeline = []
        issue = {"key": "JAVM-68", "status": "In Progress", "labels": [],
                 "updated": (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000%z")}
        jira = TicketJira(issue, timeline)

        def emit(name, **fields):
            if name == "worker.retired":
                timeline.append("worker.exited")

        def run_process(*args, **kwargs):
            timeline.append("process.started")
            self.assertTrue(kwargs["heartbeat"]())
            self.write_workers(2)
            down, _ = controller.cycle()
            self.assertEqual(down.retire, ["3", "4"])
            self.assertNotIn(("JAVM-68", "In Review"), jira.moves)
            timeline.append("process.returned")
            return 0, "ok"

        def config_load(worker_id=None):
            return self.load(worker_id)

        def reserve(*args):
            self.redis.acquire_session("wsl", "Codex", "session", 120)
            return "Codex", "session"

        with mock.patch("aiflow.config.load", side_effect=config_load), \
             mock.patch("aiflow.supervisor.Redis", return_value=self.redis), \
             mock.patch("aiflow.supervisor.Jira", return_value=jira), \
             mock.patch("aiflow.supervisor.Repo", FakeRepo), \
             mock.patch("aiflow.supervisor.agents.build_command", return_value=(["fake-ai"], None)), \
             mock.patch("aiflow.supervisor.agents.final_text", return_value="DONE: implemented"), \
             mock.patch("aiflow.supervisor.agents.verdict", return_value=("DONE", "implemented")), \
             mock.patch("aiflow.supervisor.usage.report", return_value={}), \
             mock.patch("aiflow.supervisor.runner.run", side_effect=run_process), \
             mock.patch("aiflow.supervisor.events.configure"), \
             mock.patch("aiflow.supervisor.events.emit", side_effect=emit), \
             mock.patch("aiflow.supervisor.events.close"), \
             mock.patch("aiflow.supervisor.signal.signal"), \
             mock.patch.object(Supervisor, "reserve_agent", side_effect=reserve), \
             mock.patch.object(Supervisor, "probe_ais"), \
             mock.patch.object(Supervisor, "publish_versions"), \
             mock.patch.object(Supervisor, "publish_ais"), \
             mock.patch.object(Supervisor, "recheck_blocked"), \
             mock.patch.object(Supervisor, "sweep_worktrees"), \
             mock.patch.object(Supervisor, "sweep_containers"):
            self.assertEqual(main(["--id", "3"]), 3)

        self.assertLess(timeline.index("process.returned"), timeline.index("ticket.completed"))
        self.assertLess(timeline.index("ticket.completed"), timeline.index("worker.exited"))
        self.assertIsNone(self.redis.get("hb:wsl:3"))
        backend.active.difference_update(("3", "4"))
        settled, _ = controller.cycle()
        self.assertEqual(settled.disable, ["3", "4"])
        self.assertEqual(backend.present, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
