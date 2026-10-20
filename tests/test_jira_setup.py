import unittest

from aiflow.jira import Jira, JiraError
from aiflow.jira_setup import Setup, SetupError, load_spec


class FakeJira:
    """A Jira site with a project and nothing from AI Flow yet; records every write."""

    def __init__(self, fields=None, workflow=False):
        self.writes: list[tuple[str, str, object]] = []
        self.fields = fields or []
        self.workflow = workflow

    def call(self, method, path, body=None):
        if method == "POST" and path.endswith("/validation"):  # a check, nothing is created
            return 200, {"errors": []}
        if method != "GET":
            self.writes.append((method, path, body))
            if method == "POST" and path == "/rest/api/3/field":
                return 201, {"id": "customfield_9" + str(len(self.writes))}
            if method == "POST" and path == "/rest/api/3/issuetype":
                return 201, {"id": "777"}
            if method == "POST" and path == "/rest/api/3/workflows/create/validation":
                return 200, {"errors": []}
            if method == "POST" and path == "/rest/api/3/workflowscheme":
                return 201, {"id": 55}
            return 200, {}
        if path == "/rest/api/3/project/ABC":
            return 200, {"id": "9001"}
        if path == "/rest/api/3/field":
            return 200, self.fields
        if "/context?" in path and "/option" not in path:
            return 200, {"values": [{"id": "ctx1"}], "isLast": True}
        if "/option?" in path:
            return 200, {"values": [], "isLast": True}
        if path.startswith("/rest/api/3/issuetypescreenscheme/project"):
            return 200, {"values": [{"issueTypeScreenScheme": {"id": "1"}}], "isLast": True}
        if path.startswith("/rest/api/3/issuetypescreenscheme/mapping"):
            return 200, {"values": [{"screenSchemeId": "2"}], "isLast": True}
        if path.startswith("/rest/api/3/screenscheme"):
            return 200, {"values": [{"screens": {"default": 3, "create": 3}}], "isLast": True}
        if path.endswith("/availableFields"):
            return 200, [{"id": f["id"]} for f in self.fields if f.get("id")] + [{"id": "customfield_91"}]
        if path.endswith("/tabs"):
            return 200, [{"id": 31}]
        if path == "/rest/api/3/issuetype":
            return 200, [{"id": "10", "name": "Task"}]
        if path.startswith("/rest/api/3/issuetypescheme/project"):
            return 200, {"values": [{"issueTypeScheme": {"id": "8"}}], "isLast": True}
        if path.startswith("/rest/api/3/issuetypescheme/mapping"):
            return 200, {"values": [{"issueTypeId": "10"}], "isLast": True}
        if path.startswith("/rest/api/3/workflows/search"):
            return 200, {"values": [{"name": "AI Flow Workflow v2"}] if self.workflow else []}
        if path.startswith("/rest/api/3/statuses/search"):
            return 200, {"values": [{"id": "5", "name": "To Do"}, {"id": "6", "name": "Done"}]}
        if path == "/rest/api/3/resolution":
            return 200, [{"id": "1", "name": "Done"}, {"id": "2", "name": "Won't Do"}]
        if path.startswith("/rest/api/3/workflowscheme/project"):
            return 200, {"values": []}
        if path.startswith("/rest/api/3/workflowscheme"):
            return 200, {"values": [], "isLast": True}
        raise AssertionError(f"unexpected GET {path}")


def run(api, apply, key="ABC"):
    lines: list[str] = []
    setup = Setup(api, load_spec(), key, apply, out=lines.append)
    setup.run()
    return setup, lines


class SetupTest(unittest.TestCase):
    def test_dry_run_plans_everything_and_writes_nothing(self):
        api = FakeJira()
        setup, lines = run(api, apply=False)
        self.assertEqual(api.writes, [])
        text = "\n".join(lines)
        for expected in ("create field Workspace", "create issue type Release", "create workflow AI Flow Workflow v2", "create workflow scheme"):
            self.assertIn(expected, text)
        self.assertGreater(setup.changes, 10)

    def test_apply_creates_each_object_once(self):
        api = FakeJira()
        run(api, apply=True)
        posts = [(m, p) for m, p, _ in api.writes]
        self.assertEqual(posts.count(("POST", "/rest/api/3/field")), 7)
        self.assertIn(("POST", "/rest/api/3/issuetype"), posts)
        self.assertIn(("POST", "/rest/api/3/workflows/create"), posts)
        self.assertIn(("POST", "/rest/api/3/workflowscheme"), posts)
        self.assertIn(("PUT", "/rest/api/3/workflowscheme/project"), posts)
        self.assertIn(("PUT", "/rest/api/3/issuetypescheme/8/issuetype"), posts)

    def test_workflow_payload_uses_uuid_references_and_existing_status_ids(self):
        payload = Setup(FakeJira(), load_spec(), "ABC").workflow_payload()
        by_name = {s["name"]: s for s in payload["statuses"]}
        self.assertEqual((by_name["To Do"]["id"], by_name["Done"]["id"]), ("5", "6"))
        self.assertNotIn("id", by_name["Blocked"])
        self.assertEqual(len(payload["statuses"][0]["statusReference"]), 36)
        transitions = payload["workflows"][0]["transitions"]
        self.assertEqual(len({t["id"] for t in transitions}), len(transitions))
        decline = next(t for t in transitions if t["name"] == "Decline")
        self.assertEqual(decline["actions"][0]["parameters"]["value"], "2")
        self.assertEqual(len(decline["links"]), 9)

    def test_existing_objects_are_left_alone(self):
        spec = load_spec()
        fields = [{"id": f"customfield_{n}", "name": f["name"], "custom": True} for n, f in enumerate(spec["fields"], 10)]
        api = FakeJira(fields=fields, workflow=True)
        _, lines = run(api, apply=True)
        writes = [(m, p) for m, p, _ in api.writes]
        self.assertNotIn(("POST", "/rest/api/3/field"), writes)
        self.assertNotIn(("POST", "/rest/api/3/workflows/create"), writes)
        self.assertTrue(any(line.startswith("OK    field Workspace") for line in lines))

    def test_two_fields_with_one_name_stop_the_setup(self):
        fields = [{"id": "customfield_1", "name": "Workspace", "custom": True}, {"id": "customfield_2", "name": "Workspace", "custom": True}]
        with self.assertRaises(SetupError):
            run(FakeJira(fields=fields), apply=False)


class FieldNameTest(unittest.TestCase):
    def make(self, fields):
        jira = Jira.__new__(Jira)
        jira._field_ids = None
        jira._api = type("A", (), {"call": lambda self, m, p, b=None: (200, fields)})()
        return jira

    def test_id_is_kept_and_name_is_resolved(self):
        jira = self.make([{"id": "customfield_7", "name": "AI Model", "custom": True}, {"id": "summary", "name": "Summary", "custom": False}])
        self.assertEqual(jira.field_id("customfield_10217"), "customfield_10217")
        self.assertEqual(jira.field_id("AI Model"), "customfield_7")

    def test_unknown_or_ambiguous_name_is_an_error(self):
        jira = self.make([{"id": "customfield_1", "name": "Type", "custom": True}, {"id": "customfield_2", "name": "Type", "custom": True}])
        with self.assertRaises(JiraError):
            jira.field_id("Type")
        with self.assertRaises(JiraError):
            jira.field_id("Nope")


class ReviewFixTests(unittest.TestCase):
    def test_every_transition_into_done_sets_the_resolution(self):
        spec = load_spec()
        missing = [t["name"] for t in spec["workflow"]["transitions"] if t["to"] == "Done" and t.get("resolution") != "Done"]
        self.assertEqual(missing, [])

    def test_a_select_field_without_a_context_is_an_error_when_applying_and_a_plan_otherwise(self):
        class NoContext(FakeJira):
            def call(self, method, path, body=None):
                if "/context?" in path and "/option" not in path:
                    return 200, {"values": [], "isLast": True}
                return super().call(method, path, body)
        spec = load_spec()
        fields = [{"id": f"customfield_{n}", "name": f["name"], "custom": True} for n, f in enumerate(spec["fields"], 10)]
        lines = []
        Setup(NoContext(fields=fields, workflow=True), spec, "ABC", False, out=lines.append).run()
        self.assertTrue(any("option(s) to AI Model" in line for line in lines))  # a dry run still plans it
        with self.assertRaises(SetupError):
            Setup(NoContext(fields=fields, workflow=True), spec, "ABC", True, out=lambda _l: None).run()
