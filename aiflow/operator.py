"""The operator (JAIF-70): the supervisor finds incidents with plain code (no AI, no tokens) and starts an AI only for one incident
at a time. The AI can read; it asks for actions as ACTION lines, and the supervisor performs only a fixed set of them.

    detect -> queue (Redis, one entry per incident id) -> one operator run -> audit comment -> RESOLVED | ESCALATE | NO-ACTION

Limits: per ticket per day, per hour and per day in total; `aiflow-work pause` and `aiflow-work operator off` stop it."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import agents, events, notify, runner, usage
from .config import PROJECT_DIR
from .incidents import WORKED, Incident, find_incidents
from .jira import JiraError

DAY = 86400
MAX_ACTIONS = 5
TRANSITIONS = ("To Do", "Backlog", "Blocked")
LABELS = ("needs-owner", "rework")
ACTION_NAMES = ("comment", "transition", "label", "retry_ci", "replace_ai", "restart_worker", "pause", "resume")
RESULT = re.compile(r"^RESULT:\s*(RESOLVED|ESCALATE|NO-ACTION)\b[:\s-]*(.*)$", re.M)
ACTION = re.compile(r"^ACTION:\s*(\w+)\s*(\{.*\})?\s*$", re.M)
OPTIONS = re.compile(r"^OPTIONS:\s*(.+)$", re.M)
RECOMMEND = re.compile(r"^RECOMMEND:\s*(.+)$", re.M)
TICKET = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def parse_output(text: str) -> tuple[tuple[str, str] | None, list[tuple[str, dict[str, Any]]], str, str]:
    """(last RESULT as (kind, text), ACTION lines as (name, args), OPTIONS, RECOMMEND)."""
    results = RESULT.findall(text)
    result = (results[-1][0], results[-1][1].strip()) if results else None
    actions: list[tuple[str, dict[str, Any]]] = []
    for name, raw in ACTION.findall(text):
        try:
            args = json.loads(raw) if raw else {}
        except ValueError:
            args = {"_invalid": raw}
        actions.append((name, args if isinstance(args, dict) else {"_invalid": raw}))
    options, recommend = OPTIONS.findall(text), RECOMMEND.findall(text)
    return result, actions, options[-1].strip() if options else "", recommend[-1].strip() if recommend else ""


def epoch(value: str, default: float) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return default


class Operator:
    def __init__(self, sup: Any, policy: Any, notifier: notify.Notifier | None = None,
                 run_ai: Callable[[str, str, str], tuple[str, str, bool]] | None = None) -> None:
        self.sup, self.policy = sup, policy
        self.notifier = notifier if notifier is not None else notify.from_env(sup.cfg.notify)
        self.run_ai = run_ai or self._run_ai
        self.redis, self.jira, self.cfg = sup.redis, sup.jira, sup.cfg

    # ---- switches --------------------------------------------------------------------------------------------
    def active(self) -> bool:
        """On when the policy enables it, the flow is not paused and nobody ran `aiflow-work operator off`."""
        return bool(self.policy.enabled) and not self.sup.paused() and self.redis.get("operator:off") is None

    # ---- detection ---------------------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        now = time.time()
        projects = ", ".join(self.cfg.projects)
        statuses = ", ".join(f'"{s}"' for s in ("Blocked", *WORKED))
        issues = self.jira.search(f"project in ({projects}) AND status in ({statuses}) AND issuetype != Epic ORDER BY created ASC", 200)
        parents = self.sup.parents_of_open_work(issues)
        issues = [i for i in issues if i["key"] not in parents]
        leased = {name.split(":", 1)[1] for name in self.redis.scan("lease:*")}
        blocked = self.redis.get_many([f"blocked:{i['key']}" for i in issues]) if issues else []
        cycles = self.redis.get_many([f"cycles:{i['key']}" for i in issues]) if issues else []
        tickets = []
        for issue, why, rounds in zip(issues, blocked, cycles):
            tickets.append(dict(key=issue["key"], status=issue["status"], updated=epoch(issue.get("updated", ""), now), labels=issue.get("labels", []),
                                leased=issue["key"] in leased, blocked_kind=(json.loads(why).get("kind", "") if why else ""),
                                reason=issue.get("summary", ""), cycles=int(rounds or 0)))
        beats = {slot: (int(v) if (v := self.redis.get(f"hb:{slot}")) else None) for slot in self.cfg.expected_slots}
        return dict(now=now, tickets=tickets, heartbeats=beats, stale_seconds=self.cfg.heartbeat_stale_seconds, pool=self.sup.online_pool())

    def sync_queue(self, incidents: list[Incident]) -> None:
        """Queue each new incident once; forget the ones that no longer exist."""
        current = {incident.id for incident in incidents}
        for name in self.redis.scan("operator:queue:*"):
            if name.split("operator:queue:", 1)[1] not in current:
                events.emit("operator.resolved", incident=name.split("operator:queue:", 1)[1])
                self.redis.delete(name)
        for incident in incidents:
            name = f"operator:queue:{incident.id}"
            if self.redis.get(name) is None:
                events.emit("operator.incident", incident=incident.id, kind=incident.kind, subject=incident.subject, summary=incident.summary[:300])
                self.redis.set(name, json.dumps(dict(kind=incident.kind, subject=incident.subject, summary=incident.summary, facts=incident.facts)), 7 * DAY)

    # ---- one cycle ---------------------------------------------------------------------------------------------
    def cycle(self) -> bool:
        """Called by every worker loop; True when an operator run happened."""
        if not self.active() or not self.redis.acquire("operator:cycle", self.cfg.owner, 60):
            return False
        self.sync_queue(find_incidents(self.state(), self.policy))
        self.flush_notifications()
        return self.run_next()

    def count(self, name: str) -> int:
        return int(self.redis.get(name) or 0)

    def limits_reached(self) -> bool:
        now = datetime.now(timezone.utc)
        return (self.count(f"operator:runs:h:{now:%Y%m%d%H}") >= self.policy.max_runs_per_hour
                or self.count(f"operator:runs:d:{now:%Y%m%d}") >= self.policy.max_runs_per_day)

    def run_next(self) -> bool:
        if self.limits_reached():
            return False
        for name in sorted(self.redis.scan("operator:queue:*")):
            incident_id = name.split("operator:queue:", 1)[1]
            if self.redis.get(f"operator:cool:{incident_id}") is not None:
                continue
            raw = json.loads(self.redis.get(name) or "{}")
            if not raw:
                continue
            incident = Incident(incident_id, raw["kind"], raw["subject"], raw["summary"], raw.get("facts", {}))
            return self.handle(incident)
        return False

    def handle(self, incident: Incident) -> bool:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if incident.ticket and self.count(f"operator:ticket:{incident.ticket}:{day}") >= self.policy.retries_per_ticket_per_day:
            events.emit("operator.limit", incident=incident.id, limit="retries_per_ticket_per_day")
            self.escalate(incident, f"The operator already tried {incident.ticket} {self.policy.retries_per_ticket_per_day} time(s) today and the problem is still there.",
                          "", "")
            self.redis.set(f"operator:cool:{incident.id}", "1", DAY)
            return True
        if not self.redis.acquire("operator:run", self.cfg.owner, self.policy.run_minutes * 60 + 120):
            return False
        try:
            return self.run_incident(incident, day)
        finally:
            self.redis.release("operator:run", self.cfg.owner)

    # ---- one operator run ----------------------------------------------------------------------------------------
    def choose_agent(self) -> str | None:
        usable = [a for a in self.sup.usable_ais() if a in ("Claude", "Codex", "Grok") and not self.sup.cooling(a)]
        if self.policy.agent in usable:
            return self.policy.agent
        return usable[0] if usable else None

    def prompt(self, incident: Incident) -> str:
        comments = ""
        if incident.ticket:
            try:
                comments = "\n".join(f"- {c.get('author', '?')} {c.get('created', '')}: {str(c.get('body', ''))[:1500]}" for c in self.jira.comments(incident.ticket, 6))
            except (JiraError, ValueError, TypeError):
                comments = "(unavailable)"
        values = dict(machine=self.cfg.machine, incident=f"{incident.id}: {incident.summary}", facts=json.dumps(incident.facts, indent=1, default=str),
                      comments=comments or "(none)", escalate="; ".join(self.policy.escalate_always), root=self.cfg.root,
                      ticket=incident.ticket or "<ticket>", max_actions=MAX_ACTIONS)
        text = (PROJECT_DIR / "prompts" / "operator.md").read_text(encoding="utf-8")
        for name, value in values.items():
            text = text.replace("{" + name + "}", str(value))
        return text

    def _run_ai(self, agent: str, prompt: str, tag: str) -> tuple[str, str, bool]:
        """Run one AI read-only, without a GitHub login. Returns (why, final text, quota)."""
        workdir = self.cfg.root / ".tmp" / "aiflow" / "operator"
        workdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_file, result_file = self.cfg.root / ".tmp" / "aiflow" / "logs" / f"operator-{tag}-{stamp}.log", self.cfg.root / ".tmp" / "aiflow" / "logs" / f"operator-{tag}-{stamp}.result"
        argv, stdin_text = agents.build_command(agent, workdir, result_file, prompt, self.cfg.root, read_only=True)

        def beat() -> bool:
            return self.redis.renew("operator:run", self.cfg.owner, self.policy.run_minutes * 60 + 120) and self.active()
        code, why = runner.run(argv, stdin_text, workdir, log_file, agents.run_env(agent, workdir, self.cfg.root), idle_seconds=self.cfg.idle_minutes * 60,
                               max_seconds=self.policy.run_minutes * 60, heartbeat_seconds=self.cfg.heartbeat_seconds, heartbeat=beat)
        usage.report(agent, log_file, ticket=tag.split("-", 2)[-1] if False else "", role="operator", incident=tag, why=why, code=code)
        text = agents.final_text(agent, log_file, result_file)
        quota = (code != 0 or not RESULT.search(text)) and agents.looks_like_quota(log_file, text, code)
        return why, text, quota

    def run_incident(self, incident: Incident, day: str) -> bool:
        agent = self.choose_agent()
        if agent is None:
            return False
        now = datetime.now(timezone.utc)
        self.redis.incr(f"operator:runs:h:{now:%Y%m%d%H}", 2 * 3600)
        self.redis.incr(f"operator:runs:d:{now:%Y%m%d}", 2 * DAY)
        if incident.ticket:
            self.redis.incr(f"operator:ticket:{incident.ticket}:{day}", 2 * DAY)
        self.redis.set(f"operator:cool:{incident.id}", "1", 3600)
        began = time.monotonic()
        why, text, quota = self.run_ai(agent, self.prompt(incident), incident.id.replace(":", "-"))
        self.sup.log_usage(incident.ticket or "operator", "operator", agent, (time.monotonic() - began) / 60, why, 0 if why == "ok" else None)
        if quota:
            self.redis.set(f"cooldown:{agent}", "1", self.cfg.quota_cooldown_minutes * 60)
            self.redis.delete(f"operator:cool:{incident.id}")
            return True
        result, actions, options, recommend = parse_output(text)
        events.emit("operator.run", incident=incident.id, agent=agent, why=why, minutes=round((time.monotonic() - began) / 60, 2),
                    result=result[0] if result else "NONE", actions=len(actions))
        done, refused = [], []
        for name, args in actions[:MAX_ACTIONS]:
            (done if self.perform(name, args) else refused).append(f"{name} {json.dumps(args, sort_keys=True)[:200]}")
        if len(actions) > MAX_ACTIONS:
            refused.append(f"{len(actions) - MAX_ACTIONS} action(s) over the limit of {MAX_ACTIONS}")
        if result is None:
            self.audit(incident, agent, "no usable RESULT line (" + why + ")", done, refused)
            return True
        kind, detail = result
        if kind == "ESCALATE":
            self.escalate(incident, detail, options, recommend)
        self.audit(incident, agent, f"{kind}: {detail}", done, refused)
        return True

    # ---- actions (the fixed set) ----------------------------------------------------------------------------------
    def valid_key(self, key: Any) -> bool:
        return isinstance(key, str) and bool(TICKET.match(key)) and key.rsplit("-", 1)[0] in self.cfg.projects

    def perform(self, name: str, args: dict[str, Any]) -> bool:
        """Perform one requested action. False when it is not allowed or failed; nothing outside this set can run."""
        events.emit("operator.action", action=name, args=json.dumps(args, sort_keys=True)[:300])
        try:
            if name == "comment" and self.valid_key(args.get("key")) and isinstance(args.get("text"), str) and args["text"].strip():
                self.jira.comment(args["key"], "AI Flow operator: " + args["text"].strip()[:3000])
            elif name == "transition" and self.valid_key(args.get("key")) and args.get("status") in TRANSITIONS:
                if not self.jira.transition(args["key"], args["status"]):
                    return False
                if args["status"] == "To Do":
                    self.sup.clear_block(args["key"])
            elif name == "label" and self.valid_key(args.get("key")) and args.get("label") in LABELS:
                self.jira.add_label(args["key"], args["label"])
            elif name == "retry_ci" and self.valid_key(args.get("key")):
                return self.retry_ci(args["key"])
            elif name == "replace_ai" and args.get("agent") in agents.NAMES:
                self.redis.set(f"cooldown:{args['agent']}", "1", self.cfg.quota_cooldown_minutes * 60)
            elif name == "restart_worker":
                self.redis.set("restart", str(int(time.time())), 7 * DAY)
            elif name == "pause":
                self.redis.set("paused", str(int(time.time())), 365 * DAY)
            elif name == "resume":
                self.redis.delete("paused")
            else:
                return False
        except Exception as error:  # a failed action is reported in the audit comment, never raised into the worker loop
            self.sup_log(f"operator action {name} failed: {type(error).__name__}: {error}")
            return False
        return True

    def retry_ci(self, key: str) -> bool:
        from .gitops import Repo
        project = self.cfg.project(key)
        found = self.sup.pr_of(key, Repo(self.cfg.root, project.repo, project.company))
        if not found:
            return False
        gh, branch, _ = found
        return gh.rerun_failed(branch)

    def sup_log(self, message: str) -> None:
        from .supervisor import log
        log(message)

    # ---- audit and escalation --------------------------------------------------------------------------------------
    def audit(self, incident: Incident, agent: str, outcome: str, done: list[str], refused: list[str]) -> None:
        lines = [f"AI Flow operator ({agent}) on {self.cfg.machine} handled `{incident.id}`.", f"Saw: {incident.summary}", f"Outcome: {outcome}"]
        lines.append("Did: " + ("; ".join(done) if done else "nothing"))
        if refused:
            lines.append("Refused: " + "; ".join(refused))
        target = incident.ticket or self.cfg.health_ticket
        self.sup_log("operator " + " | ".join(lines))
        if target:
            self.jira.comment(target, "\n".join(lines))

    def escalate(self, incident: Incident, question: str, options: str, recommend: str) -> None:
        """Label needs-owner and ask in a comment that starts with the question; the ticket stays as it is (Blocked)."""
        text = f"Question for the owner: {question}"
        if options:
            text += f"\nOptions: {options}"
        if recommend:
            text += f"\nRecommended: {recommend}"
        text += "\nAnswer in a comment and move the ticket to To Do; the label needs-owner is removed when you do." if incident.ticket else ""
        target = incident.ticket or self.cfg.health_ticket
        if incident.ticket:
            self.jira.add_label(incident.ticket, "needs-owner")
        if target:
            self.jira.comment(target, text)
        events.emit("operator.escalate", incident=incident.id, ticket=target, question=question[:500], options=options[:300], recommend=recommend[:200])
        self.redis.set(f"operator:notify:{incident.id}", json.dumps(dict(ticket=target, question=question)), 7 * DAY)

    def flush_notifications(self) -> None:
        """One grouped message per interval, the question and the ticket link only. No channel configured: nothing is sent."""
        names = self.redis.scan("operator:notify:*")
        if not names:
            return
        if self.notifier is None:
            for name in names:
                self.redis.delete(name)
            return
        if not self.redis.acquire("operator:notify-gate", self.cfg.owner, self.policy.notify_interval_minutes * 60):
            return
        base = self.cfg.notify.get("JIRA_URL", "").rstrip("/")
        lines = []
        for name in sorted(names):
            item = json.loads(self.redis.get(name) or "{}")
            if item:
                lines.append(f"- {item['question']}" + (f"\n  {base}/browse/{item['ticket']}" if base and item.get("ticket") else ""))
        if lines:
            events.emit("operator.notify", questions=len(lines))
            try:
                self.notifier.send("AI Flow needs the owner:\n" + "\n".join(lines))
            except Exception as error:
                self.sup_log(f"operator notification failed: {type(error).__name__}")
                self.redis.delete("operator:notify-gate")
                return
        for name in names:
            self.redis.delete(name)
