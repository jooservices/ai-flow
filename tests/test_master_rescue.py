import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiflow import master_rescue
from aiflow.config import ConfigError, load
from aiflow.incidents import find_incidents
from aiflow.jira import JiraError
from aiflow.master_rescue import MasterSettings, evaluate, owner_only_topic, parse
from aiflow.operator import Operator
from aiflow.operator_policy import Policy
from aiflow.supervisor import main
from tests.test_operator import JiraWithLabels, Notifier, state, ticket

NOW = 1_000_000.0
SETTINGS = MasterSettings()


def verdict(status, minutes, **extra):
    item = ticket("JAIF-1", status, minutes, **extra)
    return evaluate(item, SETTINGS, minutes)


class SettingsTests(unittest.TestCase):
    def test_defaults_are_the_ticket_values(self):
        self.assertEqual(SETTINGS.waits, {"Config": 10, "Attempts": 0, "Rework": 0, "Unclear": 30})
        self.assertEqual((SETTINGS.in_progress_minutes, SETTINGS.total_work_minutes, SETTINGS.master_minutes, SETTINGS.total_cap_minutes),
                         (60, 120, 60, 180))

    def test_valid_settings_are_read(self):
        found = parse({"config_wait_minutes": 5, "attempts_wait_minutes": 1, "in_progress_minutes": 45, "total_work_minutes": 100,
                       "master_minutes": 30, "total_cap_minutes": 200, "model": "Claude:opus:max", "taken_by_field": "Taken by"}, "x")
        self.assertEqual((found.waits["Config"], found.waits["Attempts"], found.waits["Unclear"]), (5, 1, 30))
        self.assertEqual((found.in_progress_minutes, found.total_work_minutes, found.master_minutes, found.total_cap_minutes), (45, 100, 30, 200))
        self.assertEqual(found.model, "Claude:opus:max")

    def test_invalid_settings_are_rejected(self):
        for table in ({"config_wait_minutes": -1}, {"config_wait_minutes": "10"}, {"rework_wait_minutes": True}, {"quota_wait_minutes": 5},
                      {"offline_wait_minutes": 5}, {"in_progress_minutes": 0}, {"master_minutes": 1.5}, {"unknown": 1},
                      {"model": "Codex:gpt"}, {"model": "Claude:"}, {"model": 3}, {"taken_by_field": " "},
                      {"total_cap_minutes": 100}, ["not a table"]):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                parse(table, "aiflow.toml [master]")
        self.assertEqual(parse({"attempts_wait_minutes": 0}, "x").waits["Attempts"], 0)

    def test_the_file_is_validated_by_the_configuration_load_and_the_check(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "aiflow").mkdir()
        (folder / "aiflow" / "a.toml").write_text('[project]\njira_key = "JAIF"\nrepo = "ai-flow"\n')
        (folder / "aiflow.toml").write_text("[master]\nconfig_wait_minutes = 3\nmaster_minutes = 20\n")
        config = load(env={"AIFLOW_MACHINE": "wsl"}, config_dir=folder)
        self.assertEqual((config.registry.master.waits["Config"], config.registry.master.master_minutes), (3, 20))
        redis = mock.MagicMock()
        redis.get.return_value = None
        with mock.patch("aiflow.config.load", return_value=config), mock.patch("aiflow.supervisor.Redis", return_value=redis), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["check"]), 0)
        self.assertEqual(config.registry.master.waits["Config"], 3)
        self.assertIn("master rescue: waits", output.getvalue())
        self.assertIn("Config 3 min", output.getvalue())
        (folder / "aiflow.toml").write_text("[master]\nquota_wait_minutes = 3\n")
        with self.assertRaises(ConfigError):
            load(env={"AIFLOW_MACHINE": "wsl"}, config_dir=folder)
        (folder / "aiflow.toml").write_text("[master]\nmaster_minutes = 0\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as errors, mock.patch("aiflow.config.load", side_effect=ConfigError("bad")):
            self.assertEqual(main(["check"]), 2)
        self.assertIn("CONFIG ERROR", errors.getvalue())

    def test_the_example_configuration_is_valid(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "aiflow").mkdir()
        (folder / "aiflow" / "a.toml").write_text('[project]\njira_key = "JAIF"\nrepo = "ai-flow"\n')
        (folder / "aiflow.toml").write_text((Path(__file__).parent.parent / "config.example" / "aiflow.toml").read_text())
        self.assertEqual(load(env={"AIFLOW_MACHINE": "wsl"}, config_dir=folder).registry.master, MasterSettings(model="Claude:opus:max"))


class RulesTests(unittest.TestCase):
    def test_blocked_kinds_wait_their_own_time(self):
        for kind, wait in (("Config", 10), ("Attempts", 0), ("Rework", 0), ("Unclear", 30)):
            with self.subTest(kind=kind):
                self.assertEqual(verdict("Blocked", wait, blocked_kind=kind).mode, "rescue")
                if wait:
                    self.assertIsNone(verdict("Blocked", wait - 1, blocked_kind=kind))

    def test_quota_offline_and_other_kinds_are_never_handed_to_claude(self):
        for kind in ("Quota", "Offline", "CI", "Human", "Other", ""):
            with self.subTest(kind=kind):
                self.assertIsNone(verdict("Blocked", 999, blocked_kind=kind))

    def test_in_progress_stretch_and_total_work_limits(self):
        self.assertIsNone(verdict("In Progress", 59, stretch_minutes=59, worked_minutes=59))
        self.assertIn("one stretch", verdict("In Progress", 60, stretch_minutes=60, worked_minutes=60).trigger)
        self.assertIn("total work time", verdict("In Progress", 10, stretch_minutes=10, worked_minutes=120).trigger)
        self.assertIsNone(verdict("In QA", 500, stretch_minutes=0, worked_minutes=0))  # CI / the gate is not work time

    def test_accumulated_work_still_qualifies_after_moving_into_qa(self):
        found = verdict("In QA", 30, stretch_minutes=0, worked_minutes=120)
        self.assertIn("total work time", found.trigger)

    def test_the_limits_are_configurable(self):
        custom = MasterSettings(in_progress_minutes=20, total_work_minutes=30)
        item = ticket("JAIF-1", "In Progress", 20, stretch_minutes=20, worked_minutes=20)
        self.assertEqual(evaluate(item, custom, 20).mode, "rescue")
        self.assertIsNone(evaluate(item, SETTINGS, 20))

    def test_budget_is_master_minutes_under_the_total_cap(self):
        self.assertEqual(verdict("Blocked", 10, blocked_kind="Config", worked_minutes=120).remaining_minutes, 60)
        self.assertEqual(verdict("Blocked", 10, blocked_kind="Config", worked_minutes=150).remaining_minutes, 30)
        self.assertEqual(verdict("Blocked", 10, blocked_kind="Config", worked_minutes=179).remaining_minutes, 1)
        capped = verdict("Blocked", 10, blocked_kind="Config", worked_minutes=180)
        self.assertEqual((capped.mode, capped.remaining_minutes), ("owner", 0))
        spent = verdict("Blocked", 10, blocked_kind="Config", worked_minutes=181)
        self.assertEqual((spent.mode, spent.remaining_minutes), ("owner", 0))

    def test_one_rescue_per_ticket(self):
        self.assertIsNone(verdict("Blocked", 99, blocked_kind="Config", labels=["master-rescue"]))
        self.assertIsNone(verdict("Blocked", 99, blocked_kind="Config", rescued=True))

    def test_owner_only_work_goes_to_the_owner_without_waiting(self):
        topics = {"Pay the invoice for the licence": "spending money", "Rotate the API key": "credentials, tokens or secrets",
                  "Delete the database": "deleting data", "Cut a release": "a release", "The requirements changed": "changing the requirements",
                  "Merge it by hand": "merging by hand", "Runtime setup of the host": "runtime setup",
                  "Install the missing tool": "changing installed software",
                  "Update the installed package": "changing installed software",
                  "Modify installed software": "changing installed software"}
        for text, topic in topics.items():
            with self.subTest(text=text):
                found = verdict("Blocked", 0, blocked_kind="Config", reason=text)  # even before the 10 minute wait
                self.assertEqual((found.mode, found.topic), ("owner", topic))
        self.assertEqual(owner_only_topic("a failing unit test"), "")
        self.assertEqual(owner_only_topic("Update the ticket documentation"), "")
        self.assertEqual(verdict("In Progress", 70, stretch_minutes=70, summary="Rotate the credentials").mode, "owner")

    def test_ordinary_owner_only_actions_are_detected_in_descriptions_and_comments(self):
        cases = (
            (ticket("JAIF-1", "In Progress", 1, summary="Routine task", description="Add a new requirement"),
             "changing the requirements"),
            (ticket("JAIF-2", "In Progress", 1, summary="Routine task", description="Routine task",
                    last_comments=[{"author": "Owner", "body": "Remove production data"}]), "deleting data"),
            (ticket("JAIF-3", "In Progress", 1, summary="Routine task", description="Set up the runtime"),
             "runtime setup"),
        )
        for item, topic in cases:
            with self.subTest(ticket=item["key"]):
                found = evaluate(item, SETTINGS, 1)
                self.assertEqual((found.mode, found.topic), ("owner", topic))

    def test_acceptance_criteria_template_heading_is_not_a_requirement_change(self):
        description = """## Business requirement
Add a status filter to the ticket list.

## Acceptance criteria
- AC1: The filter can be selected.

## Technical notes
Use the existing list endpoint.
"""
        item = ticket("JAIF-1", "In Progress", 1, summary="Add a status filter", description=description)
        self.assertIsNone(evaluate(item, SETTINGS, 1))

    def test_explicit_requirement_changes_are_owner_only(self):
        for text in ("Update the requirements", "Modify acceptance criteria", "Requirements changed after review"):
            with self.subTest(text=text):
                self.assertEqual(owner_only_topic(text), "changing the requirements")

    def test_owner_only_work_is_checked_before_work_limits_in_description_and_comments(self):
        description = ticket("JAIF-1", "In Progress", 1, summary="Routine maintenance", description="Rotate the API key")
        comment = ticket("JAIF-2", "In Progress", 1, summary="Routine maintenance", description="Routine maintenance",
                         last_comments=[{"author": "Owner", "body": "Use the new credentials for this task"}])
        for item, topic in ((description, "credentials, tokens or secrets"), (comment, "credentials, tokens or secrets")):
            with self.subTest(ticket=item["key"]):
                found = evaluate(item, SETTINGS, 1)
                self.assertEqual((found.mode, found.topic), ("owner", topic))
                self.assertIn("Owner-only work identified", found.trigger)

    def test_incident_replaces_the_generic_blocked_incident(self):
        found = find_incidents(state([ticket("A-1", "Blocked", 40, blocked_kind="Config")], master=SETTINGS))
        self.assertEqual([(i.id, i.kind) for i in found], [("rescue:A-1", "rescue")])
        self.assertEqual(found[0].facts["mode"], "rescue")
        plain = find_incidents(state([ticket("A-1", "Blocked", 40, blocked_kind="Config")]))
        self.assertEqual([i.id for i in plain], ["blocked:A-1"])

    def test_short_owner_only_in_progress_ticket_is_an_owner_incident(self):
        item = ticket("A-1", "In Progress", 1, summary="Routine maintenance", description="Rotate API key")
        found = find_incidents(state([item], master=SETTINGS))
        self.assertEqual([(incident.id, incident.facts["mode"]) for incident in found], [("rescue:A-1", "owner")])


class RescueRunTests(unittest.TestCase):
    def build(self, settings=SETTINGS):
        from tests.test_supervisor import FakeRedis, make
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira = JiraWithLabels()
        sup.online_pool = lambda: ["Claude", "Codex"]
        sup.usable_ais = lambda: ["Claude"]
        object.__setattr__(sup.cfg.registry, "master", settings)
        object.__setattr__(sup.cfg, "agent_models", {"Claude": ("opus", "max")})
        op = Operator(sup, Policy(enabled=True), Notifier(), lambda *a: ("ok", "", False))
        return op, sup, redis

    def incident(self, **extra):
        item = ticket("JAIF-1", "Blocked", 40, blocked_kind="Config", reason="the test setup fails", worked_minutes=130.0,
                      last_comments=[{"author": "AI Flow", "created": "", "body": "tried twice"}], **extra)
        return find_incidents(state([item], master=SETTINGS))[0]

    def test_handover_sets_status_taken_by_label_model_and_audit(self):
        op, sup, redis = self.build()
        redis.set("lease:JAIF-1", "w", 60)
        self.assertTrue(op.handle(self.incident()))
        jira = sup.jira
        self.assertEqual(jira.moves, [("JAIF-1", "Handover")])
        self.assertIn(("JAIF-1", "master-rescue"), jira.added)
        self.assertEqual(jira.fields[("JAIF-1", "Taken by")], {"value": "Claude"})
        self.assertEqual(jira.fields[("JAIF-1", "AI Model")], {"value": "Claude:opus"})
        self.assertIsNone(redis.get("lease:JAIF-1"))
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual((record["state"], record["budget"], record["model"]), ("active", 50, "opus:max"))
        body = [b for k, b in jira.comments if k == "JAIF-1"][0]
        self.assertIn("Stuck: Blocked for 40 min (Config)", body)
        self.assertIn("Tried:", body)
        self.assertIn("raw excerpts omitted", body)
        self.assertNotIn("the test setup fails", body)
        self.assertNotIn("tried twice", body)
        self.assertIn("Remaining budget: 50 min", body)
        self.assertIn("Taken by: Claude, model opus (effort max)", body)

    def test_owner_context_fetch_failures_fail_closed_to_the_owner(self):
        now = time.time()
        cases = (
            ("description", {"key": "JAIF-1", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                              "labels": [], "summary": "Routine maintenance"}),
            ("comments", {"key": "JAIF-2", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                           "labels": [], "summary": "Routine maintenance", "description": "Routine maintenance"}),
            ("description_type", {"key": "JAIF-3", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                                   "labels": [], "summary": "Routine maintenance"}),
            ("comments_type", {"key": "JAIF-4", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                                "labels": [], "summary": "Routine maintenance", "description": "Routine maintenance"}),
        )
        for failed_fetch, issue in cases:
            with self.subTest(failed_fetch=failed_fetch):
                op, sup, _ = self.build()
                sup.jira.found = [issue]
                sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
                written = []
                sup.jira.comment = lambda key, body: written.append((key, body))

                def fail_description(key, full=False):
                    raise JiraError("description unavailable")

                def fail_comments(key, last=3):
                    raise JiraError("comments unavailable")

                def read_description(key, full=False):
                    if failed_fetch == "description":
                        return fail_description(key, full)
                    if failed_fetch == "description_type":
                        return {"description": ["unexpected shape"]}
                    return {"description": issue["description"]}

                def read_comments(key, last=3):
                    if failed_fetch == "comments":
                        return fail_comments(key, last)
                    if failed_fetch == "comments_type":
                        return {"body": "unexpected shape"}
                    return []

                sup.jira.view = read_description
                sup.jira.comments = read_comments
                snapshot = op.state()
                self.assertFalse(snapshot["tickets"][0]["context_complete"])
                incidents = [incident for incident in find_incidents(snapshot) if incident.kind == "rescue"]
                self.assertEqual([(incident.kind, incident.facts["mode"], incident.facts["topic"])
                                  for incident in incidents],
                                 [("rescue", "owner", master_rescue.CONTEXT_UNAVAILABLE)])
                op.handle(incidents[0])
                self.assertEqual(sup.jira.moves, [])
                self.assertIn((issue["key"], "needs-owner"), sup.jira.added)
                self.assertTrue(any("required Jira description or recent comments could not be read" in body
                                    for _, body in written))

    def test_empty_description_and_comments_are_complete_when_reads_succeed(self):
        from tests.test_supervisor import FakeRedis, make
        redis = FakeRedis()
        sup = make(redis=redis)
        now = time.time()
        sup.jira.found = [{"key": "JAIF-1", "status": "In Progress",
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                           "labels": [], "summary": "Routine maintenance"}]
        sup.jira.view = lambda key, full=False: {"description": ""}
        sup.jira.comments = lambda key, last=3: []
        sup.online_pool = lambda: ["Claude"]
        sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
        op = Operator(sup, Policy(enabled=True), Notifier(), lambda *a: ("ok", "", False))
        snapshot = op.state()
        self.assertTrue(snapshot["tickets"][0]["context_complete"])
        self.assertFalse(any(item.kind == "rescue" for item in find_incidents(snapshot)))

    def test_owner_routing_audit_omits_raw_reasons_comments_and_logs(self):
        secret = "CANARY_SECRET_92_DO_NOT_COPY"
        op, sup, _ = self.build()
        item = ticket("JAIF-1", "Blocked", 1, blocked_kind="Config", reason=f"Rotate API token {secret}",
                      last_comments=[{"author": "Owner", "body": f"Rotate the API token; value {secret}"}])
        incident = find_incidents(state([item], master=SETTINGS))[0]
        logs = []
        op.sup_log = logs.append
        op.handle(incident)
        audit = "\n".join(logs + [body for _, body in sup.jira.comments])
        self.assertNotIn(secret, audit)
        self.assertNotIn("Rotate the API token", audit)
        self.assertIn("raw excerpts omitted", audit)

    def test_flow_doctor_does_not_copy_sensitive_comment_excerpts(self):
        secret = "CANARY_SECRET_92_DOCTOR"
        op, sup, redis = self.build()
        notes = []
        sup.jira.comment = lambda key, body: notes.append((key, body))
        op.handle(self.incident())
        sup.jira.status = "Blocked"
        sup.jira.comments = lambda key, last=3: [{"author": "Claude", "body": f"Finding: token={secret}"}]
        op.check_rescues()
        doctor = [body for _, body in notes if body.startswith("AI Flow doctor")][0]
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertNotIn(secret, doctor)
        self.assertIn("review them on the ticket", doctor)
        self.assertNotIn(secret, record["doctor_findings"])

    def test_only_one_rescue_per_ticket(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        again = find_incidents(state([ticket("JAIF-1", "Blocked", 80, blocked_kind="Config", labels=["master-rescue"], rescued=True)], master=SETTINGS))
        self.assertEqual([i.kind for i in again], ["blocked"])

    def test_unavailable_claude_goes_to_the_owner_at_once(self):
        for reason, setup in (("quota", lambda sup, redis: redis.set("cooldown:Claude", "1", 60)),
                              ("offline", lambda sup, redis: setattr(sup, "online_pool", lambda: ["Codex"]))):
            with self.subTest(reason=reason):
                op, sup, redis = self.build()
                setup(sup, redis)
                op.handle(self.incident())
                self.assertEqual(sup.jira.moves, [])
                self.assertIn(("JAIF-1", "needs-owner"), sup.jira.added)
                self.assertTrue(any(f"Claude is unavailable ({reason})" in b for _, b in sup.jira.comments))
                self.assertIsNone(redis.get("rescue:JAIF-1"))

    def test_owner_only_work_is_never_handed_over(self):
        op, sup, redis = self.build()
        item = ticket("JAIF-1", "Blocked", 1, blocked_kind="Config", reason="needs a new API token")
        op.handle(find_incidents(state([item], master=SETTINGS))[0])
        self.assertEqual(sup.jira.moves, [])
        self.assertIn(("JAIF-1", "needs-owner"), sup.jira.added)
        self.assertTrue(any("credentials, tokens or secrets" in b for _, b in sup.jira.comments))

    def test_the_operator_snapshot_checks_description_and_recent_comments_before_thresholds(self):
        op, sup, redis = self.build()
        now = time.time()
        sup.jira.found = [
            {"key": "JAIF-1", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
             "labels": [], "summary": "Routine maintenance"},
            {"key": "JAIF-2", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
             "labels": [], "summary": "Routine maintenance"},
        ]
        view_calls, comment_calls = [], []
        descriptions = {"JAIF-1": "Rotate the API key", "JAIF-2": "Routine maintenance"}
        comments = {"JAIF-1": [], "JAIF-2": [{"author": "Owner", "body": "Use the credentials"}]}

        def view(key, full=False):
            view_calls.append(key)
            return {"description": descriptions[key]}

        def recent_comments(key, last=3):
            comment_calls.append(key)
            return comments[key]

        sup.jira.view = view
        sup.jira.comments = recent_comments
        sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
        snapshot = op.state()
        incidents = [item for item in find_incidents({**snapshot, "now": now}) if item.kind == "rescue"]
        self.assertTrue(all(item["context_complete"] for item in snapshot["tickets"]))
        self.assertEqual([(item.subject, item.facts["topic"]) for item in incidents],
                         [("JAIF-1", "credentials, tokens or secrets"), ("JAIF-2", "credentials, tokens or secrets")])
        op.state()
        self.assertEqual(view_calls, ["JAIF-1", "JAIF-2"])
        self.assertEqual(comment_calls, ["JAIF-1", "JAIF-2"])
        sup.jira.found[0]["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + 2))
        op.state()
        self.assertEqual(view_calls, ["JAIF-1", "JAIF-2", "JAIF-1"])
        self.assertEqual(comment_calls, ["JAIF-1", "JAIF-2", "JAIF-1"])

    def test_failed_handover_leaves_no_record_and_retries_later(self):
        op, sup, redis = self.build()
        sup.jira.transition = lambda key, status: False
        incident = self.incident()
        op.handle(incident)
        self.assertIsNone(redis.get("rescue:JAIF-1"))
        self.assertIsNotNone(redis.get(f"operator:cool:{incident.id}"))
        self.assertNotIn(("JAIF-1", "master-rescue"), sup.jira.added)
        self.assertIsNone(sup.jira.fields[("JAIF-1", "Taken by")])
        self.assertIsNone(sup.jira.fields[("JAIF-1", "AI Model")])
        retry = find_incidents(state([ticket("JAIF-1", "Blocked", 40, blocked_kind="Config")], master=SETTINGS))
        self.assertEqual([(item.id, item.kind) for item in retry], [(incident.id, "rescue")])

    def test_lost_handover_response_is_reconciled_from_jira_status(self):
        op, sup, redis = self.build()

        def accepted_then_lost(key, status):
            sup.jira.status = status
            raise JiraError("response was lost after Jira accepted the transition")

        sup.jira.transition = accepted_then_lost
        self.assertTrue(op.handle(self.incident()))
        record = json.loads(redis.get("rescue:JAIF-1"))

        self.assertEqual(record["state"], "active")
        self.assertEqual(sup.jira.moves, [])
        self.assertIn(("JAIF-1", "master-rescue"), sup.jira.added)
        self.assertTrue(any("Master rescue handover audit: rescue:JAIF-1" in body for _, body in sup.jira.comments))

    def test_uncertain_handover_is_persisted_until_status_can_be_checked(self):
        op, sup, redis = self.build()
        sup.jira.transition = mock.Mock(side_effect=JiraError("ambiguous response"))
        sup.jira.view = mock.Mock(side_effect=JiraError("status unavailable"))
        self.assertTrue(op.handle(self.incident()))
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual(record["state"], "handover_pending")
        self.assertNotIn(("JAIF-1", "master-rescue"), sup.jira.added)

        sup.jira.view = lambda key, full=False: {"status": "Handover"}
        op.check_rescues()
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual(record["state"], "active")
        self.assertIn(("JAIF-1", "master-rescue"), sup.jira.added)

    def test_failed_handover_retries_cleanup_of_temporary_fields(self):
        op, sup, redis = self.build()
        sup.jira.transition = lambda key, status: False
        set_field = sup.jira.set_field
        failed_clears = set()

        def fail_first_clear(key, field, value):
            if value is None and field not in failed_clears:
                failed_clears.add(field)
                raise JiraError("temporary field cleanup failure")
            set_field(key, field, value)

        sup.jira.set_field = fail_first_clear
        op.handle(self.incident())
        self.assertIsNone(redis.get("rescue:JAIF-1"))
        self.assertIsNotNone(redis.get("operator:rescue-cleanup:JAIF-1"))
        self.assertEqual(sup.jira.fields[("JAIF-1", "Taken by")], {"value": "Claude"})
        op.check_rescues()
        self.assertIsNone(redis.get("operator:rescue-cleanup:JAIF-1"))
        self.assertIsNone(sup.jira.fields[("JAIF-1", "Taken by")])
        self.assertIsNone(sup.jira.fields[("JAIF-1", "AI Model")])

    def test_accepted_handover_retries_a_failed_label_write(self):
        op, sup, redis = self.build()
        add_label = sup.jira.add_label
        calls = []

        def fail_once(key, label):
            calls.append((key, label))
            if len(calls) == 1:
                raise JiraError("temporary label write failure")
            add_label(key, label)

        sup.jira.add_label = fail_once
        op.handle(self.incident())
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual(record["state"], "active")
        self.assertTrue(record["label_pending"])
        self.assertEqual(sup.jira.moves, [("JAIF-1", "Handover")])
        sup.jira.status = "Handover"
        op.check_rescues()
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertFalse(record["label_pending"])
        self.assertEqual(calls, [("JAIF-1", "master-rescue"), ("JAIF-1", "master-rescue")])
        self.assertIn(("JAIF-1", "master-rescue"), sup.jira.added)

    def test_temporary_handover_audit_comment_failure_is_retried(self):
        op, sup, redis = self.build()
        posted, attempts = [], []

        def comment(key, body):
            attempts.append(body)
            if len(attempts) == 1:
                raise JiraError("temporary comment failure")
            posted.append((key, body))

        sup.jira.comment = comment
        sup.jira.comments = lambda key, last=10: [{"body": body} for _, body in posted]
        op.handle(self.incident())
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertTrue(record["handover_audit_pending"])
        self.assertEqual(record["handover_audit_attempts"], 1)

        sup.jira.status = "Handover"
        op.check_rescues()

        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertFalse(record["handover_audit_pending"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(posted), 1)
        self.assertIn("Master rescue handover audit: rescue:JAIF-1", posted[0][1])
        self.assertIn("Remaining budget: 50 min", posted[0][1])

    def test_handover_audit_retry_detects_a_comment_accepted_before_timeout(self):
        op, sup, redis = self.build()
        posted, attempts = [], []

        def comment(key, body):
            attempts.append(body)
            posted.append((key, body))
            if len(attempts) == 1:
                raise JiraError("comment response was lost")

        sup.jira.comment = comment
        sup.jira.comments = lambda key, last=10: [{"body": body} for _, body in posted]
        op.handle(self.incident())
        sup.jira.status = "Handover"
        op.check_rescues()

        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertFalse(record["handover_audit_pending"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(posted), 1)

    def test_label_retry_failure_does_not_block_the_flow_doctor(self):
        op, sup, redis = self.build()
        sup.jira.add_label = mock.Mock(side_effect=JiraError("label service unavailable"))
        op.handle(self.incident())
        sup.jira.status = "Handover"
        record = json.loads(redis.get("rescue:JAIF-1"))
        record["started"] = int(time.time()) - 60 * 60
        redis.set_persistent("rescue:JAIF-1", json.dumps(record))
        op.check_rescues()
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual(record["state"], "failed")
        self.assertTrue(record["label_pending"])
        self.assertEqual(sup.jira.moves[-1], ("JAIF-1", "To Do"))
        self.assertTrue(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))

    def test_rescue_without_a_result_goes_through_the_flow_doctor_to_development(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        sup.jira.status = "Handover"
        record = json.loads(redis.get("rescue:JAIF-1"))
        op.check_rescues()  # the budget is not used up yet
        self.assertEqual(sup.jira.moves, [("JAIF-1", "Handover")])
        record["started"] = int(time.time()) - 51 * 60
        redis.set_persistent("rescue:JAIF-1", json.dumps(record))
        redis.set("blocked:JAIF-1", "{}", 60)
        op.check_rescues()
        self.assertEqual(sup.jira.moves[-1], ("JAIF-1", "To Do"))
        self.assertNotIn(("JAIF-1", "needs-owner"), sup.jira.added)
        self.assertIsNone(sup.jira.fields[("JAIF-1", "Taken by")])
        body = [b for k, b in sup.jira.comments if b.startswith("AI Flow doctor")][0]
        self.assertIn("rescue budget of 50 min ran out without a result", body)
        self.assertIn("Stuck: Blocked for 40 min", body)
        self.assertIn("not escalated to a person", body)
        self.assertEqual(json.loads(redis.get("rescue:JAIF-1"))["state"], "failed")
        op.check_rescues()  # handled once
        self.assertEqual(len(sup.jira.moves), 2)

    def test_end_without_a_result_reports_the_cause_and_findings(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        op.rescue.end("JAIF-1", ok=False, cause="Claude hit a usage limit", findings="the fixture is missing")
        body = [b for k, b in sup.jira.comments if b.startswith("AI Flow doctor")][0]
        self.assertIn("Cause: Claude hit a usage limit", body)
        self.assertIn("Findings: the fixture is missing", body)
        self.assertEqual(sup.jira.moves[-1], ("JAIF-1", "To Do"))

    def test_early_unsuccessful_status_uses_the_flow_doctor_and_retries_until_returned(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        sup.jira.status = "Blocked"
        transition = sup.jira.transition
        attempts = []

        def fail_doctor_once(key, status):
            attempts.append((key, status))
            return False if len(attempts) == 1 else transition(key, status)

        sup.jira.transition = fail_doctor_once
        op.check_rescues()
        record = json.loads(redis.get("rescue:JAIF-1"))
        self.assertEqual(record["state"], "doctor_pending")
        self.assertIn("without a confirmed result", record["doctor_cause"])
        self.assertEqual(len([body for _, body in sup.jira.comments if body.startswith("AI Flow doctor")]), 1)
        op.check_rescues()
        self.assertEqual(json.loads(redis.get("rescue:JAIF-1"))["state"], "failed")
        self.assertEqual(len([body for _, body in sup.jira.comments if body.startswith("AI Flow doctor")]), 1)
        self.assertEqual(sup.jira.moves[-1], ("JAIF-1", "To Do"))

    def test_only_a_confirmed_success_status_finishes_during_reconciliation(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        record = json.loads(redis.get("rescue:JAIF-1"))
        record["started"] = int(time.time()) - 100 * 60
        redis.set_persistent("rescue:JAIF-1", json.dumps(record))
        sup.jira.status = "Done"
        op.check_rescues()
        self.assertEqual(json.loads(redis.get("rescue:JAIF-1"))["state"], "finished")
        self.assertFalse(any(body.startswith("AI Flow doctor") for _, body in sup.jira.comments))

    def test_a_rescue_with_a_result_is_closed_without_the_doctor(self):
        op, sup, redis = self.build()
        op.handle(self.incident())
        op.rescue.end("JAIF-1", ok=True)
        self.assertEqual(json.loads(redis.get("rescue:JAIF-1"))["state"], "finished")
        self.assertEqual(sup.jira.moves, [("JAIF-1", "Handover")])

    def test_the_operator_state_carries_work_time_and_the_blocked_reason(self):
        op, sup, redis = self.build()
        now = time.time()
        sup.jira.found = [{"key": "JAIF-1", "status": "In Progress", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                           "labels": [], "summary": "A ticket"},
                          {"key": "JAIF-2", "status": "Blocked", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                           "labels": [], "summary": "Other"}]
        redis.set("statussince:JAIF-1", str(now - 15 * 60), 60)
        redis.set("worked:JAIF-1", "100", 60)
        redis.set("lease:JAIF-1", "w", 60)
        redis.set("blocked:JAIF-2", json.dumps({"kind": "Config", "reason": "env missing"}), 60)
        redis.set("statussince:JAIF-2", str(now - 20 * 60), 60)
        sup.jira.comments = lambda key, last=3: []
        sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
        snapshot = op.state()
        first, second = snapshot["tickets"]
        self.assertEqual((round(first["stretch_minutes"]), round(first["worked_minutes"])), (15, 115))
        self.assertEqual((second["reason"], second["blocked_kind"]), ("env missing", "Config"))
        self.assertEqual(find_incidents({**snapshot, "now": now})[0].id, "rescue:JAIF-2")

    def test_work_time_accumulates_when_a_work_status_is_left_and_ci_is_not_counted(self):
        from tests.test_supervisor import FakeRedis, make
        redis = FakeRedis()
        sup = make(redis=redis)
        now = time.time()
        for status, minutes, to in (("In Progress", 30, "In Review"), ("In Review", 10, "In QA"), ("In QA", 500, "In Review")):
            redis.set_persistent("status:JAIF-1", status)
            redis.set_persistent("statussince:JAIF-1", str(time.time() - minutes * 60))
            sup.record_status("JAIF-1", to, "test")
        self.assertAlmostEqual(float(redis.get("worked:JAIF-1")), 40, delta=0.2)
        self.assertLess(time.time() - now, 5)


if __name__ == "__main__":
    unittest.main()
