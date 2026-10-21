"""Jira gateway: the only component that talks to Jira (REST v3, basic auth with an API token).

Reads keep the registry's ticket mirror fresh; writes (transition, comment, fields) are issued only after the
control API has checked the caller's fencing token. 429 answers are retried after Retry-After.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Iterator

import httpx

from ..core.text import adf_to_text, text_to_adf

ORDER = ["Backlog", "Intake", "To Do", "In Progress", "In Verify", "Human review", "Done"]
FIELDS = ["summary", "status", "issuetype", "priority", "labels", "description", "parent", "issuelinks",
          "created", "updated", "resolutiondate"]


class JiraError(RuntimeError):
    pass


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")


class Jira:
    def __init__(self, site: str, email: str, token: str, timeout: float = 30.0, client: httpx.Client | None = None):
        self.site = site.rstrip("/")
        self.c = client or httpx.Client(base_url=self.site, auth=(email, token), timeout=timeout,
                                        headers={"Accept": "application/json"})
        self.field_ids: dict[str, str] = {}
        self.last_limits: dict[str, str] = {}

    # --- plumbing -------------------------------------------------------------------------------------------
    def call(self, method: str, path: str, json: Any = None, ok: tuple[int, ...] = (200, 201, 204),
             files: Any = None, headers: dict[str, str] | None = None) -> Any:
        for attempt in range(6):
            r = self.c.request(method, path, json=json, files=files, headers=headers)
            self.last_limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, int(r.headers.get("Retry-After", "0") or 0) or 2 ** attempt))
                continue
            if r.status_code not in ok:
                raise JiraError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise JiraError(f"{method} {path}: Jira kept failing")

    def custom_field(self, name: str) -> str | None:
        if not self.field_ids:
            self.field_ids = {f["name"]: f["id"] for f in self.call("GET", "/rest/api/3/field") if f.get("custom")}
        return self.field_ids.get(name)

    # --- reads ----------------------------------------------------------------------------------------------
    def search(self, jql: str, fields: list[str] | None = None, page: int = 100) -> Iterator[dict]:
        token = None
        while True:
            body: dict[str, Any] = {"jql": jql, "maxResults": page, "fields": fields or FIELDS}
            if token:
                body["nextPageToken"] = token
            data = self.call("POST", "/rest/api/3/search/jql", body)
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return

    @staticmethod
    def normalize(issue: dict) -> dict:
        """Jira issue -> registry row (tickets table)."""
        f = issue.get("fields", {})
        blocked_by = []
        for link in f.get("issuelinks") or []:
            t = (link.get("type") or {}).get("inward", "")
            if "blocked by" in t and link.get("inwardIssue"):
                blocked_by.append(link["inwardIssue"]["key"])
        return {
            "key": issue["key"], "project": issue["key"].split("-", 1)[0],
            "status": (f.get("status") or {}).get("name", ""),
            "type": (f.get("issuetype") or {}).get("name", "Task"),
            "priority": (f.get("priority") or {}).get("name", "Medium"),
            "labels": list(f.get("labels") or []), "summary": f.get("summary") or "",
            "description": adf_to_text(f.get("description")) if isinstance(f.get("description"), dict) else (f.get("description") or ""),
            "parent_key": (f.get("parent") or {}).get("key"), "blocked_by": blocked_by,
            "jira_created": _ts(f.get("created")), "jira_updated": _ts(f.get("updated")),
            "done_at": _ts(f.get("resolutiondate")),
        }

    def status(self, key: str) -> str:
        return self.call("GET", f"/rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]

    def recent_comments(self, key: str, limit: int = 10) -> list[dict[str, str]]:
        """Return the newest Jira comments in a compact, prompt-safe form."""
        data = self.call("GET", f"/rest/api/3/issue/{key}/comment?orderBy=-created&maxResults={limit}")
        return [{"author": (c.get("author") or {}).get("displayName", "unknown"),
                 "created": c.get("created", ""),
                 "body": adf_to_text(c.get("body"))[:2000]}
                for c in data.get("comments", [])[:limit]]

    # --- writes ---------------------------------------------------------------------------------------------
    def create_issue(self, project: str, summary: str, description: str, issuetype: str = "Story",
                     labels: list[str] | None = None, parent: str = "") -> str:
        """Create an issue and return its key; `parent` links it to an epic (team-managed and company-managed)."""
        fields: dict[str, Any] = {"project": {"key": project}, "summary": summary, "issuetype": {"name": issuetype},
                                  "description": text_to_adf(description), "labels": list(labels or [])}
        if parent:
            fields["parent"] = {"key": parent}
        return self.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def add_attachment(self, key: str, filename: str, content: bytes, content_type: str) -> None:
        """Attach a stakeholder screenshot to an issue."""
        self.call("POST", f"/rest/api/3/issue/{key}/attachments", ok=(200,),
                  files={"file": (filename, content, content_type)}, headers={"X-Atlassian-Token": "no-check"})

    def transition_to(self, key: str, target: str) -> str:
        """Walk the workflow until `key` reaches `target`; direct transitions first, then the forward order."""
        for _ in range(10):
            cur = self.status(key)
            if cur == target:
                return cur
            trs = self.call("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
            pick = next((t for t in trs if t["to"]["name"] == target), None)
            if not pick and cur in ORDER and target in ORDER and ORDER.index(cur) < ORDER.index(target):
                nxt = ORDER[ORDER.index(cur) + 1]
                pick = next((t for t in trs if t["to"]["name"] == nxt), None)
            if not pick:
                raise JiraError(f"{key}: no transition from {cur} towards {target}")
            self.call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": pick["id"]}})
        return self.status(key)

    def comment(self, key: str, text: str) -> str:
        return self.call("POST", f"/rest/api/3/issue/{key}/comment", {"body": text_to_adf(text)})["id"]

    def add_worklog(self, key: str, seconds: int, started, comment: str) -> None:
        started_txt = started.strftime("%Y-%m-%dT%H:%M:%S.000%z")
        self.call("POST", f"/rest/api/3/issue/{key}/worklog?notifyUsers=false&adjustEstimate=leave",
                  {"timeSpentSeconds": int(seconds), "started": started_txt, "comment": text_to_adf(comment)})

    def set_fields(self, key: str, values: dict[str, Any]) -> None:
        """values by field NAME (custom fields resolved) or by id."""
        fields: dict[str, Any] = {}
        for name, v in values.items():
            fid = name if name.startswith("customfield_") or name in ("labels", "summary") else self.custom_field(name)
            if fid:
                fields[fid] = v
        if fields:
            self.call("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})

    def set_description(self, key: str, text: str) -> None:
        self.call("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": text_to_adf(text)}})

    def add_label(self, key: str, label: str) -> None:
        self.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": label}]}})

    def remove_label(self, key: str, label: str) -> None:
        self.call("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"remove": label}]}})
