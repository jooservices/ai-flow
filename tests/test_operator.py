import json
import tempfile
import time
import unittest
from pathlib import Path

from aiflow.config import ConfigError
from aiflow.incidents import find_incidents
from aiflow.operator import Operator, parse_output
from aiflow.operator_policy import Policy, load as load_policy
from tests.test_supervisor import FakeJira, FakeRedis, make

NOW = 1_000_000.0


def ticket(key, status, minutes, **extra):
    return dict(key=key, status=status, updated=NOW - minutes * 60, labels=[], leased=False, blocked_kind="", reason="", cycles=0, **extra)


def state(tickets=(), heartbeats=None, pool=("Claude",)):
    return dict(now=NOW, tickets=list(tickets), heartbeats=heartbeats or {}, stale_seconds=300, pool=list(pool))


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
        return sorted(i.id for i in find_incidents(st, self.policy))

    def test_each_kind_is_found_once_with_a_stable_id(self):
        st = state([ticket("A-1", "Blocked", 45, ), ticket("A-2", "In Progress", 200), ticket("A-3", "In QA", 130), dict(ticket("A-4", "In Review", 5), cycles=4)],
                   heartbeats={"wsl:1": NOW - 10, "wsl:2": None}, pool=())
        self.assertEqual(self.kinds(st), ["ai_pool:all", "blocked:A-1", "ci:A-3", "rework:A-4", "stuck:A-2", "worker:wsl:2"])
        self.assertEqual(self.kinds(st), self.kinds(st))

    def test_nothing_is_an_incident_when_all_is_well(self):
        st = state([ticket("A-1", "Blocked", 10), dict(ticket("A-2", "In Progress", 500), leased=True), ticket("A-3", "Human review", 900)],
                   heartbeats={"wsl:1": NOW - 5})
        self.assertEqual(self.kinds(st), [])

    def test_quota_offline_hold_and_escalated_are_left_alone(self):
        blocked = [dict(ticket("A-1", "Blocked", 99), blocked_kind="Quota"), dict(ticket("A-2", "Blocked", 99), blocked_kind="Offline"),
                   dict(ticket("A-3", "Blocked", 99), labels=["aiflow-hold"]), dict(ticket("A-4", "Blocked", 99), labels=["needs-owner"])]
        self.assertEqual(self.kinds(state(blocked)), [])


class ParseTests(unittest.TestCase):
    def test_result_actions_options(self):
        text = ('thinking\nACTION: comment {"key": "A-1", "text": "ok"}\nACTION: pause\nACTION: bad {not json}\n'
                'RESULT: ESCALATE: delete the database?\nOPTIONS: yes; no\nRECOMMEND: no')
        result, actions, options, recommend = parse_output(text)
        self.assertEqual(result, ("ESCALATE", "delete the database?"))
        self.assertEqual([a[0] for a in actions], ["comment", "pause", "bad"])
        self.assertEqual((options, recommend), ("yes; no", "no"))
        self.assertIn("_invalid", actions[2][1])

    def test_no_result(self):
        self.assertIsNone(parse_output("nothing")[0])


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
        op, sup, redis = self.build("RESULT: ESCALATE: Delete the old database?\nOPTIONS: delete; keep\nRECOMMEND: keep", notifier=notifier)
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
        op, sup, _ = self.build("I looked but wrote no result")
        op.run_incident(self.incident(), "20260101")
        audit = next(body for key, body in sup.jira.comments if body.startswith("AI Flow operator (Claude)"))
        self.assertIn("no usable RESULT line", audit)

    def test_machine_incident_is_audited_on_the_health_ticket(self):
        op, sup, _ = self.build("RESULT: NO-ACTION: seems fine")
        op.cfg = sup.cfg = sup.cfg.__class__(**{**sup.cfg.__dict__, "health_ticket": "JAIF-12"})
        op.run_incident(self.incident("worker:wsl:2", "wsl:2"), "20260101")
        self.assertEqual(sup.jira.comments[0][0], "JAIF-12")

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
        self.assertEqual(len(redis.scan("operator:queue:*")), 2)
        op.sync_queue([second])
        self.assertEqual(redis.scan("operator:queue:*"), ["operator:queue:blocked:JAIF-2"])

    def test_the_read_only_ai_command_has_no_edit_or_shell_tools(self):
        from aiflow import agents
        argv, _ = agents.build_command("Claude", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertEqual(allowed, "Read,Glob,Grep")
        codex, _ = agents.build_command("Codex", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
        self.assertEqual(codex[codex.index("-s") + 1], "read-only")
        with self.assertRaises(ValueError):
            agents.build_command("OpenCode", Path("/tmp"), Path("/tmp/r"), "p", Path("/tmp"), read_only=True)
