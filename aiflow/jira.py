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


def adf_inline(node: dict[str, Any]) -> str:
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "status", "inlineCard"):
        attrs = node.get("attrs", {})
        return str(attrs.get("text") or attrs.get("url") or attrs.get("shortName") or "")
    return "".join(adf_inline(c) for c in node.get("content", []))


def adf_to_markdown(node: dict[str, Any], depth: int = 0) -> str:
    """Atlassian Document Format -> markdown, keeping headings, lists, task lists, code and tables readable."""
    kind = node.get("type")
    children = node.get("content", [])
    if kind == "doc":
        return "\n\n".join(part for part in (adf_to_markdown(c) for c in children) if part).strip()
    if kind == "heading":
        return "#" * int(node.get("attrs", {}).get("level", 2)) + " " + adf_inline(node).strip()
    if kind == "paragraph":
        return adf_inline(node).strip()
    if kind == "codeBlock":
        return "```\n" + adf_inline(node) + "\n```"
    if kind == "rule":
        return "---"
    if kind in ("bulletList", "orderedList", "taskList"):
        lines = []
        for number, item in enumerate(children, 1):
            mark = f"{number}." if kind == "orderedList" else "-"
            if kind == "taskList":
                mark += " [x]" if item.get("attrs", {}).get("state") == "DONE" else " [ ]"
            parts = [adf_to_markdown(c, depth + 1) for c in item.get("content", [])]
            if kind == "taskList" and item.get("type") == "taskItem":
                parts = [adf_inline(item).strip()]
            first, *rest = "\n".join(p for p in parts if p).split("\n") or [""]
            lines.append("  " * depth + f"{mark} {first}")
            lines += [line if line.startswith(" ") else "  " * (depth + 1) + line for line in rest]  # nested lists carry their own indent
        return "\n".join(lines)
    if kind == "blockquote":
        return "\n".join("> " + line for line in "\n\n".join(adf_to_markdown(c) for c in children).splitlines())
    if kind == "table":
        return "\n".join("| " + " | ".join(adf_inline(cell).strip() for cell in row.get("content", [])) + " |" for row in children)
    return "\n\n".join(part for part in (adf_to_markdown(c, depth) for c in children) if part) or adf_inline(node)


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

    def view(self, key: str, full: bool = False) -> dict[str, Any]:
        """The issue as scripts/jira prints it; `full` replaces its flattened description by markdown with the headings kept."""
        data = json.loads(self._run("view", key))
        if full:
            status, body = self._library().call("GET", f"/rest/api/3/issue/{key}?fields=description")
            document = body["fields"].get("description") if status == 200 else None
            if isinstance(document, dict):
                data["description"] = adf_to_markdown(document)
        return data

    def comments(self, key: str, last: int = 6) -> list[dict[str, Any]]:
        """The last comments, oldest first, as {author, created, body}; bodies are markdown with headings and lists kept
        (`scripts/jira comments` flattens them, which hides the template sections)."""
        status, body = self._library().call("GET", f"/rest/api/3/issue/{key}/comment?orderBy=-created&maxResults={last}")
        if status != 200:
            raise JiraError(f"comments HTTP {status}")
        found = []
        for item in reversed(body.get("comments", [])):
            document = item.get("body")
            text = adf_to_markdown(document) if isinstance(document, dict) else str(document or "")
            found.append({"author": (item.get("author") or {}).get("displayName", "?"), "created": item.get("created", ""), "body": text})
        return found

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

    def create_issue(self, project: str, summary: str, description: str, issue_type: str = "Task", labels: str = "") -> str:
        """Create an issue (in Backlog) and return its key."""
        with tempfile.NamedTemporaryFile("w", suffix=".md", dir=self.root / ".tmp", delete=False) as handle:
            handle.write(description)
            name = handle.name
        try:
            out = self._run("create", "--project", project, "--type", issue_type, "--summary", summary, "--description-file", name,
                            *(["--labels", labels] if labels else []), "--format", "markdown")
        finally:
            Path(name).unlink(missing_ok=True)
        return str(json.loads(out)["key"])

    def link(self, source: str, target: str, link_type: str = "Relates") -> None:
        self._run("link", "--type", link_type, source, target)

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
