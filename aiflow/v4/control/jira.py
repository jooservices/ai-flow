"""Jira gateway: the only component that talks to Jira (REST v3).

Reads keep the registry's ticket mirror fresh; writes (transition, comment, fields) are issued only after the
control API has checked the caller's fencing token. 429 answers are retried after Retry-After.

Credentials: either a fixed (email, API token) pair against the site (basic auth), or an `auth` source
(jira_connect.JiraAuth) asked before every request. The source returns the credential connected on the
Integrations screen (OAuth 2.0 bearer through https://api.atlassian.com/ex/jira/<cloud id>, or basic auth with
an API token) or the owner.env fallback; it refreshes an expiring OAuth token, and a 401 forces one refresh and a
retry. With a source, the gateway is falsy while no credential is usable, so `if ctx.jira:` keeps meaning
"Jira is connected".
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ..core.text import adf_to_text, text_to_adf

ORDER = ["Backlog", "Intake", "To Do", "In Progress", "In Verify", "Human review", "Done"]
FIELDS = ["summary", "status", "issuetype", "priority", "labels", "description", "parent", "issuelinks", "assignee",
          "created", "updated", "resolutiondate", "fixVersions"]


class JiraError(RuntimeError):
    pass


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")


class Jira:
    def __init__(self, site: str, email: str = "", token: str = "", timeout: float = 30.0,
                 client: httpx.Client | None = None, auth: Any = None):
        self.site = site.rstrip("/")
        self.auth = auth
        if auth is None:
            self.c = client or httpx.Client(base_url=self.site, auth=(email, token), timeout=timeout,
                                            headers={"Accept": "application/json"})
        else:
            self.c = client or httpx.Client(timeout=timeout, headers={"Accept": "application/json"})
        self.field_ids: dict[str, str] = {}
        self.last_limits: dict[str, str] = {}
        self._base = ""

    def __bool__(self) -> bool:
        return self.auth is None or self.auth.available()

    # --- plumbing -------------------------------------------------------------------------------------------
    def call(self, method: str, path: str, json: Any = None, ok: tuple[int, ...] = (200, 201, 204),
             files: Any = None, headers: dict[str, str] | None = None) -> Any:
        refreshed = False
        for attempt in range(6):
            url, send_headers, cred = path, headers, None
            if self.auth is not None:
                cred = self.auth.credential()
                if cred.base_url != self._base:  # another site or credential: custom field ids may differ
                    self._base, self.field_ids = cred.base_url, {}
                url = cred.base_url + path
                send_headers = {**(headers or {}), **cred.headers()}
            r = self.c.request(method, url, json=json, files=files, headers=send_headers)
            self.last_limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")}
            if r.status_code == 401 and cred is not None and cred.kind == "oauth" and not refreshed:
                refreshed = True  # the access token was revoked or expired early: refresh once and retry
                self.auth.credential(force_refresh=True, stale=cred)
                continue
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
    def search(self, jql: str, fields: list[str] | None = None, page: int = 100,
               expand: str = "") -> Iterator[dict]:
        token = None
        while True:
            body: dict[str, Any] = {"jql": jql, "maxResults": page, "fields": fields or FIELDS}
            if expand:
                body["expand"] = expand
            if token:
                body["nextPageToken"] = token
            data = self.call("POST", "/rest/api/3/search/jql", body)
            yield from data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return

    def search_history(self, jql: str) -> Iterator[dict]:
        """`search` with each issue's change history, for the registry sync."""
        return self.search(jql, expand="changelog")

    @staticmethod
    def history(issue: dict) -> dict[str, Any]:
        """From an issue read with expand=changelog: when each Fix version was last attached and when the ticket
        first moved to In Progress. Empty values when the issue carries no changelog."""
        added: dict[str, datetime] = {}
        first_in_progress: datetime | None = None
        floor = datetime.min.replace(tzinfo=timezone.utc)
        histories = sorted(((_ts(h.get("created")), h) for h in (issue.get("changelog") or {}).get("histories") or []),
                           key=lambda pair: pair[0] or floor)
        for at, h in histories:
            for item in h.get("items") or []:
                field = str(item.get("field") or "").lower()
                if field in ("fix version", "fixversions") and item.get("to") and at:
                    added[str(item["to"])] = at
                elif field == "status" and item.get("toString") == "In Progress" and at and not first_in_progress:
                    first_in_progress = at
        return {"fix_version_added": added, "first_in_progress": first_in_progress}

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
            "assignee": ({"name": (f.get("assignee") or {}).get("displayName", ""),
                          "email": (f.get("assignee") or {}).get("emailAddress", ""),
                          "account_id": (f.get("assignee") or {}).get("accountId", "")}
                         if f.get("assignee") else None),
            "jira_created": _ts(f.get("created")), "jira_updated": _ts(f.get("updated")),
            "done_at": _ts(f.get("resolutiondate")),
            "fix_versions": [str(version["id"]) for version in (f.get("fixVersions") or []) if version.get("id")],
            **Jira.history(issue),
        }

    def status(self, key: str) -> str:
        return self.call("GET", f"/rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]

    def projects(self) -> list[dict[str, str]]:
        """List projects visible to the connected Jira account, following Jira's pagination."""
        out: list[dict[str, str]] = []
        start = 0
        while True:
            data = self.call("GET", f"/rest/api/3/project/search?startAt={start}&maxResults=100")
            values = data.get("values", [])
            out.extend({"key": str(item.get("key") or ""), "name": str(item.get("name") or "")}
                       for item in values if item.get("key"))
            if data.get("isLast", True) or not values:
                return out
            start += len(values)

    def project_write_permissions(self, project: str) -> dict[str, bool]:
        """Read the project permissions needed to browse and work with project issues."""
        from urllib.parse import quote

        key = quote(project, safe="")
        data = self.call("GET", f"/rest/api/3/mypermissions?projectKey={key}"
                         "&permissions=BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES,LINK_ISSUES,TRANSITION_ISSUES,ADD_COMMENTS")
        permissions = data.get("permissions", {}) if isinstance(data, dict) else {}
        return {name: (permissions.get(name) or {}).get("havePermission") is True
                for name in ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES", "LINK_ISSUES",
                             "TRANSITION_ISSUES", "ADD_COMMENTS")}

    def project_statuses(self, key: str) -> list[str]:
        """Return distinct workflow status names used by the issue types in a Jira project."""
        data = self.call("GET", f"/rest/api/3/project/{key}/statuses")
        return sorted({str(status.get("name") or "") for issue_type in data
                       for status in issue_type.get("statuses", []) if status.get("name")})

    def project_versions(self, key: str) -> list[dict[str, Any]]:
        """List the Jira versions for a project in a compact registry-ready form."""
        data = self.call("GET", f"/rest/api/3/project/{key}/versions")
        return [{"id": str(version["id"]), "name": str(version.get("name") or ""),
                 "released": bool(version.get("released", False)),
                 "release_date": version.get("releaseDate") or None}
                for version in data if version.get("id")]

    def versions(self, project: str) -> list[dict[str, Any]]:
        """List Jira versions for one project, including their stable ids and Released state."""
        data = self.call("GET", f"/rest/api/3/project/{project}/versions")
        return [{"id": str(v.get("id") or ""), "name": str(v.get("name") or ""),
                 "released": bool(v.get("released")), "release_date": v.get("releaseDate") or None,
                 "archived": bool(v.get("archived"))}
                for v in data if v.get("id") and v.get("name")]

    def version_issues(self, project: str, version_id: str) -> list[dict]:
        """Read current project membership for a Jira version; callers must not use a cached ticket snapshot."""
        if not str(version_id).isdigit():
            raise JiraError("Jira version id must be numeric")
        return list(self.search(f'project = "{project}" AND fixVersion = {version_id} ORDER BY key'))

    def release_version(self, version_id: str, released: bool = True) -> None:
        """Set a Jira version's Released flag."""
        from datetime import date

        values: dict[str, Any] = {"released": bool(released)}
        if released:
            values["releaseDate"] = date.today().isoformat()
        self.call("PUT", f"/rest/api/3/version/{version_id}", values)

    def recent_comments(self, key: str, limit: int = 10) -> list[dict[str, str]]:
        """Return the newest Jira comments in a compact, prompt-safe form."""
        data = self.call("GET", f"/rest/api/3/issue/{key}/comment?orderBy=-created&maxResults={limit}")
        return [{"author": (c.get("author") or {}).get("displayName", "unknown"),
                 "account_id": (c.get("author") or {}).get("accountId", ""),
                 "created": c.get("created", ""),
                 "body": adf_to_text(c.get("body"))[:2000]}
                for c in data.get("comments", [])[:limit]]

    def current_account_id(self) -> str:
        """Return the stable Jira identity of the account connected by the owner.

        Ticket comments are eligible to answer owner-only questions only when their accountId matches this
        authenticated Jira identity; display names and email addresses are not authorization identifiers.
        """
        profile = self.call("GET", "/rest/api/3/myself")
        return str((profile or {}).get("accountId") or "")

    # --- writes ---------------------------------------------------------------------------------------------
    def create_issue(self, project: str, summary: str, description: str, issuetype: str = "Story",
                     labels: list[str] | None = None, parent: str = "", fix_version: str = "",
                     version_field: str = "") -> str:
        """Create an issue and return its key; `parent` links it to an epic (team-managed and company-managed)."""
        fields: dict[str, Any] = {"project": {"key": project}, "summary": summary, "issuetype": {"name": issuetype},
                                  "description": text_to_adf(description), "labels": list(labels or [])}
        if parent:
            fields["parent"] = {"key": parent}
        if fix_version:
            fields["fixVersions"] = [{"id": str(fix_version)}]
        if version_field:
            field_id = self.custom_field("Version")
            if field_id:
                fields[field_id] = str(version_field)
        return self.call("POST", "/rest/api/3/issue", {"fields": fields})["key"]

    def link_issues(self, blocker: str, blocked: str, link_type: str = "Blocks") -> None:
        """Create a Jira issue link where `blocker` blocks `blocked`."""
        self.call("POST", "/rest/api/3/issueLink", {
            "type": {"name": link_type}, "outwardIssue": {"key": blocker}, "inwardIssue": {"key": blocked},
        })

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
