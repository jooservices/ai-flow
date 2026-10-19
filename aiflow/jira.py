"""Thin wrapper over the Workspaces Jira adapter (`scripts/jira`). No second auth flow."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class JiraError(RuntimeError):
    pass


class Jira:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cli = str(root / "scripts" / "jira")
        self._api: Any = None

    def _run(self, *args: str) -> str:
        result = subprocess.run([self.cli, *args], capture_output=True, text=True, cwd=self.root, timeout=120)
        if result.returncode != 0:
            raise JiraError((result.stdout + result.stderr).strip()[:400])
        return result.stdout

    def _library(self) -> Any:
        if self._api is None:
            path = str(self.root / "JOOservices" / "scripts" / "jira")
            loader = importlib.machinery.SourceFileLoader("aiflow_jira_adapter", path)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            self._api = module.Atlassian()
        return self._api

    def search(self, jql: str, limit: int = 50) -> list[dict[str, Any]]:
        return json.loads(self._run("search", "--jql", jql, "--limit", str(limit)))["issues"]

    def view(self, key: str) -> dict[str, Any]:
        return json.loads(self._run("view", key))

    def comments(self, key: str, last: int = 6) -> list[dict[str, Any]]:
        data = json.loads(self._run("comments", "--last", str(last), key))
        return data.get("comments", data) if isinstance(data, dict) else data

    def blockers(self, key: str) -> list[dict[str, str]]:
        """Issues that block `key` (link type Blocks, `key` on the "is blocked by" side)."""
        status, body = self._library().call("GET", f"/rest/api/3/issue/{key}?fields=issuelinks")
        if status != 200:
            raise JiraError(f"issuelinks HTTP {status}")
        found = []
        for link in body["fields"].get("issuelinks", []):
            if link["type"]["name"] == "Blocks" and "outwardIssue" in link:
                other = link["outwardIssue"]
                found.append({"key": other["key"], "status": other["fields"]["status"]["name"]})
        return found

    def transition(self, key: str, status: str) -> bool:
        try:
            self._run("transition", key, status)
            return True
        except JiraError:
            return False

    def comment(self, key: str, body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=self.root / ".tmp", delete=False) as handle:
            handle.write(body)
            name = handle.name
        try:
            self._run("comment", "--body-file", name, key)
        finally:
            Path(name).unlink(missing_ok=True)

    def set_agent(self, key: str, agent: str) -> None:
        self._run("edit", key, "--agent", agent)

    def labels(self, key: str) -> list[str]:
        return list(self.view(key).get("labels", []))

    def add_label(self, key: str, label: str) -> None:
        current = self.labels(key)
        if label not in current:
            self._run("edit", key, "--labels", ",".join(current + [label]))

    def remove_label(self, key: str, label: str) -> None:
        current = self.labels(key)
        if label in current:
            self._run("edit", key, "--labels", ",".join(x for x in current if x != label))

    def set_field(self, key: str, field: str, value: Any) -> None:
        """Set a custom field (text value, {"value": "Yes"} for a select) or clear it with None."""
        status, body = self._library().call("PUT", f"/rest/api/3/issue/{key}", {"fields": {field: value}})
        if status not in (200, 204):
            raise JiraError(f"set {field} HTTP {status}: {str(body)[:200]}")

    def set_workspace(self, key: str, field: str, value: str) -> None:
        self.set_field(key, field, value)

    def read_field(self, key: str, field: str) -> Any:
        status, body = self._library().call("GET", f"/rest/api/3/issue/{key}?fields={field}")
        if status != 200:
            raise JiraError(f"read {field} HTTP {status}")
        return body["fields"].get(field)

    def issue_type(self, key: str) -> str:
        return str(self.view(key).get("type", ""))
