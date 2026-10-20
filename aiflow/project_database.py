"""Provision, verify, and remove a project's isolated ticket databases."""
from __future__ import annotations

import re
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

TICKET_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9]*)-([0-9]+)$")
DATABASE_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
SECRET_PAIR = re.compile(r"(?i)(pass(?:word|wd)?|secret|token|key|auth\w*)(\s*[=:]\s*)\S+")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
URL_LOGIN = re.compile(r"://[^/\s:@]+:[^@\s]+@")


def redact(text: str) -> str:
    """The last line of a helper error without anything that looks like a credential, short enough for a ticket field."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    line = URL_LOGIN.sub("://<hidden>@", lines[-1])
    line = SECRET_PAIR.sub(lambda m: m.group(1) + m.group(2) + "<hidden>", line)
    return LONG_TOKEN.sub("<hidden>", line)[:160]


DATABASE_ID = re.compile(r"^[a-z0-9]{1,12}$")


def ticket_database_id(ticket_key: str) -> str:
    """Convert a Jira key to a valid shared-database worktree id."""
    match = TICKET_KEY.fullmatch(ticket_key)
    if not match:
        raise ValueError("ticket key is not a Jira key")
    value = (match.group(1) + match.group(2)).lower()
    if not DATABASE_ID.fullmatch(value):
        raise ValueError("ticket key does not map to a 1-12 character database id")
    return value


class ProjectDatabase:
    """Call the Workspaces shared database helper without exposing its output."""

    def __init__(self, workspaces_root: Path, command_runner: Callable[..., Any] | None = None) -> None:
        self.root = workspaces_root
        self.command_runner = command_runner or subprocess.run
        self.shared_script = str(workspaces_root / "scripts" / "docker" / "shared")

    def env_is_ignored(self, worktree: Path) -> bool:
        """Check that Git will exclude the credential-bearing worktree .env file."""
        return self._succeeded(["git", "-C", str(worktree), "check-ignore", "--quiet", ".env"])

    def prepare(self, name: str, ticket_key: str, worktree: Path) -> str | None:
        """Provision the ticket resources, write its worktree .env, and explicitly verify it.

        The caller must first confirm `env_is_ignored`. Returns a safe step description on failure. Command output
        is discarded because the env helper writes shared service credentials into the generated file.
        """
        if not DATABASE_NAME.fullmatch(name):
            return "invalid project database name"
        try:
            database_id = ticket_database_id(ticket_key)
        except ValueError:
            return "invalid ticket database id"

        commands = (
            [self.shared_script, "provision-project", name, "--postgres", "--mongodb", "--redis", "--db", database_id],
            [self.shared_script, "env-project", name, "--mode", f"wt:{database_id}", "--output", str(worktree / ".env")],
            [self.shared_script, "check-project", name, "--env-file", str(worktree / ".env"), "--mode", f"wt:{database_id}"],
        )
        steps = ("provision-project", "env-project", "check-project")
        for command, step in zip(commands, steps):
            ok, detail = self._run(command)
            if not ok:
                return f"{step} failed" + (f": {detail}" if detail else "")
        return None

    def teardown(self, name: str, ticket_key: str) -> bool:
        """Remove only this ticket's databases and Redis prefix."""
        if not DATABASE_NAME.fullmatch(name):
            return False
        try:
            database_id = ticket_database_id(ticket_key)
        except ValueError:
            return False
        command = [self.shared_script, "teardown-project", name, "--confirm", name, "--db", database_id]
        return self._succeeded(command)

    def _run(self, command: list[str]) -> tuple[bool, str]:
        """Run a helper command: stdout is discarded (it may carry credentials), stderr is kept only as a redacted last line."""
        try:
            result = self.command_runner(command, cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return False, error.__class__.__name__
        return result.returncode == 0, "" if result.returncode == 0 else redact(getattr(result, "stderr", "") or "")

    def _succeeded(self, command: list[str]) -> bool:
        return self._run(command)[0]
