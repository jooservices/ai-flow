"""Connect the shared memory service to ticket prompts and completion events."""
from __future__ import annotations

import json
from typing import Any, Callable

from .. import events
from ..jira import Jira, JiraError
from ..lock import Redis
from . import cases
from .client import Memory, spool_path

DAY = 86400


class MemoryWorkflow:
    """Memory use cases shared by the legacy supervisor, kept out of its giant module."""

    def __init__(self, cfg: Any, redis: Redis, jira: Jira, state: Callable[..., str],
                 change_type: Callable[..., str], log: Callable[[str], None]) -> None:
        self.cfg, self.redis, self.jira = cfg, redis, jira
        self.state, self.change_type, self.log = state, change_type, log
        self.default_url = cfg.memory.get("AIFLOW_MEMORY_URL", "")
        self.default = Memory(self.default_url, cfg.memory.get("AIFLOW_MEMORY_TOKEN", ""),
                              spool=cfg.root / ".tmp" / "aiflow" / "memory-spool.jsonl", log=log)
        self.clients: dict[str, Memory] = {}

    def client_for(self, key: str) -> Memory:
        """Return the project's client, inheriting the configured default service URL."""
        project = self.cfg.project(key)
        project_url = getattr(project, "memory_url", None)
        url = project_url if project_url is not None else self.cfg.memory.get("AIFLOW_MEMORY_URL", "")
        if url.rstrip("/") == self.default_url.rstrip("/"):
            return self.default
        if url not in self.clients:
            self.clients[url] = Memory(url, self.cfg.memory.get("AIFLOW_MEMORY_TOKEN", ""),
                                       spool=spool_path(self.cfg.root, url, self.default_url), log=self.log)
        return self.clients[url]

    def recall(self, key: str, view: dict[str, Any], repo: Any) -> str:
        """Return relevant eligible entries as a prompt block; an unavailable service gives no advice."""
        memory = self.client_for(key)
        if not memory.enabled:
            return "(the shared memory is not configured)"
        project = self.cfg.project(key)
        description = str(view.get("description", ""))
        parsed = cases.sections(description)
        wanted = " ".join([str(view.get("summary", "")),
                           (parsed.get("ac") or parsed.get("acceptance criteria") or "")[:600],
                           parsed.get("scopes", "")[:300]])
        items = memory.search(wanted, project.company, project.jira_key, repo.name, limit=6)
        events.emit("memory.recall", ticket=key, found=len(items), ids=",".join(str(i["id"])[:8] for i in items),
                    case_ids=",".join(str(i["id"])[:8] for i in items if i.get("kind") == "case"))
        if items:
            self.redis.set(f"recalled:{key}", json.dumps([i["id"] for i in items]), 30 * DAY)
        return cases.prompt_block(items)

    def remember_lessons(self, key: str, repo: Any, agent: str, text: str) -> None:
        """Propose valid MEMORY lines from an AI's final response."""
        memory = self.client_for(key)
        if not memory.enabled:
            return
        project = self.cfg.project(key)
        for kind, title, body in cases.lessons(text):
            entry_id = memory.propose(dict(kind=kind, scope="project", namespace=project.company, project=project.jira_key,
                                           repo=repo.name, jira_key=key, title=title, body=body,
                                           tags=[project.jira_key.lower(), repo.name.lower(), agent.lower()],
                                           source={"jira": key}, created_by=agent))
            events.emit("memory.propose", ticket=key, kind=kind, title=title[:120], agent=agent, id=entry_id or "")

    def remember_case(self, key: str, repo: Any, pr: dict[str, Any]) -> None:
        """Propose one case when a ticket reaches Done, using its available ticket context."""
        memory = self.client_for(key)
        marker = f"memory:case:{key}"
        if not memory.enabled or self.redis.get(marker):
            return
        project = self.cfg.project(key)
        try:
            comments = self.jira.comments(key, 30)
            summary = str(self.jira.view(key).get("summary", key))
        except (JiraError, ValueError, TypeError) as error:
            self.log(f"{key}: no case written ({error})")
            return
        has_handover = bool(cases.handover(comments))
        rounds = int(self.redis.get(f"cycles:{key}") or 0)
        entry = cases.build_case(key, summary, project.company, repo.name, self.change_type(key), self.state(key, "dev"),
                                 str(pr.get("url", "")), comments, rounds, self.state(key, "rework"))
        entry_id = memory.propose(entry) if entry else None
        self.redis.set(marker, entry_id or "queued", 365 * DAY)
        events.emit("memory.case", ticket=key, written=bool(entry_id), has_handover=has_handover, rework_rounds=rounds)
        if rounds == 0:
            for recalled in json.loads(self.redis.get(f"recalled:{key}") or "[]"):
                memory.feedback(recalled, key, True)

    def env(self, key: str, repo: Any) -> dict[str, str]:
        """Return only the client's scoped service credentials and ticket defaults."""
        token = self.cfg.memory.get("AIFLOW_MEMORY_AI_TOKEN", "")
        project = self.cfg.project(key)
        url = project.memory_url if project.memory_url is not None else self.cfg.memory.get("AIFLOW_MEMORY_URL", "")
        if not (token and url):
            return {}
        return {"AIFLOW_MEMORY_URL": url, "AIFLOW_MEMORY_TOKEN": token, "AIFLOW_MEMORY_NAMESPACE": project.company,
                "AIFLOW_MEMORY_PROJECT": project.jira_key, "AIFLOW_MEMORY_REPO": repo.name, "AIFLOW_MEMORY_TICKET": key,
                "AIFLOW_MEMORY_ACTOR": "ai", "AIFLOW_MEMORY_SCOPED_STORAGE": "postgres"}
