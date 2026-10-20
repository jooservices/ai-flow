import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiflow.config import ConfigError
from aiflow.incidents import find_incidents
from aiflow.operator import Operator, parse_output
from aiflow.operator_policy import Policy, load as load_policy
from tests.test_supervisor import FakeJira, FakeRedis, make

NOW = 1_000_000.0


def ticket(key, status, minutes, **extra):
    found = dict(key=key, status=status, status_since=NOW - minutes * 60, updated=NOW - minutes * 60, labels=[], leased=False,
                 blocked_kind="", reason="", cycles=0, last_comments=[], pull_request=None, ci_state="unknown", ci_since=None,
                 minutes_waiting_for_ci=0)
    found.update(extra)
    return found


def state(tickets=(), heartbeats=None, pool=("Claude",), **extra):
    thresholds = dict(blocked_minutes=30, stuck_minutes=120, ci_wait_minutes=120, rework_limit=3)
    thresholds.update(extra.pop("thresholds", {}))
    roles = ("developer", "reviewer", "qa")
    role_pools = {role: list(pool) for role in roles}
    role_pools.update(extra.pop("role_pools", {}))
    return dict(now=NOW, tickets=list(tickets), heartbeats=heartbeats or {}, stale_seconds=300,
                thresholds=thresholds, role_pools=role_pools, **extra)


class PolicyTests(unittest.TestCase):
    def write(self, text):
        folder = Path(tempfile.mkdtemp())
        (folder / "operator.toml").write_text(text)
        return folder

    def test_without_a_file_the_operator_is_off_with_defaults(self):
        policy = load_policy(Path(tempfile.mkdtemp()))
        self.assertFalse(policy.enabled)
        self.assertEqual((policy.blocked_minutes, policy.max_runs_per_hour, policy.agent), (30, 6, "Claude"))
        self.assertIn("a release", policy.escalate_always)

    def test_values_are_read(self):
        policy = load_policy(self.write('[operator]\nenabled = true\nagent = "Codex"\nblocked_minutes = 5\n[escalation]\nalways = ["money"]'))
        self.assertEqual((policy.enabled, policy.agent, policy.blocked_minutes, policy.escalate_always), (True, "Codex", 5, ("money",)))

    def test_bad_values_are_rejected(self):
        for text in ('[operator]\nenabled = "yes"', '[operator]\nagent = "OpenCode"', '[operator]\nblocked_minutes = 0', '[operator]\nblocked_minutes = "5"',
                     '[operator]\nunknown = 1', '[other]\nx = 1', '[escalation]\nalways = []', '[escalation]\nextra = 1'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_policy(self.write(text))


class IncidentTests(unittest.TestCase):
    policy = Policy(enabled=True)

    def kinds(self, st):
        return sorted(i.id for i in find_incidents(st))

    def test_each_kind_is_found_once_with_a_stable_id(self):
        st = state([ticket("A-1", "Blocked", 45), ticket("A-2", "In Progress", 200),
                    ticket("A-3", "In QA", 130, pull_request={"number": 7}, ci_state="pending", ci_since=NOW - 130 * 60,
                           minutes_waiting_for_ci=130), dict(ticket("A-4", "In Review", 5), cycles=4)],
                   heartbeats={"wsl:1": NOW - 10, "wsl:2": None, "wsl:3": NOW - 301}, pool=())
        self.assertEqual(self.kinds(st), ["ai_pool:developer", "ai_pool:qa", "ai_pool:reviewer", "blocked:A-1", "ci:A-3",
                                         "rework:A-4", "stuck:A-2", "worker:wsl:2", "worker:wsl:3"])
        self.assertEqual(self.kinds(st), self.kinds(st))

    def test_nothing_is_an_incident_when_all_is_well(self):
        st = state([ticket("A-1", "Blocked", 10), dict(ticket("A-2", "In Progress", 500), leased=True), ticket("A-3", "Human review", 900)],
                   heartbeats={"wsl:1": NOW - 5})
        self.assertEqual(self.kinds(st), [])

    def test_quota_offline_hold_and_escalated_are_left_alone(self):
        blocked = [dict(ticket("A-1", "Blocked", 99), blocked_kind="Quota"), dict(ticket("A-2", "Blocked", 99), blocked_kind="Offline"),
                   dict(ticket("A-3", "Blocked", 99), labels=["aiflow-hold"]), dict(ticket("A-4", "Blocked", 99), labels=["needs-owner"]),
                   dict(ticket("A-5", "In Progress", 500), labels=["needs-owner"]),
                   dict(ticket("A-6", "In QA", 500, pull_request={"number": 2}, ci_state="pending", minutes_waiting_for_ci=500),
                        labels=["needs-owner"])]
        self.assertEqual(self.kinds(state(blocked)), [])

    def test_blocked_and_stuck_thresholds_use_status_entry_time(self):
        st = state([ticket("A-1", "Blocked", 30), ticket("A-2", "Blocked", 40, status_since=NOW - 5 * 60),
                    ticket("A-3", "In Review", 120)])
        self.assertEqual(self.kinds(st), ["blocked:A-1", "stuck:A-3"])

    def test_ci_incident_requires_a_pull_request_still_waiting_for_ci(self):
        waiting = ticket("A-1", "In QA", 180, pull_request={"number": 9, "url": "https://example/pr/9"},
                         ci_state="pending", ci_since=NOW - 120 * 60, minutes_waiting_for_ci=120)
        no_pr = ticket("A-2", "In QA", 180, ci_state="pending", ci_since=NOW - 180 * 60, minutes_waiting_for_ci=180)
        passed = ticket("A-3", "In QA", 180, pull_request={"number": 11}, ci_state="passed",
                        ci_since=NOW - 180 * 60, minutes_waiting_for_ci=180)
        within_ci_limit = ticket("A-4", "In QA", 180, pull_request={"number": 12}, ci_state="pending",
                                 ci_since=NOW - 10 * 60, minutes_waiting_for_ci=10)
        no_checks = ticket("A-5", "In QA", 180, pull_request={"number": 13}, ci_state="none",
                           ci_since=NOW - 180 * 60, minutes_waiting_for_ci=180)
        closed = ticket("A-6", "In QA", 180, pull_request={"number": 14, "state": "CLOSED"}, ci_state="pending",
                        ci_since=NOW - 180 * 60, minutes_waiting_for_ci=180)
        self.assertEqual(self.kinds(state([waiting, no_pr, passed, within_ci_limit, no_checks, closed])),
                         ["ci:A-1", "ci:A-5", "stuck:A-2", "stuck:A-3", "stuck:A-6"])

    def test_rework_incident_survives_a_rework_block_and_is_not_a_duplicate_blocked_incident(self):
        blocked = ticket("A-1", "Blocked", 90, blocked_kind="Rework", rework_rounds=4)
        self.assertEqual(self.kinds(state([blocked])), ["rework:A-1"])

    def test_rework_block_below_the_incident_limit_uses_the_blocked_threshold(self):
        blocked = ticket("A-1", "Blocked", 90, blocked_kind="Rework", rework_rounds=3)
        self.assertEqual(self.kinds(state([blocked])), ["blocked:A-1"])

    def test_facts_include_ticket_times_comments_pull_request_and_ci_state(self):
        comments = [{"author": "Owner", "created": "2026-01-01T00:00:00Z", "body": "waiting for CI"}]
        item = ticket("A-1", "In QA", 150, pull_request={"number": 12}, ci_state="pending",
                      ci_since=NOW - 150 * 60, minutes_waiting_for_ci=150, last_comments=comments)
        incident = find_incidents(state([item]))[0]
        self.assertEqual(incident.id, "ci:A-1")
        self.assertEqual(incident.facts["ticket"], "A-1")
        self.assertEqual(incident.facts["status"], "In QA")
        self.assertEqual(incident.facts["times"]["status_since"], NOW - 150 * 60)
        self.assertEqual(incident.facts["last_comments"], comments)
        self.assertEqual(incident.facts["pull_request"], {"number": 12})
        self.assertEqual(incident.facts["ci_state"], "pending")


class ParseTests(unittest.TestCase):
    def test_result_actions_options(self):
        text = ('thinking\nACTION: comment {"key": "A-1", "text": "ok"}\nACTION: pause\nACTION: bad {not json}\n'
                'OPTIONS: yes; no\nRECOMMEND: no\nRESULT: ESCALATE: delete the database?')
        result, actions, options, recommend = parse_output(text)
        self.assertEqual(result, ("ESCALATE", "delete the database?"))
        self.assertEqual([a[0] for a in actions], ["comment", "pause", "bad"])
        self.assertEqual((options, recommend), ("yes; no", "no"))
        self.assertIn("_invalid", actions[2][1])

    def test_no_result(self):
        self.assertIsNone(parse_output("nothing")[0])

    def test_result_must_be_the_only_result_and_the_final_nonempty_line(self):
        self.assertIsNone(parse_output("RESULT: RESOLVED: done\nextra text")[0])
        self.assertIsNone(parse_output("RESULT: NO-ACTION: done\nRESULT: RESOLVED: done")[0])
        self.assertIsNone(parse_output("RESULT: ESCALATE:")[0])


class JiraWithLabels(FakeJira):
    def __init__(self):
        super().__init__()
        self.added = []

    def add_label(self, key, label):
        self.added.append((key, label))

    def comments(self, key, last=6):
        return []


class Notifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class OperatorTests(unittest.TestCase):
    def build(self, output="RESULT: NO-ACTION: fine", policy=None, notifier=None):
        redis = FakeRedis()
        sup = make(redis=redis)
        sup.jira = JiraWithLabels()
        sup.usable_ais = lambda: ["Claude", "Codex"]
        self.calls = []

        def run_ai(agent, prompt, tag):
            self.calls.append((agent, prompt))
            return "ok", output, False
        policy = policy or Policy(enabled=True)
        op = Operator(sup, policy, notifier or Notifier(), run_ai)
        return op, sup, redis

    def incident(self, id="blocked:JAIF-1", ticket="JAIF-1"):
        from aiflow.incidents import Incident
        return Incident(id, id.split(":")[0], ticket, f"{ticket} is Blocked", {"ticket": ticket})

    def test_state_snapshot_carries_status_comments_pull_request_and_ci_facts(self):
        op, sup, redis = self.build()
        now = time.time()
        sup.jira.found = [{"key": "JAIF-1", "status": "In QA", "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3 * 3600)),
                           "labels": [], "summary": "A ticket"}]
        sup.jira.comments = lambda key, last=6: [{"author": "Owner", "created": "2026-01-01T00:00:00Z", "body": "Waiting"}]
        sup.online_pool = lambda: ["Claude"]
        sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
        redis.set("statussince:JAIF-1", str(now - 3 * 3600), 60)
        redis.set("pr:JAIF-1", "17", 60)
        redis.set("gateentry:JAIF-1", str(now - 3 * 3600), 60)
        redis.set("pushed:JAIF-1", str(now - 3 * 3600), 60)
        redis.set("cycles:JAIF-1", "0", 60)
        pr = {"number": 17, "url": "https://github.com/jooservices/repo/pull/17", "state": "OPEN",
              "headRefName": "JAIF-1-branch", "statusCheckRollup": [{"__typename": "StatusContext", "context": "CI", "state": "PENDING"}]}

        with mock.patch("aiflow.operator.Repo.github_slug", return_value="jooservices/repo"), \
                mock.patch("aiflow.operator.GitHub.view", return_value=pr):
            snapshot = op.state()

        found = snapshot["tickets"][0]
        self.assertEqual(found["status_since"], now - 3 * 3600)
        self.assertEqual(found["last_comments"][0]["body"], "Waiting")
        self.assertEqual(found["pull_request"]["url"], pr["url"])
        self.assertEqual(found["ci_state"], "pending")
        self.assertEqual(snapshot["thresholds"]["ci_wait_minutes"], op.policy.ci_wait_minutes)

    def test_github_timeout_does_not_stop_the_operator_cycle(self):
        op, sup, redis = self.build()
        now = time.time()
        sup.jira.found = [{"key": "JAIF-1", "status": "In QA",
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3 * 3600)),
                           "labels": [], "summary": "A ticket"}]
        sup.online_pool = lambda: ["Claude"]
        sup.eligible_agents = lambda role, key, field_agent, pool: list(pool)
        for key, value in (("statussince:JAIF-1", now - 3 * 3600), ("pr:JAIF-1", 17),
                           ("gateentry:JAIF-1", now - 3 * 3600), ("pushed:JAIF-1", now - 3 * 3600)):
            redis.set(key, str(value), 60)
        for slot in op.cfg.expected_slots:
            redis.set(f"hb:{slot}", str(int(now)), 60)

        with mock.patch("aiflow.operator.Repo.github_slug", return_value="jooservices/repo"), \
                mock.patch("aiflow.operator.GitHub.view", side_effect=subprocess.TimeoutExpired("gh pr view", 120)), \
                mock.patch.object(op, "sync_queue", wraps=op.sync_queue) as sync_queue, \
                mock.patch.object(op, "flush_notifications") as flush_notifications, \
                mock.patch.object(op, "run_next", return_value=True) as run_next:
            self.assertTrue(op.cycle())

        self.assertEqual([item.id for item in sync_queue.call_args.args[0]], ["stuck:JAIF-1"])
        self.assertEqual(sync_queue.call_args.args[0][0].facts["ci_state"], "unknown")
        self.assertTrue(redis.list_values("operator:queue"))
        flush_notifications.assert_called_once_with()
        run_next.assert_called_once_with()

    def test_resolved_runs_the_whitelisted_actions_and_audits(self):
        out = ('ACTION: comment {"key": "JAIF-1", "text": "answer"}\nACTION: transition {"key": "JAIF-1", "status": "To Do"}\n'
               'ACTION: label {"key": "JAIF-1", "label": "rework"}\nRESULT: RESOLVED: answered and unblocked')
        op, sup, _ = self.build(out)
        self.assertTrue(op.run_incident(self.incident(), "20260101"))
        jira = sup.jira
        self.assertIn(("JAIF-1", "To Do"), jira.moves)
        self.assertIn(("JAIF-1", "rework"), jira.added)
        audit = [body for key, body in jira.comments if key == "JAIF-1" and body.startswith("AI Flow operator (Claude)")]
        self.assertEqual(len(audit), 1)
        self.assertIn("RESOLVED: answered and unblocked", audit[0])

    def test_unknown_or_unsafe_actions_are_refused(self):
        out = ('ACTION: shell {"cmd": "rm -rf /"}\nACTION: transition {"key": "JAIF-1", "status": "Done"}\nACTION: label {"key": "JAIF-1", "label": "x"}\n'
               'ACTION: comment {"key": "OTHER-1", "text": "x"}\nACTION: comment {"key": "JAIF-1; rm", "text": "x"}\nRESULT: RESOLVED: tried')
        op, sup, _ = self.build(out)
        op.run_incident(self.incident(), "20260101")
        self.assertEqual(sup.jira.moves, [])
        self.assertEqual(sup.jira.added, [])
        audit = next(body for key, body in sup.jira.comments if body.startswith("AI Flow operator (Claude)"))
        self.assertEqual(audit.count("Refused:"), 1)
        self.assertIn("Did: nothing", audit)

    def test_actions_are_capped(self):
        out = "\n".join('ACTION: comment {"key": "JAIF-1", "text": "n%d"}' % n for n in range(9)) + "\nRESULT: RESOLVED: x"
        op, sup, _ = self.build(out)
        op.run_incident(self.incident(), "20260101")
        self.assertEqual(sum(1 for _, body in sup.jira.comments if body.startswith("AI Flow operator: n")), 5)

    def test_pause_restart_replace_use_redis_flags(self):
        op, sup, redis = self.build('ACTION: pause {}\nACTION: restart_worker {}\nACTION: replace_ai {"agent": "Codex"}\nRESULT: RESOLVED: ok')
        op.run_incident(self.incident("worker:wsl:1", "wsl:1"), "20260101")
        self.assertIsNotNone(redis.get("paused"))
        self.assertIsNotNone(redis.get("restart"))
        self.assertIsNotNone(redis.get("cooldown:Codex"))

    def test_escalate_labels_comments_the_question_first_and_notifies_once(self):
        notifier = Notifier()
        op, sup, redis = self.build("OPTIONS: delete; keep\nRECOMMEND: keep\nRESULT: ESCALATE: Delete the old database?", notifier=notifier)
        op.cfg.notify["JIRA_URL"] = "https://example.atlassian.net"
        op.run_incident(self.incident(), "20260101")
        self.assertIn(("JAIF-1", "needs-owner"), sup.jira.added)
        question = next(body for key, body in sup.jira.comments if body.startswith("Question for the owner"))
        self.assertIn("Options: delete; keep", question)
        self.assertIn("Recommended: keep", question)
        op.flush_notifications()
        op.flush_notifications()
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("https://example.atlassian.net/browse/JAIF-1", notifier.sent[0])
        self.assertNotIn("password", notifier.sent[0].lower())

    def test_notifications_are_grouped_and_rate_limited(self):
        notifier = Notifier()
        op, sup, redis = self.build(notifier=notifier)
        op.escalate(self.incident("blocked:JAIF-1", "JAIF-1"), "q1", "", "")
        op.escalate(self.incident("blocked:JAIF-2", "JAIF-2"), "q2", "", "")
        op.flush_notifications()
        self.assertEqual(len(notifier.sent), 1)
        self.assertIn("q1", notifier.sent[0])
        self.assertIn("q2", notifier.sent[0])
        op.escalate(self.incident("blocked:JAIF-3", "JAIF-3"), "q3", "", "")
        op.flush_notifications()
        self.assertEqual(len(notifier.sent), 1)  # the interval has not passed

    def test_no_channel_sends_nothing(self):
        op, sup, redis = self.build()
        op.notifier = None
        op.escalate(self.incident(), "q", "", "")
        op.flush_notifications()
        self.assertEqual(redis.scan("operator:notify:*"), [])

    def test_missing_result_is_audited_not_hidden(self):
        op, sup, _ = self.build('ACTION: comment {"key": "JAIF-1", "text": "unsafe without a final result"}\n'
                                'RESULT: RESOLVED: done\nAdditional commentary')
        op.run_incident(self.incident(), "20260101")
        audit = next(body for key, body in sup.jira.comments if body.startswith("AI Flow operator (Claude)"))
        self.assertIn("no usable RESULT line", audit)
        self.assertIn("Refused:", audit)
        self.assertFalse(any(body.startswith("AI Flow operator: unsafe") for _, body in sup.jira.comments))

    def test_no_action_result_refuses_requested_actions(self):
        op, sup, _ = self.build('ACTION: comment {"key": "JAIF-1", "text": "should not run"}\nRESULT: NO-ACTION: nothing is needed')
        with mock.patch.object(op, "perform", wraps=op.perform) as perform:
            op.run_incident(self.incident(), "20260101")
        perform.assert_not_called()
        self.assertFalse(any(body.startswith("AI Flow operator: should not run") for _, body in sup.jira.comments))
        audit = next(body for _, body in sup.jira.comments if body.startswith("AI Flow operator (Claude)"))
        self.assertIn("Outcome: NO-ACTION: nothing is needed", audit)
        self.assertIn("Refused:", audit)

    def test_quota_run_is_audited_and_does_not_perform_actions(self):
        op, sup, redis = self.build('ACTION: pause {}')
        op.run_ai = lambda agent, prompt, tag: ("quota", 'ACTION: pause {}', True)
        with mock.patch.object(op, "perform", wraps=op.perform) as perform:
            op.run_incident(self.incident(), "20260101")
        perform.assert_not_called()
        self.assertIsNotNone(redis.get("cooldown:Claude"))
        self.assertIsNone(redis.get("operator:cool:blocked:JAIF-1"))
        self.assertIsNone(redis.get("paused"))
        self.assertEqual(len(sup.jira.comments), 1)
        audit = sup.jira.comments[0][1]
        self.assertIn("Outcome: AI run ended without a usable RESULT (quota)", audit)
        self.assertIn("Did: nothing", audit)
        self.assertIn("Refused: pause {}", audit)

    def test_prompt_requires_a_final_result_line_and_contains_incident_policy_and_actions(self):
        op, _, _ = self.build()
        prompt = op.prompt(self.incident())
        self.assertIn("blocked:JAIF-1: JAIF-1 is Blocked", prompt)
        self.assertIn("## Policy", prompt)
        self.assertIn('ACTION: retry_ci {"key": "JAIF-1"}', prompt)
        self.assertIn("Finish with exactly one final line", prompt)
        self.assertIn("OPTIONS:", prompt)
        self.assertIn("RECOMMEND:", prompt)
        self.assertLess(prompt.index("OPTIONS:"), prompt.index("RESULT: ESCALATE:"))

    def test_default_claude_falls_back_to_another_usable_ai(self):
        op, sup, _ = self.build()
        self.assertEqual(op.choose_agent(), "Claude")
        with mock.patch.object(sup, "cooling", side_effect=lambda name: name == "Claude"):
            self.assertEqual(op.choose_agent(), "Codex")

    def test_ai_process_has_no_github_login(self):
        from aiflow import agents

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_with_github_login = {
                "PATH": "/bin",
                "GH_TOKEN": "test-token",
                "GITHUB_TOKEN": "test-token",
                "GH_ENTERPRISE_TOKEN": "test-token",
                "GITHUB_ENTERPRISE_TOKEN": "test-token",
            }
            with mock.patch("aiflow.agents.child_env", return_value=env_with_github_login):
                child = agents.run_env("Claude", root, root)

        self.assertTrue(all(name not in child for name in env_with_github_login if name.endswith("TOKEN")))
        self.assertTrue(child["GH_CONFIG_DIR"].endswith("/.tmp/aiflow/gh-empty"))

    def test_machine_incident_is_audited_on_the_health_ticket(self):
        output = "RESULT: NO-ACTION: seems fine"
        result, actions, _, _ = parse_output(output)
        self.assertEqual(result, ("NO-ACTION", "seems fine"))
        self.assertEqual(actions, [])

        op, sup, _ = self.build(output)
        op.cfg = sup.cfg = sup.cfg.__class__(**{**sup.cfg.__dict__, "health_ticket": "JAIF-12"})
        with mock.patch.object(op, "perform", wraps=op.perform) as perform:
            op.run_incident(self.incident("worker:wsl:2", "wsl:2"), "20260101")

        perform.assert_not_called()
        self.assertEqual(len(sup.jira.comments), 1)
        key, audit = sup.jira.comments[0]
        self.assertEqual(key, "JAIF-12")
        self.assertIn("Outcome: NO-ACTION: seems fine", audit)
        self.assertIn("Did: nothing", audit)
        self.assertEqual(sup.jira.added, [])
        self.assertEqual(sup.jira.moves, [])

    def test_per_ticket_limit_escalates_without_running_the_ai(self):
        op, sup, redis = self.build(policy=Policy(enabled=True, retries_per_ticket_per_day=1))
        day = time.strftime("%Y%m%d", time.gmtime())
        redis.set(f"operator:ticket:JAIF-1:{day}", "1", 10)
        self.assertTrue(op.handle(self.incident()))
        self.assertEqual(self.calls, [])
        self.assertIn(("JAIF-1", "needs-owner"), sup.jira.added)

    def test_hourly_limit_stops_runs(self):
        op, sup, redis = self.build(policy=Policy(enabled=True, max_runs_per_hour=1))
        op.sync_queue([self.incident("blocked:JAIF-1", "JAIF-1"), self.incident("blocked:JAIF-2", "JAIF-2")])
        self.assertTrue(op.run_next())
        self.assertFalse(op.run_next())
        self.assertEqual(len(self.calls), 1)

    def test_switches_stop_the_operator(self):
        op, sup, redis = self.build()
        self.assertTrue(op.active())
        redis.set("paused", "1", 10)
        self.assertFalse(op.active())
        redis.delete("paused")
        redis.set("operator:off", "1", 10)
        self.assertFalse(op.active())
        redis.delete("operator:off")
        op.policy = Policy(enabled=False)
        self.assertFalse(op.active())
        self.assertFalse(op.cycle())

    def test_resolved_incidents_leave_the_queue_and_new_ones_enter_once(self):
        op, sup, redis = self.build()
        first, second = self.incident("blocked:JAIF-1", "JAIF-1"), self.incident("blocked:JAIF-2", "JAIF-2")
        op.sync_queue([first, second])
        op.sync_queue([first, second])
        self.assertEqual(len(redis.list_values("operator:queue")), 2)
        op.sync_queue([second])
        queued = [json.loads(value) for value in redis.list_values("operator:queue")]
        self.assertEqual([item["id"] for item in queued], ["blocked:JAIF-2"])

    def test_queue_deduplicates_by_stable_id_and_stores_facts(self):
        op, sup, redis = self.build()
        incident = self.incident()
        incident.facts.update({"ticket": "JAIF-1", "status": "Blocked", "times": {"minutes_in_status": 45},
                               "last_comments": [{"body": "Still blocked"}],
                               "pull_request": {"number": 4, "url": "https://example/pr/4"}, "ci_state": "pending"})

        op.sync_queue([incident, incident])

        queued = [json.loads(value) for value in redis.list_values("operator:queue")]
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["id"], "blocked:JAIF-1")
        self.assertEqual(queued[0]["facts"]["last_comments"][0]["body"], "Still blocked")
        self.assertEqual(queued[0]["facts"]["pull_request"]["number"], 4)
        self.assertEqual(queued[0]["facts"]["ci_state"], "pending")

    def test_the_read_only_ai_command_has_no_edit_or_shell_tools(self):
        from aiflow import agents
        argv, _ = agents.build_command("Claude", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(allowed, "Read,Glob,Grep")
        codex, _ = agents.build_command("Codex", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
        self.assertEqual(codex[codex.index("-s") + 1], "read-only")
        with self.assertRaises(ValueError):
            agents.build_command("OpenCode", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
