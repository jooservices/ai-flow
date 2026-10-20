import tempfile
import unittest
from pathlib import Path

from aiflow.config import ConfigError, load, parse_worker_id


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir / "aiflow.toml", '[defaults]\nqa = "Claude:sonnet"\nmerge_method = "squash"\n[machines.wsl]\nworkers = 2\n')
        write(self.dir / "aiflow" / "ai.toml", '[project]\njira_key = "JAIF"\nrepo = "ai-flow"\ndatabase = "ai-flow"\nci_required = true\nmax_parallel = 1\n')
        write(self.dir / "aiflow" / "crawler.toml", '[project]\njira_key = "JOOCX"\nrepo = "crawlerx"\nreviewer = "Codex"\nauto_merge = false\nhuman_on_deployment = false\n')

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **env):
        return load(env={"AIFLOW_MACHINE": "wsl", **env}, config_dir=self.dir)

    def test_every_file_is_loaded(self):
        cfg = self.load()
        self.assertEqual(sorted(cfg.projects), ["JAIF", "JOOCX"])
        self.assertTrue(cfg.registry.from_files)

    def test_project_beats_defaults_and_defaults_beat_env(self):
        cfg = self.load(AIFLOW_QA_DEFAULT="Grok", AIFLOW_REVIEWER_DEFAULT="Grok", AIFLOW_MERGE_METHOD="rebase")
        ai, crawler = cfg.project("JAIF-3"), cfg.project("JOOCX-9")
        self.assertEqual((ai.qa, ai.reviewer, ai.merge_method), ("Claude:sonnet", "Grok", "squash"))  # toml default, env, toml default
        self.assertEqual((crawler.reviewer, crawler.auto_merge, ai.auto_merge), ("Codex", False, True))
        self.assertEqual((ai.ci_required, crawler.ci_required, ai.max_parallel), (True, False, 1))
        self.assertEqual((ai.human_on_deployment, crawler.human_on_deployment), (True, False))
        self.assertEqual((ai.database, crawler.database), ("ai-flow", ""))

    def test_deploy_settings_default_and_project_override(self):
        write(self.dir / "aiflow" / "crawler.toml", '[project]\njira_key = "JOOCX"\nrepo = "crawlerx"\n'
              'deploy_command = "./deploy.sh"\ndeploy_timeout_minutes = 12\ndeploy_on = "all"\n')
        cfg = self.load()
        ai, crawler = cfg.project("JAIF-3"), cfg.project("JOOCX-9")
        self.assertEqual((ai.deploy_command, ai.deploy_timeout_minutes, ai.deploy_on), ("", 30, "base"))
        self.assertEqual((crawler.deploy_command, crawler.deploy_timeout_minutes, crawler.deploy_on), ("./deploy.sh", 12, "all"))

    def test_workers_of_the_machine(self):
        cfg = self.load()
        self.assertEqual((cfg.workers, cfg.expected_slots), (2, ("wsl:1", "wsl:2")))
        self.assertEqual(load(env={"AIFLOW_MACHINE": "mac", "AIFLOW_EXPECTED_SLOTS": "mac:1"}, config_dir=self.dir).expected_slots, ("mac:1",))

    def test_machine_ai_limits_are_independent_of_worker_count(self):
        write(self.dir / "aiflow.toml", '[machines.wsl]\nworkers = 10\n[machines.wsl.ai_limits]\ncodex = 4\nclaude = 2\nopencode = 4\n')
        cfg = self.load()
        self.assertEqual(cfg.workers, 10)
        self.assertEqual(cfg.expected_slots, tuple(f"wsl:{number}" for number in range(1, 11)))
        self.assertEqual(cfg.ai_limits, {"codex": 4, "claude": 2, "opencode": 4})
        self.assertNotIn("grok", cfg.ai_limits)
        self.assertEqual(load(env={"AIFLOW_MACHINE": "wsl"}, worker_id="10", config_dir=self.dir).slot, 10)

    def test_ai_limits_can_be_omitted(self):
        self.assertEqual(self.load().ai_limits, {})

    def test_worker_number_is_checked(self):
        self.assertEqual(load(env={"AIFLOW_MACHINE": "wsl"}, worker_id="2", config_dir=self.dir).slot, 2)
        with self.assertRaises(ConfigError):
            load(env={"AIFLOW_MACHINE": "wsl"}, worker_id="3", config_dir=self.dir)
        for bad in ("0", "x", "B-2"):
            with self.assertRaises(ConfigError):
                parse_worker_id(bad)

    def test_unknown_project_is_refused(self):
        with self.assertRaises(ConfigError):
            self.load().project("NOPE-1")

    def test_mistakes_are_reported(self):
        cases = {
            "twice": ('[project]\njira_key = "JAIF"\nrepo = "x"\n', "declared in"),
            "typo": ('[project]\njira_key = "ABC"\nrepo = "x"\nauto_mrege = true\n', "unknown setting"),
            "type": ('[project]\njira_key = "ABC"\nrepo = "x"\nmax_parallel = "2"\n', "must be int"),
            "norepo": ('[project]\njira_key = "ABC"\n', "`repo` is required"),
            "database": ('[project]\njira_key = "ABC"\nrepo = "x"\ndatabase = "Invalid Name"\n', "database must be"),
            "method": ('[project]\njira_key = "ABC"\nrepo = "x"\nmerge_method = "ff"\n', "merge_method"),
            "deploy timeout": ('[project]\njira_key = "ABC"\nrepo = "x"\ndeploy_timeout_minutes = 0\n', "deploy_timeout_minutes"),
            "deploy scope": ('[project]\njira_key = "ABC"\nrepo = "x"\ndeploy_on = "release"\n', "deploy_on must be"),
            "section": ('[proj]\njira_key = "ABC"\n', "[project]"),
            "syntax": ("[project\n", "aiflow"),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                write(self.dir / "aiflow" / "zz.toml", text)
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertIn(expected, str(caught.exception))

    def test_bad_general_file(self):
        for text, expected in (("[machines.wsl]\nworkers = 0\n", "workers"), ("[extra]\n", "unknown section"), ("[defaults]\nbase = 3\n", "must be str")):
            with self.subTest(text):
                write(self.dir / "aiflow.toml", text)
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertIn(expected, str(caught.exception))

    def test_bad_machine_ai_limits(self):
        cases = (
            ("[machines.wsl]\nworkers = 2\n[machines.wsl.ai_limits]\ngemini = 1\n", "unknown setting"),
            ("[machines.wsl]\nworkers = 2\n[machines.wsl.ai_limits]\ncodex = true\n", "must be int"),
            ("[machines.wsl]\nworkers = 2\n[machines.wsl.ai_limits]\ncodex = -1\n", "zero or greater"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                write(self.dir / "aiflow.toml", text)
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertIn(expected, str(caught.exception))

    def test_without_files_the_env_applies(self):
        with tempfile.TemporaryDirectory() as empty:
            cfg = load(env={"AIFLOW_PROJECTS": "JAIF,JOOS", "AIFLOW_DEFAULT_REPO": "ai-flow", "AIFLOW_CI_REQUIRED_REPOS": "ai-flow",
                            "AIFLOW_DEVELOPER_DEFAULT": "Codex:gpt-6-luna:xhigh"}, config_dir=Path(empty))
        self.assertFalse(cfg.registry.from_files)
        self.assertEqual(cfg.projects, ("JAIF", "JOOS"))
        project = cfg.project("ANY-1")
        self.assertEqual((project.repo, project.ci_required, project.developer), ("ai-flow", True, "Codex:gpt-6-luna:xhigh"))
        self.assertTrue(project.human_on_deployment)


if __name__ == "__main__":
    unittest.main()


class RuntimeSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir / "aiflow" / "ai.toml", '[project]\njira_key = "JAIF"\nrepo = "ai-flow"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, general: str, **env):
        write(self.dir / "aiflow.toml", general + '\n[machines.wsl]\nworkers = 2\n')
        return load(env={"AIFLOW_MACHINE": "wsl", **env}, config_dir=self.dir)

    def test_runtime_beats_env(self):
        cfg = self.load('[runtime]\npoll_seconds = 10\nagent_order = ["Codex", "Claude"]\nhealth_ticket = "JAIF-12"',
                        AIFLOW_POLL_SECONDS="99", AIFLOW_AGENT_ORDER="Grok", AIFLOW_HEALTH_TICKET="X-1")
        self.assertEqual((cfg.poll_seconds, cfg.agent_order, cfg.health_ticket), (10, ("Codex", "Claude"), "JAIF-12"))

    def test_without_the_section_env_applies(self):
        cfg = self.load("", AIFLOW_POLL_SECONDS="99", AIFLOW_AGENT_ORDER="Grok,Codex")
        self.assertEqual((cfg.poll_seconds, cfg.agent_order), (99, ("Grok", "Codex")))

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.load("[runtime]\npol_seconds = 10")

    def test_wrong_types_are_rejected(self):
        for body in ('poll_seconds = "10"', 'poll_seconds = true', 'agent_order = "Codex"', 'agent_order = [1]', 'health_ticket = 5'):
            with self.subTest(body=body), self.assertRaises(ConfigError):
                self.load(f"[runtime]\n{body}")
