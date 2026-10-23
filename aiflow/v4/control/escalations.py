"""Only real business decisions reach a person (docs/v4/escalations.md).

Every question a developer AI or the master would put to the owner is classified when it is raised:

  missing_tool_or_environment   the machine lacks a browser, Docker, a Dev VM, a SonarQube, devices, a CI log
  policy_message                a refused command or write that the AI took for a restriction needing a person
  unreachable_criterion         an acceptance criterion that cannot be met in this environment
  ambiguity_repository          a question the ticket, the spec or the code can answer
  decision_already_taken        the owner decided it before (the owner decision register)
  new_business_decision         a real business decision          -> a person
  irreversible_or_production    an irreversible or production action -> a person

Only the last two reach a person. The register (`owner_decisions`) holds what the owner already decided; a
matching entry is applied and cited, a new kind of decision is asked once with options and a recommended default
and then becomes an entry, and an unreachable criterion is amended on the ticket and recorded as an entry too.
Classification is deterministic (rules below) so it works without a model run; the triage AI refines the
`ambiguity_repository` and `new_business_decision` cases.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.text import slug
from . import tooling
from .db import J
from ..core.toolchains import parse_missing_requirements

log = logging.getLogger("aiflow4.escalations")

MISSING = "missing_tool_or_environment"
POLICY = "policy_message"
UNREACHABLE = "unreachable_criterion"
AMBIGUITY = "ambiguity_repository"
TAKEN = "decision_already_taken"
BUSINESS = "new_business_decision"
IRREVERSIBLE = "irreversible_or_production"
CLASSES = (MISSING, POLICY, UNREACHABLE, AMBIGUITY, TAKEN, BUSINESS, IRREVERSIBLE)
PERSON_CLASSES = (BUSINESS, IRREVERSIBLE)
LABELS = {MISSING: "Missing tool or environment", POLICY: "Policy or permission message",
          UNREACHABLE: "Unreachable acceptance criterion", AMBIGUITY: "Ambiguity the repository can answer",
          TAKEN: "Decision already taken", BUSINESS: "New business decision",
          IRREVERSIBLE: "Irreversible or production action"}
# What removes a cause for good; the weekly report names it next to the three biggest causes.
FIXES = {
    MISSING: "Provision the tool on a machine (Machines and AI) or declare it in the project stack; the dev prompt lists "
             "what each machine has, so a request for a tool the machine has is answered automatically.",
    POLICY: "Read the refusal: it names the allowed alternative. Extend the project's command allowlist in the control "
            "repo when a legitimate command keeps being refused.",
    UNREACHABLE: "Write acceptance criteria whose evidence a machine of the fleet can produce; triage already narrows "
                 "the criterion and records the amendment.",
    AMBIGUITY: "Put the missing convention or location in AGENTS.md or the project docs so the AI finds it itself.",
    TAKEN: "Add the decision to the owner register (Command Center) with a pattern that matches how it is asked.",
    BUSINESS: "Answer once in Command Center: the choice becomes a register entry and is applied from then on.",
    IRREVERSIBLE: "Keep releases, tags and production changes with the owner; nothing to fix.",
}
ACTIVE_WINDOW_WEEKS = 8


@dataclass
class Classification:
    cls: str
    reason: str
    decision: dict | None = None          # register entry that was applied (decision_already_taken)
    pending: dict | None = None           # register entry asked and not yet answered
    environment: str = ""                 # browser | ci_log | dev_vm | sonarqube | devices | docker
    criterion: str = ""                   # "AC3"
    requirements: list[str] = field(default_factory=list)

    @property
    def reaches_person(self) -> bool:
        return self.cls in PERSON_CLASSES


# --- rules ------------------------------------------------------------------------------------------------
POLICY_RE = re.compile(
    r"command policy|policy denies|policy denied|denied by (?:the )?(?:policy|hook)|blocked by (?:the )?(?:policy|hook)|"
    r"not allowed by (?:the )?policy|refused (?:by|because|the (?:command|write))|writes? (?:is|are) (?:not allowed|allowed only)|"
    r"write restriction|permission ?denied by|permissionDecision|pretooluse|not in the project's command",
    re.I)
CRITERION_RE = re.compile(r"\bAC\s?(\d+)\b|acceptance criteri\w+|definition of done|\bevidence\b", re.I)
IMPOSSIBLE_RE = re.compile(
    r"\bcannot\b|\bcan't\b|\bcan not\b|\bunable\b|not possible|impossible|\bno (?:such )?(?:machine|environment|vm|device|server)\b|"
    r"does(?: not|n't) exist|unreachable|not reachable|not available|unavailable|\bwithout (?:a|an|the|any)\b|"
    r"requires? (?:a|an|the) (?:machine|vm|device|environment)", re.I)
ENVIRONMENTS = (
    ("browser", re.compile(r"\b(?:browser|chromium|chrome|playwright|puppeteer|selenium|headless)\b", re.I)),
    ("docker", re.compile(r"\b(?:docker|podman)\b", re.I)),
    ("ci_log", re.compile(r"\b(?:ci|github actions?|workflow|pipeline|job|build)\b.{0,40}\blogs?\b|"
                          r"\blogs? (?:of|from|for) (?:the )?(?:failing|failed|ci|job|build|pipeline)", re.I | re.S)),
    ("dev_vm", re.compile(r"\bdev(?:elopment)?[ -]?vm\b|\bstaging (?:vm|server|environment|machine)\b|"
                          r"\bdev(?:elopment)? (?:server|machine|environment)\b", re.I)),
    ("sonarqube", re.compile(r"\bsonar\w*\b", re.I)),
    ("devices", re.compile(r"\b(?:real |physical |mobile |android |ios )?devices?\b|\bsimulators?\b|\bemulators?\b|"
                           r"\biphones?\b|\bipad\b", re.I)),
)
NEED_RE = re.compile(r"\bneed|\brequire|\bmissing|\bnot (?:installed|available|found)|\bno access|\bcannot|\bcan't|\bunable|"
                     r"\bdo(?:es)? not have|\bdon't have|\bwithout|\bunavailable|\black|\bask|\bprovide|\bgrant|\bplease",
                     re.I)
IRREVERSIBLE_RE = re.compile(
    r"\bforce[- ]?push\b|\bdrop (?:the )?(?:table|database|schema)\b|"
    r"\b(?:delete|destroy|wipe|truncate)\b.{0,60}\b(?:data|database|records|repository|repo|branch|bucket)\b|"
    r"\b(?:rotate|revoke)\b.{0,30}\b(?:secret|key|token|credential)s?\b|\birreversible\b|\bcannot be undone\b|"
    r"\b(?:deploy|publish|roll ?out|ship)\b.{0,60}\b(?:to|into|on) (?:the )?(?:production|prod)\b|"
    r"\b(?:production|prod)\b.{0,60}\b(?:deploy|publish|roll ?out|ship)\b|"
    r"\b(?:create|cut|make|publish|push|tag)\b.{0,40}\b(?:release|version tag|tag)\b|"
    r"\brelease\b.{0,40}\b(?:to production|to prod|public|live)\b", re.I | re.S)
AMBIGUITY_RE = re.compile(
    r"\b(?:which|what) (?:file|files|folder|directory|module|class|function|library|framework|package|existing|naming|"
    r"convention|pattern|helper|test|command|script|config|setting)s?\b|"
    r"\bwhere (?:is|are|does|do|should (?:the|this|i put))\b.{0,50}\b(?:defined|implemented|configured|located|live|stored|put|go)\b|"
    r"\bhow (?:does|do|is|are) the existing\b|\bexisting (?:convention|pattern|helper|implementation|code)\b|"
    r"\bnaming convention\b|\bfolder structure\b|\bcode ?style\b", re.I | re.S)
REPOSITORY_FACT_RE = re.compile(
    r"\b(?:which|what)\s+(?:http\s+)?(?:status code|response status|http method|request method|method)\b"
    r".{0,180}(?:/v\d+/|\b(?:endpoint|route|api|handler)\b)|"
    r"\b(?:which|what)\s+(?:http\s+)?(?:request\s+)?method\b.{0,120}"
    r"(?:/\w[\w./{}:-]*|\b(?:endpoint|route|url)\b)|"
    r"\bwhat\s+does\s+(?:(?:get|post|put|patch|delete|head|options)\s+)?`?/v\d+/[^`\s]+`?"
    r".{0,100}\b(?:return|respond|produce|do)\b|"
    r"\bwhat\s+does\b.{0,120}(?:`?/\w[\w./{}:-]*`?|\b(?:endpoint|route|handler|function)\b)"
    r".{0,80}\b(?:return|respond|produce|do)\b|"
    r"\b(?:which|what)\s+(?:status code|response|parameter|header)\b.{0,100}"
    r"\b(?:endpoint|route|api|function|handler)\b|"
    r"\bwhat\s+(?:value|setting)\b.{0,80}\b(?:existing|current|configured)\b.{0,80}\b(?:use|set|configured)\b",
    re.I | re.S)


def _search(pattern: str, text: str) -> bool:
    try:
        return re.search(str(pattern), text, re.I | re.S) is not None
    except re.error:
        return False


def match_decision(db, text: str, project: str = "", status: str = "decided") -> dict | None:
    """The first register entry of the given status whose pattern matches the question text."""
    rows = db.all("SELECT * FROM owner_decisions WHERE kind='decision' AND status=%s AND (project='' OR project=%s)"
                  " ORDER BY id", (status, project))
    for row in rows:
        if any(_search(p, text) for p in row["patterns"] or []):
            return row
    return None


def environment_of(text: str) -> str:
    for name, pattern in ENVIRONMENTS:
        if pattern.search(text):
            return name
    return ""


def classify(db, text: str, project: str = "") -> Classification:
    """Classify one escalation from its question text. The register is consulted first: what the owner already
    decided is never asked again."""
    text = (text or "")[:6000]
    decision = match_decision(db, text, project)
    if decision:
        return Classification(TAKEN, f"the owner already decided this ({decision['title']})", decision=decision)
    if POLICY_RE.search(text):
        return Classification(POLICY, "a policy message about one command or write, not a restriction needing a person")
    environment = environment_of(text)
    criterion = CRITERION_RE.search(text)
    if criterion and IMPOSSIBLE_RE.search(text) and (environment or re.search(r"\b(?:machine|environment|evidence|screenshot|server)\b", text, re.I)):
        ac = re.search(r"\bAC\s?(\d+)\b", text, re.I)
        return Classification(UNREACHABLE, "an acceptance criterion that cannot be met in this environment",
                              environment=environment, criterion=f"AC{ac.group(1)}" if ac else "")
    missing = parse_missing_requirements(text)
    if missing:
        requirement = missing[0]
        return Classification(MISSING, f"the run explicitly reported missing tool or service {requirement.replace('_', ' ')}",
                              environment=requirement)
    if environment and NEED_RE.search(text):
        return Classification(MISSING, f"the machine lacks or cannot reach {environment.replace('_', ' ')}",
                              environment=environment)
    pending = match_decision(db, text, project, "asked")
    if pending:
        return Classification(BUSINESS, f"already asked ({pending['title']}); waiting for the owner", pending=pending)
    if AMBIGUITY_RE.search(text) or REPOSITORY_FACT_RE.search(text):
        return Classification(AMBIGUITY, "the repository (ticket, spec or code) can answer this")
    if IRREVERSIBLE_RE.search(text):
        return Classification(IRREVERSIBLE, "an irreversible or production action")
    return Classification(BUSINESS, "a business decision no source answers")


# --- register ---------------------------------------------------------------------------------------------
def citation(decision: dict) -> str:
    when = decision["decided_at"].date().isoformat() if decision.get("decided_at") else ""
    who = f", {decision['decided_by']}" if decision.get("decided_by") else ""
    return f"Owner decision D{decision['id']} \"{decision['title']}\"{who}{', ' + when if when else ''}"


def cite(db, decision: dict) -> str:
    db.run("UPDATE owner_decisions SET cited_count=cited_count+1, last_cited_at=now() WHERE id=%s", (decision["id"],))
    return citation(decision)


def keyword_patterns(keywords: list[str], topic: str = "") -> list[str]:
    """A pattern that needs every keyword; at least two so one generic word never matches a whole family."""
    words = [k.strip().lower() for k in keywords if len(k.strip()) >= 3]
    if len(words) < 2:
        stop = {"should", "which", "about", "would", "could", "there", "their", "these", "those", "using", "while", "other"}
        words = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]{5,}", topic.lower()) if w not in stop))[:3]
    words = list(dict.fromkeys(words))[:5]
    if len(words) < 2:
        return []
    return ["".join(f"(?=.*{re.escape(w)})" for w in words)]


def normalize_question(text: str) -> str:
    """Create a stable key for the actual question, independent of generated triage keywords and punctuation."""
    normalized = unicodedata.normalize("NFKC", (text or "")).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE).split())[:3000]


def unique_topic(db, base: str) -> str:
    base = slug(base)[:36] or "decision"
    topic, n = base, 1
    while db.one("SELECT 1 AS x FROM owner_decisions WHERE topic=%s", (topic,)):
        n += 1
        topic = f"{base}-{n}"
    return topic


def add_decision(db, title: str, decision: str, keywords: list[str] | None = None, patterns: list[str] | None = None,
                 project: str = "", by: str = "owner", question: str = "", source: str = "owner") -> dict:
    """Record a decision the owner has made (Command Center, or an answered ask)."""
    patterns = [p for p in (patterns or []) if str(p).strip()]
    for p in patterns:
        re.compile(p)  # a bad pattern is refused with re.error, never stored
    patterns = patterns or keyword_patterns(keywords or [], title)
    topic = unique_topic(db, title)
    return db.one("INSERT INTO owner_decisions (topic,title,question,decision,patterns,project,source,decided_by,decided_at)"
                  " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *",
                  (topic, title[:200], question[:2000], decision[:3000], J(patterns), project, source, by))


def ask(ctx, ticket: str, project: str, text: str, why: str, options: list[str], recommended: str, topic: str,
        keywords: list[str]) -> dict:
    """A new kind of decision is asked once: a pending register entry carries the options and the recommended
    default. A decision already taken is applied, and a question already asked only adds the ticket to the wait.
    Matching and insertion share a transaction lock so concurrent triage runs cannot create duplicate asks."""
    db = ctx.db
    full = f"{text}\n{why}"
    question_key = normalize_question(text)
    with db.pool.connection() as connection:
        with connection.transaction():
            # A single short critical section protects the normalized-question lookup and insert across control
            # workers. Generated topics and keyword patterns can change between triage runs.
            connection.execute("SELECT pg_advisory_xact_lock(255, 255)")
            locked_db = _ConnectionQueries(connection)
            decided_question = (locked_db.one(
                "SELECT * FROM owner_decisions WHERE kind='decision' AND status='decided' "
                "AND question_key=%s AND (project='' OR project=%s) ORDER BY id LIMIT 1",
                (question_key, project)) if question_key else None)
            if decided_question:
                return {"action": "applied", "decision": decided_question,
                        "citation": cite(locked_db, decided_question)}
            decided = match_decision(locked_db, full, project)
            if decided:
                return {"action": "applied", "decision": decided, "citation": cite(locked_db, decided)}
            pending = (locked_db.one("SELECT * FROM owner_decisions WHERE kind='decision' AND status='asked' "
                                     "AND question_key=%s AND (project='' OR project=%s) ORDER BY id LIMIT 1",
                                     (question_key, project)) if question_key else None)
            pending = pending or match_decision(locked_db, full, project, "asked") or locked_db.one(
                "SELECT * FROM owner_decisions WHERE status='asked' AND %s = ANY(waiting_tickets)", (ticket,))
            if pending:
                locked_db.run("UPDATE owner_decisions SET waiting_tickets=CASE WHEN %s = ANY(waiting_tickets) THEN waiting_tickets"
                              " ELSE array_append(waiting_tickets,%s) END WHERE id=%s", (ticket, ticket, pending["id"]))
                return {"action": "waiting", "decision": pending,
                        "citation": f"Question D{pending['id']} \"{pending['title']}\""}
            cleaned_options = [o.strip() for o in options if o.strip()]
            if recommended and recommended not in cleaned_options:
                cleaned_options.append(recommended)
            row = locked_db.one(
                "INSERT INTO owner_decisions (topic,title,question,options,recommended,patterns,project,status,source,"
                "ticket_key,waiting_tickets,question_key) VALUES (%s,%s,%s,%s,%s,%s,%s,'asked','ask',%s,%s,%s) RETURNING *",
                (unique_topic(locked_db, topic or why[:60]), (topic or why)[:200], f"{text}\n{why}".strip()[:3000],
                 J(cleaned_options), recommended[:500], J(keyword_patterns(keywords, topic or why)), "", ticket, [ticket],
                 question_key))
            return {"action": "asked", "decision": row, "citation": f"Question D{row['id']} \"{row['title']}\""}


class _ConnectionQueries:
    """Small DB facade for queries that must participate in one psycopg transaction."""

    def __init__(self, connection) -> None:
        self.connection = connection

    def all(self, sql: str, params: Any = None) -> list[dict]:
        return list(self.connection.execute(sql, params).fetchall())

    def one(self, sql: str, params: Any = None) -> dict | None:
        return self.connection.execute(sql, params).fetchone()

    def run(self, sql: str, params: Any = None) -> int:
        return self.connection.execute(sql, params).rowcount


def resume_waiting(ctx, decision: dict, text: str, skip_ticket: str = "") -> list[str]:
    """Resume persisted owner answers. A failed Jira write leaves a `resuming` escalation for the next retry."""
    resumed = []
    pending = ctx.db.all("SELECT DISTINCT ticket_key FROM escalations WHERE decision_id=%s AND resolution='resuming'"
                         " ORDER BY ticket_key", (decision["id"],))
    keys = [row["ticket_key"] for row in pending]
    if not keys:  # compatibility for callers that have not yet persisted a resumption
        keys = list(decision.get("waiting_tickets") or [])
    for key in keys:
        try:
            if key != skip_ticket:
                ctx.jira.remove_label(key, "owner-escalated")
                ctx.db.run("UPDATE tickets SET labels=array_remove(labels,'owner-escalated') WHERE key=%s", (key,))
                comment = (f"AI Flow · owner decision\nWhat: {decision['title']}\nResult: {text}\n"
                           f"Next: back to To Do; the developer continues with {citation(decision)}")
                try:
                    comments = ctx.jira.recent_comments(key, limit=100)
                except Exception:  # noqa: BLE001 - comment history is only a duplicate guard
                    comments = []
                if not any(str(item.get("body") or "").strip() == comment for item in comments):
                    ctx.jira.comment(key, comment)
                status = ctx.jira.transition_to(key, "To Do")
                if status != "To Do":
                    raise RuntimeError(f"{key} did not transition back to To Do")
                ctx.db.run("UPDATE tickets SET status=%s, synced_at=now() WHERE key=%s", (status, key))
            ctx.db.run("UPDATE escalations SET answer=%s, resolution='person', reached_person=true, resolved_at=now()"
                       " WHERE ticket_key=%s AND decision_id=%s AND resolution='resuming'",
                       (text, key, decision["id"]))
            resumed.append(key)
        except Exception as exc:  # noqa: BLE001 - one ticket must not keep the others waiting
            log.warning("resuming %s after decision %s failed: %s", key, decision["id"], exc)
    return resumed


def _store_owner_answer(db, decision_id: int, text: str, by: str) -> dict:
    """Save the owner's choice and mark every affected escalation retryable in one transaction."""
    with db.pool.connection() as connection:
        with connection.transaction():
            row = connection.execute("SELECT * FROM owner_decisions WHERE id=%s FOR UPDATE", (decision_id,)).fetchone()
            if not row or row["status"] not in ("asked", "decided"):
                raise LookupError("this question is not waiting for an answer")
            was_asked = row["status"] == "asked"
            if was_asked:
                row = connection.execute(
                    "UPDATE owner_decisions SET status='decided', decision=%s, decided_by=%s, decided_at=now()"
                    " WHERE id=%s AND status='asked' RETURNING *", (text[:3000], by, decision_id)).fetchone()
            pending = connection.execute(
                "UPDATE escalations SET answer=%s, resolution='resuming', reached_person=true, resolved_at=NULL"
                " WHERE decision_id=%s AND resolution IN ('person','waiting','resuming') RETURNING id",
                (row["decision"], decision_id)).fetchall()
            if not was_asked and not pending:
                raise LookupError("this question is not waiting for an answer")
            return row


def answer_decision(ctx, decision_id: int, choice: str, note: str, by: str) -> dict:
    row = ctx.db.one("SELECT * FROM owner_decisions WHERE id=%s", (decision_id,))
    if not row or row["status"] not in ("asked", "decided"):
        raise LookupError("this question is not waiting for an answer")
    if row["status"] == "asked":
        chosen = row["recommended"] if choice.strip().lower() in ("", "recommended", "default") else choice.strip()
        text = chosen + (f" ({note.strip()})" if note.strip() else "")
        row = _store_owner_answer(ctx.db, decision_id, text, by)
    else:
        text = row["decision"]
        row = _store_owner_answer(ctx.db, decision_id, text, row["decided_by"] or by)
    resume_waiting(ctx, row, text)
    return ctx.db.one("SELECT * FROM owner_decisions WHERE id=%s", (decision_id,))


def retry_resumptions(ctx) -> list[str]:
    """Retry tickets whose saved owner decision could not yet be written back to Jira."""
    decisions = ctx.db.all(
        "SELECT DISTINCT d.* FROM owner_decisions d JOIN escalations e ON e.decision_id=d.id"
        " WHERE d.status='decided' AND e.resolution='resuming' ORDER BY d.id")
    resumed = []
    for decision in decisions:
        keys = [row["ticket_key"] for row in ctx.db.all(
            "SELECT DISTINCT ticket_key FROM escalations WHERE decision_id=%s AND resolution='resuming'"
            " ORDER BY ticket_key", (decision["id"],))]
        resumed.extend(resume_waiting(ctx, {**decision, "waiting_tickets": keys}, decision["decision"]))
    return resumed


def retire_decision(db, decision_id: int) -> dict | None:
    return db.one("UPDATE owner_decisions SET status='retired' WHERE id=%s RETURNING *", (decision_id,))


def _comment_time(comment: dict) -> datetime | None:
    value = str(comment.get("created") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def owner_answered_on_ticket(ctx, ticket: str) -> dict | None:
    """Capture the owner's Jira comment when they move an asked ticket back to work.

    A question comment posted by AI Flow is not an answer. If no later human comment is available, leave the
    decision pending instead of silently applying the recommended default.
    """
    rows = ctx.db.all("SELECT * FROM owner_decisions WHERE status='asked' AND %s = ANY(waiting_tickets) ORDER BY id",
                      (ticket,))
    if not rows or not ctx.jira:
        return None
    try:
        comments = ctx.jira.recent_comments(ticket, limit=30)
        authorized_account_id = str(ctx.jira.current_account_id() or "").strip()
    except Exception as exc:  # noqa: BLE001 - a Jira read failure must not fabricate an owner decision
        log.warning("comments of %s for owner decision failed: %s", ticket, type(exc).__name__)
        return None
    if not authorized_account_id:
        log.warning("Jira did not provide an authorized owner account id for %s", ticket)
        return None
    eligible = []
    for comment in comments:
        body = str(comment.get("body") or "").strip()
        author_account_id = str(comment.get("account_id") or "").strip()
        created = _comment_time(comment)
        if (not body or not created or author_account_id != authorized_account_id
                or body.lower().startswith(("ai flow", "aiflow"))):
            continue
        eligible.append((created, body))
    for row in rows:
        answer = next((body for created, body in sorted(eligible, reverse=True)
                       if created > row["created_at"].astimezone(timezone.utc)), "")
        if not answer:
            continue
        text = answer[:3000]
        try:
            decided = _store_owner_answer(ctx.db, row["id"], text, "owner")
        except LookupError:
            continue
        raised = ctx.db.one("SELECT question FROM escalations WHERE ticket_key=%s AND decision_id=%s"
                            " ORDER BY id DESC LIMIT 1", (ticket, row["id"]))
        # The decision is shared by every ticket that joined the ask. Resume the other waiters and record the
        # actual owner answer against each escalation; the current ticket is already being claimed for work.
        resume_waiting(ctx, {**decided, "waiting_tickets": row.get("waiting_tickets") or []}, text,
                       skip_ticket=ticket)
        return {"decision": decided, "answer": text, "question": (raised or {}).get("question") or row["question"],
                "ticket": ticket}
    return None


# --- ticket amendment (AC4) ---------------------------------------------------------------------------------
def amend_description(description: str, criterion: str, decision_id: int, environment: str, narrowed: str = "",
                      today: str = "") -> tuple[str, str] | None:
    """Narrow or remove one acceptance criterion in the ticket text. Returns the new description and a summary,
    or None when the criterion line is not in the description."""
    today = today or datetime.now(timezone.utc).date().isoformat()
    number = re.sub(r"\D", "", criterion)
    if not number:
        return None
    line = re.compile(rf"^(?P<lead>\s*(?:[-*]\s*)?)(?P<name>AC\s?{number})\b\s*[:.)-]?\s*(?P<body>.*)$", re.I | re.M)
    found = line.search(description or "")
    if not found:
        return None
    name, original = found.group("name").replace(" ", ""), found.group("body").strip()
    tag = f"amended {today}, register D{decision_id}"
    if tag in original:
        summary = f"{name} {'removed' if '[removed' in original else 'narrowed'}"
        return description, summary
    if narrowed.strip().upper() == "REMOVED":
        replacement = f"{found.group('lead')}{name}: [removed, {tag}] Not verifiable in this environment. Original: {original}"
        summary = f"{name} removed"
    else:
        what = narrowed.strip() or (f"met by the checks a machine of the fleet can run (tests and static checks); evidence that needs "
                                    f"{environment.replace('_', ' ') or 'an environment that does not exist'} is not required")
        replacement = f"{found.group('lead')}{name}: [narrowed, {tag}] {what}. Original: {original}"
        summary = f"{name} narrowed"
    return description[:found.start()] + replacement + description[found.end():], summary


def amend_ticket(ctx, ticket: str, project: str, criterion: str, environment: str, reason: str, narrowed: str = "",
                 by: str = "triage") -> dict | None:
    """Amend the ticket itself and record the amendment in the register. Never blocks: the ticket goes back to work."""
    row = ctx.db.one("SELECT description FROM tickets WHERE key=%s", (ticket,))
    if not row or not criterion:
        return None
    existing = ctx.db.one("SELECT * FROM owner_decisions WHERE kind='amendment' AND ticket_key=%s AND title LIKE %s",
                          (ticket, f"{criterion.upper()} %"))
    if existing and existing["decision"] != "pending":  # the same completed criterion is amended once
        done = existing
        return {"decision": done, "summary": done["decision"], "citation": citation(done), "already": True}
    entry = existing or ctx.db.one(
        "INSERT INTO owner_decisions (topic,kind,title,question,decision,project,source,ticket_key,decided_by,decided_at)"
        " VALUES (%s,'amendment',%s,%s,%s,%s,'amendment',%s,%s,now()) RETURNING *",
        (unique_topic(ctx.db, f"amend {ticket} {criterion}"), f"{criterion.upper()} of {ticket} amended", reason[:2000],
         "pending", project, ticket, by))
    amended = amend_description(row["description"] or "", criterion, entry["id"], environment, narrowed)
    if not amended:
        if not existing:
            ctx.db.run("DELETE FROM owner_decisions WHERE id=%s AND kind='amendment' AND decision='pending'",
                       (entry["id"],))
        return None
    text, summary = amended
    # Keep the pending register row if Jira is unavailable or the process stops here. A retry finds the same row
    # and replays this ticket update instead of treating the pending entry as a completed amendment.
    ctx.jira.set_description(ticket, text)
    ctx.db.run("UPDATE tickets SET description=%s, synced_at=now() WHERE key=%s", (text, ticket))
    entry = ctx.db.one("UPDATE owner_decisions SET decision=%s WHERE id=%s RETURNING *",
                       (f"{summary}: {reason}"[:3000], entry["id"]))
    return {"decision": entry, "summary": summary, "citation": citation(entry), "already": False}


# --- recording ----------------------------------------------------------------------------------------------
def record(db, ticket: str, project: str, source: str, run_id: str, cls: str, question: str, reason: str = "",
           resolution: str = "open", decision_id: int | None = None, answer: str = "",
           reached_person: bool = False) -> dict:
    return db.one("INSERT INTO escalations (ticket_key,project,source,run_id,class,question,reason,decision_id,"
                  "resolution,reached_person,answer,resolved_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                  "CASE WHEN %s IN ('open','waiting') THEN NULL ELSE now() END) RETURNING *",
                  (ticket, project, source, run_id, cls, question[:3000], reason[:500], decision_id, resolution,
                   reached_person, answer[:3000], resolution))


def open_escalation(db, ticket: str) -> dict | None:
    return db.one("SELECT * FROM escalations WHERE ticket_key=%s AND resolution='open' ORDER BY id DESC LIMIT 1", (ticket,))


def settle(db, ticket: str, project: str, source: str, run_id: str, cls: str, question: str, reason: str,
           resolution: str, decision_id: int | None = None, answer: str = "", reached_person: bool = False) -> dict:
    """Close the open escalation of the ticket with its final class, or record a new one."""
    current = open_escalation(db, ticket)
    if not current:
        return record(db, ticket, project, source, run_id, cls, question, reason, resolution, decision_id, answer,
                      reached_person)
    return db.one("UPDATE escalations SET class=%s, reason=%s, resolution=%s, decision_id=coalesce(%s, decision_id),"
                  " answer=%s, reached_person=%s, run_id=%s,"
                  " resolved_at=CASE WHEN %s IN ('open','waiting') THEN NULL ELSE now() END"
                  " WHERE id=%s RETURNING *",
                  (cls, reason[:500], resolution, decision_id, answer[:3000], reached_person, run_id or current["run_id"],
                   resolution, current["id"]))


ENVIRONMENT_ANSWERS = {
    "ci_log": "The tail of the failing CI job log of the pull request is read through the control API "
              "(GET /v1/github/ci-log) and is part of the run prompt when the branch has a pull request; do not ask for it.",
}


def dev_question(ctx, run: dict, question: str) -> dict:
    """Classify the question a developer run stopped on and, for the classes that never need a person, resolve it:
    the result tells the worker what to post and which outcome the run has."""
    db, ticket, project = ctx.db, run["ticket_key"], run["project"]
    c = classify(db, question, project)
    base = {"class": c.cls, "label": LABELS[c.cls], "reason": c.reason, "resolved": False, "outcome": "blocked",
            "answer": "", "citation": "", "missing_tools": []}
    if c.cls == TAKEN:
        text = c.decision["decision"]
        cited = cite(db, c.decision)
        settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "register", c.decision["id"], text)
        return {**base, "resolved": True, "outcome": "auto_answered", "answer": text, "citation": cited,
                "decision_id": c.decision["id"]}
    if c.cls == POLICY:
        from .policy import POLICY_MESSAGE_HELP
        settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "policy", None, POLICY_MESSAGE_HELP)
        return {**base, "resolved": True, "outcome": "auto_answered", "answer": POLICY_MESSAGE_HELP}
    if c.cls == UNREACHABLE:
        amended = amend_ticket(ctx, ticket, project, c.criterion, c.environment, question, by="triage")
        if amended:
            text = (f"{amended['summary']} on {ticket}: the criterion cannot be met in this environment "
                    f"({c.environment.replace('_', ' ') or 'evidence needs a machine that does not exist'}). Continue with the "
                    "amended criterion; the amendment is in the ticket text and the owner register, and appears in the weekly report.")
            settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "amendment",
                   amended["decision"]["id"], text)
            return {**base, "resolved": True, "outcome": "auto_answered", "answer": text, "citation": amended["citation"],
                    "decision_id": amended["decision"]["id"], "amended": True}
        # the criterion cannot be named from the text: the triage AI amends it (DECISION: AMEND)
        record(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason)
        return base
    if c.cls == MISSING:
        node = db.one("SELECT tools, metrics FROM nodes WHERE name=%s", (run["node"],)) or {}
        if c.environment in ENVIRONMENT_ANSWERS:
            text = ENVIRONMENT_ANSWERS[c.environment]
            settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "machine", None, text)
            return {**base, "resolved": True, "outcome": "auto_answered", "answer": text}
        if c.environment == "browser":
            from ..core.toolchains import parse_missing_requirements
            browser_tools = {"chromium", "firefox", "webkit"}
            requirement = next((name for name in parse_missing_requirements(question) if name in browser_tools),
                               "chromium")
        else:
            requirement = c.environment.replace("_", "-")
        if not tooling.requirement_is_accepted(ctx, project, requirement):
            text = (f"AI Flow cannot report {requirement} from a node heartbeat yet, so the requirement was not parked. "
                    f"The run failed with the original report: {question[:1200]}")
            settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason,
                   "unreportable_requirement", None, text)
            return {**base, "resolved": True, "outcome": "failed", "answer": text}
        if tooling.machine_has(node, requirement):
            version = (node.get("tools") or {}).get(requirement, "")
            text = (f"{requirement} is installed on {run['node']} ({version}); it is available to this run. "
                    "Use it instead of asking for it. If it fails to start, report the exact error with "
                    "`RESULT: BLOCKED: MISSING-TOOL: " + requirement + "`.")
            settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "machine", None, text)
            return {**base, "resolved": True, "outcome": "auto_answered", "answer": text}
        names = tooling.record_missing(ctx, run, [requirement], question)
        text = (f"{requirement} is missing on {run['node']}. The ticket waits for a machine that has it; "
                "nothing is asked of the owner.")
        settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason, "machine", None, text)
        return {**base, "resolved": True, "outcome": "tool_missing", "answer": text, "missing_tools": names}
    record(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason)
    return base


def triage_decision(ctx, run: dict, decision: dict, question: str) -> dict:
    """Apply the triage AI's decision through the register. Returns the action for the worker:
    answered | applied | amended | asked | waiting | refused."""
    db, ticket, project, run_id = ctx.db, run["ticket_key"], run["project"], str(run["id"])
    kind = decision["decision"]
    raised = open_escalation(db, ticket)
    if not raised:
        return {"action": "refused", "class": "", "reason": "there is no open raised escalation to decide"}
    # The class and question come from the fenced raise path. Triage output cannot relabel a question to
    # bypass the owner boundary or change which decision is being answered.
    cls = raised["class"]
    question = raised["question"]
    if cls not in CLASSES:
        return {"action": "refused", "class": cls, "reason": "the raised escalation has an invalid class"}
    if kind == "ANSWER":
        if cls in PERSON_CLASSES:
            return {"action": "refused", "class": cls,
                    "reason": f"{LABELS[cls]} requires the owner; triage cannot answer it"}
        settle(db, ticket, project, "triage", run_id, cls, question, decision.get("why", ""), "triage", None,
               decision.get("answer", ""))
        return {"action": "answered", "class": cls}
    if kind == "AMEND":
        if cls != UNREACHABLE:
            return {"action": "refused", "class": cls,
                    "reason": "only an unreachable acceptance criterion can be amended"}
        amended = amend_ticket(ctx, ticket, project, decision.get("criterion", ""), environment_of(question),
                               decision.get("why", ""), decision.get("narrowed", ""), by="triage")
        if not amended:
            return {"action": "refused", "class": UNREACHABLE,
                    "reason": f"{decision.get('criterion') or 'the criterion'} is not a line of the ticket description"}
        settle(db, ticket, project, "triage", run_id, UNREACHABLE, question, decision.get("why", ""), "amendment",
               amended["decision"]["id"], amended["summary"])
        return {"action": "amended", "class": UNREACHABLE, "citation": amended["citation"], "summary": amended["summary"],
                "decision_id": amended["decision"]["id"]}
    if cls not in PERSON_CLASSES:
        return {"action": "refused", "class": cls,
                "reason": f"{LABELS.get(cls, cls)} never reaches a person: answer it (DECISION: ANSWER) or amend the ticket"}
    asked = ask(ctx, ticket, project, question, decision.get("why", ""), decision.get("options") or [],
                decision.get("recommended", ""), decision.get("topic", ""), decision.get("keywords") or [])
    row = asked["decision"]
    if asked["action"] == "applied":
        settle(db, ticket, project, "triage", run_id, TAKEN, question, "decision already taken", "register", row["id"],
               row["decision"])
        return {"action": "applied", "class": TAKEN, "citation": asked["citation"], "answer": row["decision"],
                "decision_id": row["id"]}
    if asked["action"] == "waiting":
        settle(db, ticket, project, "triage", run_id, cls, question, "already asked; waiting", "waiting", row["id"], "")
        return {"action": "waiting", "class": cls, "citation": asked["citation"], "decision_id": row["id"],
                "options": row["options"], "recommended": row["recommended"]}
    settle(db, ticket, project, "triage", run_id, cls, question, decision.get("why", ""), "person", row["id"], "",
           reached_person=True)
    return {"action": "asked", "class": cls, "citation": asked["citation"], "decision_id": row["id"],
            "options": row["options"], "recommended": row["recommended"]}


def pending_answer(db, ticket: str) -> dict | None:
    """The newest answer an escalation of this ticket received without a triage run (register, policy, machine,
    amendment): the next developer run gets the question and the answer in its prompt."""
    return db.one("SELECT question, answer, created_at FROM escalations WHERE ticket_key=%s AND answer<>''"
                  " AND resolution IN ('register','policy','machine','amendment')"
                  " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=escalations.ticket_key AND r.step='dev'"
                  " AND r.started_at > escalations.created_at AND r.outcome IN ('done','blocked','auto_answered'))"
                  " ORDER BY id DESC LIMIT 1", (ticket,))


# --- metrics (AC7) ----------------------------------------------------------------------------------------------
def week_start(value: datetime) -> datetime:
    utc = value.astimezone(timezone.utc)
    return datetime.combine(utc.date() - timedelta(days=utc.weekday()), datetime.min.time(), tzinfo=timezone.utc)


def weekly_metrics(db, weeks: int = ACTIVE_WINDOW_WEEKS, now: datetime | None = None) -> dict[str, Any]:
    """Escalations per week by class and project, the share resolved without a person and the top three causes."""
    now = now or datetime.now(timezone.utc)
    this_week = week_start(now)
    first = this_week - timedelta(weeks=weeks - 1)
    rows = db.all("SELECT id,ticket_key,project,class,resolution,reached_person,created_at,question FROM escalations"
                  " WHERE created_at >= %s ORDER BY created_at", (first,))
    series = {first + timedelta(weeks=i): {"week_start": (first + timedelta(weeks=i)).date().isoformat(), "total": 0,
                                           "by_class": {c: 0 for c in CLASSES}, "by_project": {},
                                           "without_person": 0, "reached_person": 0} for i in range(weeks)}
    for row in rows:
        bucket = series.get(week_start(row["created_at"]))
        if not bucket:
            continue
        bucket["total"] += 1
        bucket["by_class"][row["class"]] = bucket["by_class"].get(row["class"], 0) + 1
        per = bucket["by_project"].setdefault(row["project"] or "-", {c: 0 for c in CLASSES})
        per[row["class"]] = per.get(row["class"], 0) + 1
        if row["reached_person"]:
            bucket["reached_person"] += 1
        elif row["resolution"] not in ("open", "waiting"):
            bucket["without_person"] += 1
    trend = []
    for bucket in series.values():
        bucket["share_without_person_pct"] = (round(100 * bucket["without_person"] / bucket["total"], 1)
                                              if bucket["total"] else None)
        trend.append(bucket)
    counts: dict[str, list[dict]] = {}
    for row in rows:
        counts.setdefault(row["class"], []).append(row)
    causes = sorted(counts.items(), key=lambda kv: (-len(kv[1]), CLASSES.index(kv[0]) if kv[0] in CLASSES else 99))[:3]
    top = [{"class": cls, "label": LABELS.get(cls, cls), "count": len(items), "fix": FIXES.get(cls, ""),
            "tickets": list(dict.fromkeys(i["ticket_key"] for i in items))[:8],
            "reaches_person": cls in PERSON_CLASSES} for cls, items in causes]
    current = series[this_week]
    previous = series.get(this_week - timedelta(weeks=1))
    falling = bool(previous and previous["total"] and current["total"] < previous["total"])
    amendments = db.all("SELECT id,ticket_key,project,title,decision,question,created_at FROM owner_decisions"
                        " WHERE kind='amendment' AND created_at >= %s ORDER BY id DESC LIMIT 100", (first,))
    return {"weeks": trend, "classes": list(CLASSES), "labels": LABELS, "person_classes": list(PERSON_CLASSES),
            "this_week": current, "falling": falling, "top_causes": top,
            "amendments": [{**a, "created_at": a["created_at"].isoformat()} for a in amendments]}


def register(db) -> dict[str, list[dict]]:
    """The register for Command Center: decisions, open questions and amendments."""
    rows = db.all("SELECT * FROM owner_decisions WHERE status<>'retired' ORDER BY id DESC LIMIT 500")
    def public(row: dict) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
    resuming = db.all("SELECT DISTINCT d.*, ARRAY(SELECT DISTINCT pending.ticket_key FROM escalations pending"
                      " WHERE pending.decision_id=d.id AND pending.resolution='resuming' ORDER BY pending.ticket_key)"
                      " AS resuming_tickets FROM owner_decisions d JOIN escalations e ON e.decision_id=d.id"
                      " WHERE d.status='decided' AND e.resolution='resuming' ORDER BY d.id DESC LIMIT 100")
    return {"decisions": [public(r) for r in rows if r["kind"] == "decision" and r["status"] == "decided"],
            "asked": [public(r) for r in rows if r["status"] == "asked"],
            "resuming": [public(r) for r in resuming],
            "amendments": [public(r) for r in rows if r["kind"] == "amendment"]}
