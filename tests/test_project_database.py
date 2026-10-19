from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest

from aiflow.project_database import ProjectDatabase, ticket_database_id


class FakeCommandRunner:
    def __init__(self, returncodes=()):
        self.returncodes = list(returncodes)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        code = self.returncodes.pop(0) if self.returncodes else 0
        return SimpleNamespace(returncode=code)


class TicketDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worktree = self.root / "ticket-worktree"
        self.commands = FakeCommandRunner()
        self.database = ProjectDatabase(self.root, self.commands)

    def tearDown(self):
        self.tmp.cleanup()

    def test_id_mapping(self):
        self.assertEqual(ticket_database_id("JAVM-7"), "javm7")
        self.assertEqual(ticket_database_id("JAIF-123"), "jaif123")
        self.assertEqual(ticket_database_id("jaif-123"), "jaif123")
        for key in ("JAIF-", "A-123456789012"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ticket_database_id(key)

    def test_prepare_provisions_writes_and_verifies(self):
        self.assertIsNone(self.database.prepare("my-project", "JAIF-123", self.worktree))
        self.assertEqual(
            [call[0][1:] for call in self.commands.calls],
            [
                ["provision-project", "my-project", "--postgres", "--mongodb", "--redis", "--db", "jaif123"],
                ["env-project", "my-project", "--mode", "wt:jaif123", "--output", str(self.worktree / ".env")],
                ["check-project", "my-project", "--env-file", str(self.worktree / ".env"), "--mode", "wt:jaif123"],
            ],
        )
        self.assertTrue(all(call[1]["stdout"] == subprocess.DEVNULL for call in self.commands.calls))
        self.assertTrue(all(call[1]["stderr"] == subprocess.DEVNULL for call in self.commands.calls))

    def test_provisioning_failure_stops_the_sequence(self):
        self.commands.returncodes = [1]
        self.assertEqual(self.database.prepare("my-project", "JAIF-7", self.worktree), "provision-project failed")
        self.assertEqual(len(self.commands.calls), 1)

    def test_env_generation_failure_stops_before_verification(self):
        self.commands.returncodes = [0, 1]
        self.assertEqual(self.database.prepare("my-project", "JAIF-7", self.worktree), "env-project failed")
        self.assertEqual(len(self.commands.calls), 2)

    def test_check_failure_is_returned_without_command_output(self):
        self.commands.returncodes = [0, 0, 1]
        self.assertEqual(self.database.prepare("my-project", "JAIF-7", self.worktree), "check-project failed")
        self.assertEqual(len(self.commands.calls), 3)

    def test_env_ignore_check_uses_git_without_exposing_output(self):
        self.assertTrue(self.database.env_is_ignored(self.worktree))
        self.assertEqual(self.commands.calls[0][0][1:], ["-C", str(self.worktree), "check-ignore", "--quiet", ".env"])

    def test_env_must_be_git_ignored_before_credentials_are_written(self):
        self.commands.returncodes = [1]
        self.assertFalse(self.database.env_is_ignored(self.worktree))
        self.assertEqual(len(self.commands.calls), 1)

    def test_teardown_is_scoped_to_the_ticket(self):
        self.assertTrue(self.database.teardown("my-project", "JAIF-7"))
        self.assertEqual(
            self.commands.calls[0][0][1:],
            ["teardown-project", "my-project", "--confirm", "my-project", "--db", "jaif7"],
        )

    def test_command_arguments_do_not_include_credentials(self):
        credential = "test-only-value-not-used-by-the-command"
        self.database.prepare("my-project", "JAIF-7", self.worktree)
        command_line = " ".join(part for command, _ in self.commands.calls for part in command)
        self.assertNotIn(credential, command_line)
        self.assertNotIn("DB_PASSWORD", command_line)


if __name__ == "__main__":
    unittest.main()
