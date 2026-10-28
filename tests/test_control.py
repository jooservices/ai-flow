import io
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from aiflow import health
from aiflow.config import ConfigError, load, parse_worker
from aiflow.control import MAX_CHANGES, Control, config_hash, make_plan
from aiflow.projects import desired_ids
from tests.test_supervisor import FakeJira, FakeRedis


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class FakeBackend:
    def __init__(self, present=(), active=()):
        self.present, self.active = set(present), set(active)
        self.calls = []

    def instances(self):
        return set(self.present), set(self.active)

    def enable(self, worker):
        self.calls.append(("enable", worker))
        self.present.add(worker)
        self.active.add(worker)

    def disable(self, worker):
        self.calls.append(("disable", worker))
        self.present.discard(worker)


class Machine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.general('[machines.wsl]\nworkers = 1\nmax_workers = 8\n')
        self.project("JAVM", 3)
        self.project("JOOWT", 2)

    def tearDown(self):
        self.tmp.cleanup()

    def general(self, text):
        write(self.dir / "aiflow.toml", text)

    def project(self, key, workers):
        write(self.dir / "aiflow" / f"{key}.toml", f'[project]\njira_key = "{key}"\nrepo = "{key.lower()}"\nworkers = {workers}\n')

    def load(self, worker_id=None):
        return load({"AIFLOW_MACHINE": "wsl"}, worker_id=worker_id, config_dir=self.dir)


class WorkerIdTests(Machine):
    def test_ids(self):
        self.assertEqual(parse_worker("2"), ("", 2))
        self.assertEqual(parse_worker("JAVM-3"), ("JAVM", 3))
        for bad in ("0", "JAVM-0", "javm-1", "JAVM", "", "1x", "JAVM-1-2"):
            with self.subTest(bad), self.assertRaises(ConfigError):
                parse_worker(bad)

    def test_desired_ids_and_expected_heartbeats(self):
        cfg = self.load()
        self.assertEqual(desired_ids(cfg.registry, "wsl"), ["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"])
        self.assertEqual(cfg.expected_slots, tuple(f"wsl:{i}" for i in ["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"]))

    def test_a_project_worker_knows_its_home(self):
        cfg = self.load("JAVM-2")
        self.assertEqual((cfg.home, cfg.slot, cfg.worker_label), ("JAVM", 2, "JAVM-2"))
        shared = self.load("1")
        self.assertEqual((shared.home, shared.worker_label), ("", "1"))

    def test_a_worker_outside_the_configuration_is_refused(self):
        for worker in ("JAVM-4", "NOPE-1", "2"):
            with self.subTest(worker), self.assertRaises(ConfigError):
                self.load(worker)

    def test_the_total_cannot_pass_max_workers(self):
        self.general('[machines.wsl]\nworkers = 2\nmax_workers = 6\n')
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertIn("max_workers is 6", str(caught.exception))
        self.general('[machines.wsl]\nworkers = 1\nmax_workers = 6\n')
        self.assertEqual(len(desired_ids(self.load().registry, "wsl")), 6)

    def test_workers_must_not_be_negative(self):
        self.project("JAVM", -1)
        with self.assertRaises(ConfigError):
            self.load()


class PlanTests(unittest.TestCase):
    def test_plan(self):
        plan = make_plan(["1", "A-1", "A-2"], present={"1", "A-1", "A-3", "A-4", "A-5"}, active={"1", "A-1", "A-3", "A-4"}, flagged={"A-4", "A-1"})
        self.assertEqual(plan.start, ["A-2"])
        self.assertEqual(plan.keep, ["A-1"])        # flagged but wanted again
        self.assertEqual(plan.retire, ["A-3"])      # running, not wanted, not flagged yet
        self.assertEqual(plan.disable, ["A-5"])     # not running, not wanted
        self.assertTrue(make_plan(["1"], {"1"}, {"1"}, set()).empty())


class ControlTests(Machine):
    def build(self, present=(), active=None, operator=None):
        self.backend = FakeBackend(present, present if active is None else active)
        self.redis = FakeRedis()
        self.logs = []
        self.control = Control(self.backend, self.redis, self.load, self.dir, operator, self.logs.append)

    def test_a_fresh_machine_gets_its_workers_a_few_per_cycle(self):
        self.build()
        self.control.cycle()
        self.assertEqual(len(self.backend.calls), MAX_CHANGES)
        self.control.cycle()
        self.assertEqual(sorted(self.backend.present), ["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"])
        self.control.cycle()
        self.assertEqual(len(self.backend.calls), 6)  # settled: nothing more to do

    def test_fewer_workers_retire_softly_then_are_disabled(self):
        self.build(present=["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"])
        self.project("JAVM", 1)
        self.control.cycle()
        self.assertIsNotNone(self.redis.get("retire:wsl:JAVM-2"))
        self.assertIsNotNone(self.redis.get("retire:wsl:JAVM-3"))
        self.assertEqual([c for c in self.backend.calls if c[0] == "disable"], [])  # still running: not touched
        self.backend.active -= {"JAVM-2", "JAVM-3"}  # they left after their step
        self.control.cycle()
        self.assertEqual(sorted(c[1] for c in self.backend.calls if c[0] == "disable"), ["JAVM-2", "JAVM-3"])
        self.assertIsNone(self.redis.get("retire:wsl:JAVM-2"))

    def test_a_worker_wanted_again_loses_its_retire_flag(self):
        self.build(present=["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"])
        self.project("JAVM", 1)
        self.control.cycle()
        self.project("JAVM", 3)
        self.control.cycle()
        self.assertIsNone(self.redis.get("retire:wsl:JAVM-3"))
        self.assertEqual(self.backend.calls, [])

    def test_a_wrong_configuration_changes_nothing(self):
        self.build(present=["1", "JAVM-1"])
        write(self.dir / "aiflow" / "JAVM.toml", "[project\nbroken")
        self.control.cycle()
        self.assertEqual(self.backend.calls, [])
        self.assertIn("nothing is changed", self.redis.get("control:error"))
        self.general('[machines.wsl]\nworkers = 3\nmax_workers = 4\n')  # over the cap
        self.project("JAVM", 3)
        self.control.cycle()
        self.assertEqual(self.backend.calls, [])

    def test_it_never_stops_every_worker(self):
        self.build(present=["1", "JAVM-1"])
        self.general('[machines.wsl]\nworkers = 0\n')
        self.project("JAVM", 0)
        self.project("JOOWT", 0)
        self.control.cycle()
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(self.redis.get("retire:wsl:1"))
        self.assertIn("refusing", self.redis.get("control:error"))

    def test_dry_run_changes_nothing(self):
        self.build()
        plan, _ = self.control.cycle(dry_run=True)
        self.assertEqual(len(plan.start), 6)
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(self.redis.get("control:confighash"))

    def test_a_changed_configuration_restarts_the_workers_and_the_control(self):
        calls = []
        self.build(present=["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"], operator=calls.append)
        _, changed = self.control.cycle()
        self.assertFalse(changed)
        self.assertIsNone(self.redis.get("restart"))
        self.assertEqual(len(calls), 1)  # the operator ran
        self.project("JOOWT", 2)  # same text: no change
        _, changed = self.control.cycle()
        self.assertFalse(changed)
        write(self.dir / "aiflow" / "JOOWT.toml", '[project]\njira_key = "JOOWT"\nrepo = "joowt"\nworkers = 2\nmax_parallel = 5\n')
        _, changed = self.control.cycle()
        self.assertTrue(changed)
        self.assertIsNotNone(self.redis.get("restart"))
        self.assertEqual(len(calls), 2)  # no operator run in the pass that asks for a restart

    def test_heartbeat_and_seen_marker(self):
        self.build(present=["1", "JAVM-1", "JAVM-2", "JAVM-3", "JOOWT-1", "JOOWT-2"])
        self.control.cycle()
        self.assertIsNotNone(self.redis.get("hb:wsl:control"))
        self.assertIsNotNone(self.redis.get("control:seen:wsl"))

    def test_hash_covers_every_file(self):
        before = config_hash(self.dir)
        write(self.dir / "operator.toml", "[operator]\nenabled = true\n")
        self.assertNotEqual(before, config_hash(self.dir))


class WorkerBehaviourTests(Machine):
    def sup(self, worker_id):
        cfg = self.load(worker_id)
        redis = FakeRedis()
        jira = FakeJira()
        jira.found = [{"key": k, "labels": labels, "status": "To Do", "updated": ""} for k, labels in (
            ("JAVM-1", []), ("JAVM-2", []), ("JOOWT-9", []), ("JOOWT-8", ["rework"]))]
        from aiflow.supervisor import Supervisor
        return Supervisor(cfg, jira, redis)

    def keys(self, sup):
        return [i["key"] for i in sup.candidates()]

    def test_worker_one_serves_only_its_project(self):
        self.assertEqual(self.keys(self.sup("JAVM-1")), ["JAVM-1", "JAVM-2"])

    def test_other_project_workers_prefer_home_then_take_turns(self):
        self.assertEqual(self.keys(self.sup("JAVM-2")), ["JAVM-1", "JAVM-2", "JOOWT-8", "JOOWT-9"])
        self.assertEqual(self.keys(self.sup("JOOWT-2")), ["JOOWT-8", "JOOWT-9", "JAVM-1", "JAVM-2"])

    def test_a_shared_worker_has_no_home(self):
        self.assertEqual(self.keys(self.sup("1"))[0], "JOOWT-8")  # rework first, then rounds

    def test_a_retired_worker_leaves(self):
        sup = self.sup("JAVM-3")
        sup.redis.set("retire:wsl:JAVM-3", "1", 10)
        self.assertTrue(sup.should_exit(int(time.time())))
        self.assertTrue(sup.retired)

    def test_another_worker_is_not_retired_by_that_flag(self):
        sup = self.sup("JAVM-2")
        sup.redis.set("retire:wsl:JAVM-3", "1", 10)
        self.assertFalse(sup.should_exit(int(time.time())))


class HealthTests(Machine):
    def test_health_report_shows_configured_and_running_worker_counts(self):
        cfg = replace(self.load(), expected_slots=("wsl:1", "wsl:2"), heartbeat_stale_seconds=60)
        redis = FakeRedis()
        now = int(time.time())
        redis.set("hb:wsl:1", str(now), 300)
        redis.set("hb:wsl:2", str(now - 61), 300)
        redis.set("hb:wsl:control", str(now), 300)

        with mock.patch("aiflow.health.load", return_value=cfg), \
             mock.patch("aiflow.health.Redis", return_value=redis), \
             mock.patch("aiflow.health.Jira", return_value=FakeJira()), \
             mock.patch("aiflow.health.problems", return_value=[]), \
             mock.patch("aiflow.health.usage_summary", return_value=None), \
             mock.patch("aiflow.health.events.configure"), mock.patch("aiflow.health.events.emit"), \
             mock.patch("aiflow.health.events.close"), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(health.main(), 0)

        self.assertIn("WORKERS wsl: configured 2, running 1", output.getvalue())

    def test_control_alerts(self):
        cfg = self.load()
        redis = FakeRedis()
        kinds = lambda: [k for k, _ in health.problems(cfg, redis, FakeJira())]
        self.assertNotIn("control", kinds())  # never deployed on this machine: no alert
        redis.set("control:seen:wsl", "1", 10)
        self.assertIn("control", kinds())
        redis.set("hb:wsl:control", str(int(time.time())), 10)
        self.assertNotIn("control", kinds())
        redis.set("control:error", "bad file", 10)
        self.assertIn("control-error", kinds())
