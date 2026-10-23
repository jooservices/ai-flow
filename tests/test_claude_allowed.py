import unittest
from pathlib import Path

from aiflow import agents


class ClaudeAllowedTests(unittest.TestCase):
    def test_project_check_commands_are_allowed(self):
        rules = agents.claude_allowed(Path("/w")).split(",")
        for name in ("make", "composer", "npm", "npx", "node", "php", "bash ./docker/scripts/"):
            self.assertIn(f"Bash({name}:*)", rules)

    def test_safe_tools_and_jira_adapter_stay_allowed(self):
        rules = agents.claude_allowed(Path("/w")).split(",")
        self.assertIn("Bash(git:*)", rules)
        self.assertIn("Bash(/w/scripts/jira:*)", rules)

    def test_dangerous_commands_stay_denied_and_never_allowed(self):
        rules = set(agents.claude_allowed(Path("/w")).split(","))
        for denied in ("Bash(git push --force:*)", "Bash(git reset --hard:*)", "Bash(docker run:*)", "Bash(docker volume rm:*)"):
            self.assertIn(denied, agents.DENIED_BASH)
            self.assertNotIn(denied, rules)


if __name__ == "__main__":
    unittest.main()
