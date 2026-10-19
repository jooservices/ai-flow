import tempfile
import unittest
from pathlib import Path

from aiflow.config import ConfigError, check_profiles, expected_workers, load, parse_worker_id


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / f"{name}.env"
    path.write_text(text)
    return path


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir, "A", "AIFLOW_PROJECTS=JAVM\nAIFLOW_WORKERS=1\n")
        write(self.dir, "B", "AIFLOW_PROJECTS=JOOCX,JAIF,JOOS\nAIFLOW_WORKERS=2\nAIFLOW_WORKER_MAX_MINUTES=90\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_worker_id_is_split_from_the_right(self):
        self.assertEqual(parse_worker_id("B-2"), ("B", 2))
        self.assertEqual(parse_worker_id("jira-b-3"), ("jira-b", 3))
        for bad in ("B", "B-0", "B-x", "-2"):
            with self.assertRaises(ConfigError):
                parse_worker_id(bad)

    def test_worker_reads_its_profile(self):
        cfg = load(env={}, worker_id="B-2", profile_dir=self.dir)
        self.assertEqual(cfg.projects, ("JOOCX", "JAIF", "JOOS"))
        self.assertEqual((cfg.profile, cfg.slot, cfg.worker_max_minutes), ("B", 2, 90))
        self.assertTrue(cfg.owner.split(":")[1] == "B-2")

    def test_worker_outside_the_profile_is_refused(self):
        with self.assertRaises(ConfigError):
            load(env={}, worker_id="A-2", profile_dir=self.dir)

    def test_unknown_profile_is_refused(self):
        with self.assertRaises(ConfigError):
            load(env={}, worker_id="Z-1", profile_dir=self.dir)

    def test_shared_project_blocks_start(self):
        write(self.dir, "C", "AIFLOW_PROJECTS=JAIF\n")
        with self.assertRaises(ConfigError) as error:
            load(env={}, worker_id="A-1", profile_dir=self.dir)
        self.assertIn("JAIF", str(error.exception))

    def test_profile_without_projects_is_refused(self):
        with self.assertRaises(ConfigError):
            check_profiles({"X": {}})

    def test_legacy_supervisor_is_unchanged(self):
        cfg = load(env={"AIFLOW_SLOT": "2", "AIFLOW_PROJECTS": "JAIF"}, profile_dir=self.dir)
        self.assertEqual((cfg.profile, cfg.worker_label, cfg.projects), ("", "2", ("JAIF",)))

    def test_health_expects_every_worker_of_every_profile(self):
        self.assertEqual(expected_workers({}, "wsl", self.dir), ("wsl:A-1", "wsl:B-1", "wsl:B-2"))
        empty = Path(tempfile.mkdtemp())
        self.assertEqual(expected_workers({"AIFLOW_EXPECTED_SLOTS": "wsl:1"}, "wsl", empty), ("wsl:1",))

    def test_worker_leaves_on_restart_request_or_after_max_runtime(self):
        import time
        from types import SimpleNamespace
        from aiflow.supervisor import Supervisor

        data = {}
        worker = Supervisor.__new__(Supervisor)
        worker.redis = SimpleNamespace(get=data.get)
        worker.cfg = SimpleNamespace(worker_max_minutes=0)
        now = int(time.time())
        self.assertFalse(worker.should_exit(now))
        data["restart"] = str(now + 5)
        self.assertTrue(worker.should_exit(now))
        data.clear()
        worker.cfg = SimpleNamespace(worker_max_minutes=1)
        self.assertFalse(worker.should_exit(now))
        self.assertTrue(worker.should_exit(now - 120))


if __name__ == "__main__":
    unittest.main()
