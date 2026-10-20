"""Prepare a Jira project for AI Flow: fields, issue type Release, workflow and workflow scheme (`bin/aiflow-setup`).

The definition lives in `jira_spec.json` and uses names only (no ids), so it fits any Jira site. Every step looks first and
creates only what is missing; without `--apply` nothing is written and the plan is printed. An object that already exists is
never changed. Not covered: the Kanban board (one column per status) and the Jira sprint/board filters.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

from . import events

FIELD_TYPES = {
    "select": ("com.atlassian.jira.plugin.system.customfieldtypes:select", "com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher"),
    "text": ("com.atlassian.jira.plugin.system.customfieldtypes:textfield", "com.atlassian.jira.plugin.system.customfieldtypes:textsearcher"),
}
SPEC_FILE = Path(__file__).with_name("jira_spec.json")
COLUMNS = 4


class SetupError(RuntimeError):
    pass


def load_spec(path: Path = SPEC_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class Setup:
    def __init__(self, api: Any, spec: dict[str, Any], project_key: str, apply: bool = False, out=print) -> None:
        self.api, self.spec, self.key, self.apply, self.out = api, spec, project_key, apply, out
        self.project_id = ""
        self.changes = 0

    # --- plumbing -------------------------------------------------------------------------------------------------------
    def call(self, method: str, path: str, body: dict[str, Any] | None = None, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        status, data = self.api.call(method, path, body)
        if status not in ok:
            raise SetupError(f"{method} {path} -> HTTP {status}: {json.dumps(data)[:300]}")
        return data

    def pages(self, path: str) -> list[Any]:
        """All `values` of a paginated listing."""
        values: list[Any] = []
        start = 0
        while True:
            data = self.call("GET", f"{path}{'&' if '?' in path else '?'}startAt={start}&maxResults=100")
            values += data.get("values", [])
            if data.get("isLast", True) or not data.get("values"):
                return values
            start += len(data["values"])

    def step(self, text: str) -> bool:
        """Report an action; True when it must really run."""
        self.changes += 1
        events.emit("setup.step", project=self.key, text=text, applied=self.apply)
        self.out(("DO    " if self.apply else "PLAN  ") + text)
        return self.apply

    def have(self, text: str) -> None:
        self.out("OK    " + text)

    # --- steps ------------------------------------------------------------------------------------------------------------
    def run(self) -> int:
        project = self.call("GET", f"/rest/api/3/project/{self.key}")
        self.project_id = str(project["id"])
        self.out(f"Project {self.key} (id {self.project_id}), {'APPLY' if self.apply else 'dry run, nothing is written'}")
        field_ids = self.fields()
        self.screens(field_ids)
        self.issue_types()
        self.workflow()
        self.scheme()
        self.out(f"{self.changes} change(s) {'made' if self.apply else 'planned'}.")
        return self.changes

    def fields(self) -> dict[str, str]:
        existing: dict[str, list[str]] = {}
        for item in self.call("GET", "/rest/api/3/field"):
            if item.get("custom"):
                existing.setdefault(item["name"], []).append(item["id"])
        ids: dict[str, str] = {}
        for field in self.spec["fields"]:
            name, found = field["name"], existing.get(field["name"], [])
            if len(found) > 1:
                raise SetupError(f"{len(found)} custom fields are named `{name}`: rename or delete the extra ones.")
            field_id = found[0] if found else ""
            if field_id:
                self.have(f"field {name} ({field_id})")
            else:
                kind, searcher = FIELD_TYPES[field["type"]]
                if self.step(f"create field {name} ({field['type']})"):
                    field_id = self.call("POST", "/rest/api/3/field", {"name": name, "description": field.get("description", ""), "type": kind, "searcherKey": searcher})["id"]
            ids[name] = field_id
            if field["type"] == "select" and field.get("options"):
                self.options(name, field_id, field["options"])
        return ids

    def options(self, name: str, field_id: str, wanted: list[str]) -> None:
        current: set[str] = set()
        context = ""
        if field_id:
            contexts = self.pages(f"/rest/api/3/field/{field_id}/context")
            if contexts:
                context = str(contexts[0]["id"])
                current = {o["value"] for o in self.pages(f"/rest/api/3/field/{field_id}/context/{context}/option")}
        missing = [value for value in wanted if value not in current]
        if not missing:
            return
        if self.step(f"add {len(missing)} option(s) to {name}: {', '.join(missing[:4])}{' ...' if len(missing) > 4 else ''}") and field_id:
            if not context:
                raise SetupError(f"Field `{name}` has no context; create one in Jira, then run again.")
            self.call("POST", f"/rest/api/3/field/{field_id}/context/{context}/option", {"options": [{"value": v} for v in missing]})

    def screens(self, field_ids: dict[str, str]) -> None:
        """Put each field on the project's screens (the first tab) when it is not there yet."""
        screens: set[str] = set()
        for link in self.pages(f"/rest/api/3/issuetypescreenscheme/project?projectId={self.project_id}"):
            scheme = str(link["issueTypeScreenScheme"]["id"])
            for mapping in self.pages(f"/rest/api/3/issuetypescreenscheme/mapping?issueTypeScreenSchemeId={scheme}"):
                for screen_scheme in self.pages(f"/rest/api/3/screenscheme?id={mapping['screenSchemeId']}"):
                    screens.update(str(v) for v in screen_scheme.get("screens", {}).values())
        for screen in sorted(screens):
            available = {f["id"] for f in self.call("GET", f"/rest/api/3/screens/{screen}/availableFields")}
            for name, field_id in field_ids.items():
                if field_id and field_id not in available:
                    continue
                if self.step(f"add field {name} to screen {screen}") and field_id:
                    tabs = self.call("GET", f"/rest/api/3/screens/{screen}/tabs")
                    self.call("POST", f"/rest/api/3/screens/{screen}/tabs/{tabs[0]['id']}/fields", {"fieldId": field_id})

    def issue_types(self) -> None:
        existing = {t["name"]: t for t in self.call("GET", "/rest/api/3/issuetype")}
        schemes = self.pages(f"/rest/api/3/issuetypescheme/project?projectId={self.project_id}")
        scheme_id = str(schemes[0]["issueTypeScheme"]["id"]) if schemes else ""
        in_scheme = {str(m["issueTypeId"]) for m in self.pages(f"/rest/api/3/issuetypescheme/mapping?issueTypeSchemeId={scheme_id}")} if scheme_id else set()
        for wanted in self.spec["issue_types"]:
            name, found = wanted["name"], existing.get(wanted["name"])
            type_id = str(found["id"]) if found else ""
            if found:
                self.have(f"issue type {name} ({type_id})")
            elif self.step(f"create issue type {name}"):
                type_id = str(self.call("POST", "/rest/api/3/issuetype", {"name": name, "description": wanted.get("description", ""), "type": "standard"})["id"])
            if type_id and type_id in in_scheme:
                continue
            if scheme_id and self.step(f"add issue type {name} to the issue type scheme {scheme_id} of {self.key}") and type_id:
                self.call("PUT", f"/rest/api/3/issuetypescheme/{scheme_id}/issuetype", {"issueTypeIds": [type_id]})

    def workflow_payload(self) -> dict[str, Any]:
        """The create request of the workflow; statuses that exist by name are referenced, the others are created."""
        spec = self.spec["workflow"]
        data = self.call("GET", "/rest/api/3/statuses/search?maxResults=200")
        known = {s["name"]: s for s in data.get("values", [])}
        resolutions = {r["name"]: str(r["id"]) for r in self.call("GET", "/rest/api/3/resolution")}
        refs: dict[str, str] = {}
        statuses = []
        for item in spec["statuses"]:
            existing = known.get(item["name"])
            refs[item["name"]] = str(uuid.uuid4())
            entry = {"statusReference": refs[item["name"]], "name": item["name"], "statusCategory": item["category"]}
            if existing:
                entry["id"] = str(existing["id"])
            statuses.append(entry)
        transitions: list[dict[str, Any]] = [{"id": "1", "type": "INITIAL", "toStatusReference": refs[spec["statuses"][0]["name"]], "name": "Create", "links": []}]
        for number, t in enumerate(spec["transitions"], 11):
            actions: list[dict[str, Any]] = []
            if t.get("assign_current_user"):
                actions.append({"ruleKey": "system:change-assignee", "parameters": {"type": "to-current-user"}})
            if "resolution" in t:
                value = resolutions.get(t["resolution"], "") if t["resolution"] else ""
                if t["resolution"] and not value:
                    raise SetupError(f"Jira has no resolution named `{t['resolution']}`.")
                actions.append({"ruleKey": "system:update-field", "parameters": {"field": "resolution", "value": value, "mode": ""}})
            transition = {"id": str(number), "type": "DIRECTED", "toStatusReference": refs[t["to"]], "name": t["name"],
                          "links": [{"fromStatusReference": refs[name]} for name in t["from"]]}
            if actions:
                transition["actions"] = actions
            transitions.append(transition)
        layout = [{"statusReference": refs[item["name"]], "layout": {"x": 220 * (n % COLUMNS), "y": 120 + 140 * (n // COLUMNS)}} for n, item in enumerate(spec["statuses"])]
        return {"scope": {"type": "GLOBAL"}, "statuses": statuses,
                "workflows": [{"name": spec["name"], "description": spec.get("description", ""), "startPointLayout": {"x": 0, "y": 0},
                               "statuses": layout, "transitions": transitions}]}

    def workflow(self) -> None:
        name = self.spec["workflow"]["name"]
        found = self.call("GET", f"/rest/api/3/workflows/search?workflowName={name.replace(' ', '%20')}").get("values", [])
        if any(w["name"] == name for w in found):
            self.have(f"workflow {name}")
            return
        payload = self.workflow_payload()
        problems = self.call("POST", "/rest/api/3/workflows/create/validation", {"payload": payload, "validationOptions": {"levels": ["ERROR"]}}).get("errors", [])
        if problems:
            raise SetupError(f"Jira rejects the workflow definition: {json.dumps(problems)[:400]}")
        if self.step(f"create workflow {name} ({len(payload['statuses'])} statuses, {len(payload['workflows'][0]['transitions'])} transitions)"):
            self.call("POST", "/rest/api/3/workflows/create", payload)

    def scheme(self) -> None:
        name, workflow = self.spec["scheme"]["name"], self.spec["workflow"]["name"]
        schemes = {s["name"]: s for s in self.pages("/rest/api/3/workflowscheme")}
        found = schemes.get(name)
        scheme_id = str(found["id"]) if found else ""
        if found:
            self.have(f"workflow scheme {name} ({scheme_id})")
        elif self.step(f"create workflow scheme {name} (default workflow {workflow})"):
            scheme_id = str(self.call("POST", "/rest/api/3/workflowscheme", {"name": name, "description": "AI Flow", "defaultWorkflow": workflow})["id"])
        current = self.call("GET", f"/rest/api/3/workflowscheme/project?projectId={self.project_id}").get("values", [])
        if current and str(current[0]["workflowScheme"]["id"]) == scheme_id and scheme_id:
            self.have(f"project {self.key} already uses {name}")
            return
        if self.step(f"assign workflow scheme {name} to project {self.key}") and scheme_id:
            self.call("PUT", "/rest/api/3/workflowscheme/project", {"projectId": self.project_id, "workflowSchemeId": scheme_id})


def main(argv: list[str] | None = None) -> int:
    import argparse
    from .config import load
    from .jira import Jira
    parser = argparse.ArgumentParser(prog="aiflow-setup", description="Prepare a Jira project for AI Flow (dry run unless --apply).")
    parser.add_argument("project", help="Jira project key, for example JAIF")
    parser.add_argument("--apply", action="store_true", help="really create what is missing (default: only print the plan)")
    args = parser.parse_args(argv)
    cfg = load()
    events.configure(cfg.obs, cfg.machine, "setup", cfg.root / ".tmp" / "aiflow")
    try:
        Setup(Jira(cfg.root)._library(), load_spec(), args.project.upper(), args.apply).run()
    except SetupError as error:
        print(f"ERROR {error}", file=sys.stderr)
        events.close()
        return 1
    events.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
