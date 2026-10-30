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

from ..core import blocker_kind, technical_decision
from ..core.environment import is_server_question
from ..core.owner_escalation import owner_question_comment
from ..core.text import normalize_adf_text, slug
from . import tooling
from . import ticket_lifecycle
from .features.tickets.repository import TicketRepository
from .repositories import escalations as escalation_repository
from .repositories import nodes as node_repository
from .repositories import triage_claim as triage_claim_repository
from ..core.toolchains import parse_missing_requirements

log = logging.getLogger("aiflow4.escalations")

MISSING = "missing_tool_or_environment"
POLICY = "policy_message"
UNREACHABLE = "unreachable_criterion"
AMBIGUITY = "ambiguity_repository"
TAKEN = "decision_already_taken"
BUSINESS = "new_business_decision"
TECHNICAL = "technical_decision"
IRREVERSIBLE = "irreversible_or_production"
CLASSES = (MISSING, POLICY, UNREACHABLE, AMBIGUITY, TAKEN, TECHNICAL, BUSINESS, IRREVERSIBLE)
PERSON_CLASSES = (BUSINESS, IRREVERSIBLE)
LABELS = {MISSING: "Missing tool or environment", POLICY: "Policy or permission message",
          UNREACHABLE: "Unreachable acceptance criterion", AMBIGUITY: "Ambiguity the repository can answer",
          TAKEN: "Decision already taken", TECHNICAL: "Technical decision for the SA", BUSINESS: "New business decision",
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
    TECHNICAL: "The SA decides and records the choice and the reason; the register keeps it as precedent for the next question.",
    BUSINESS: "Answer once in Command Center: the choice becomes a register entry and is applied from then on.",
    IRREVERSIBLE: "Keep releases, tags and production changes with the owner; nothing to fix.",
}
ACTIVE_WINDOW_WEEKS = 8


class JiraOwnerAnswerRequired(LookupError):
    """A ticket-linked owner question must be answered on Jira so its comment can be verified."""


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
BUSINESS_TOPIC_RE = re.compile(
    r"\b(?:customer(?:s|-facing)?|refund|payment|pricing|billing|subscription|marketing|brand|legal|contract|"
    r"privacy|data retention)\b", re.I)
REQUIREMENT_CONFLICT_RE = re.compile(r"\b(?:conflicts?|contradicts?|contradicting)\s+with\s+(?:the\s+)?AC\s?\d+\b", re.I)
BUSINESS_DECISION_CUE_RE = re.compile(r"\b(?:should|whether|choose|choice|decision|decide|option|or)\b|\?", re.I)


def _search(pattern: str, text: str) -> bool:
    try:
        return re.search(str(pattern), text, re.I | re.S) is not None
    except re.error:
        return False


def match_decision(db, text: str, project: str = "", status: str = "decided") -> dict | None:
    """The first register entry of the given status whose pattern matches the question text."""
    rows = escalation_repository.decision_candidates(db, status, project)
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
    inferred = blocker_kind.missing_tools(text)
    if inferred:  # a tool the run calls absent, in its own words: a machine problem, never the owner's
        return Classification(MISSING, f"the run reported {', '.join(inferred)} as missing on the machine",
                              environment=inferred[0], requirements=inferred)
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
    return _classify_by_topic(text)


def _classify_by_topic(text: str) -> Classification:
    """The questions no source or marker answered: a server question, an engineering choice, a business choice."""
    if is_server_question(text):  # where and how something runs is never a question for the owner
        return Classification(MISSING, "a server, host, network or access question: the project configuration and the "
                                       "run environment answer it", environment="server")
    if technical_decision.is_technical(text):
        return Classification(TECHNICAL, "an engineering choice: the SA decides it and records the reason")
    if REQUIREMENT_CONFLICT_RE.search(text):  # the ticket contradicts its own acceptance criteria: only the owner can say which wins
        return Classification(BUSINESS, "the ticket conflicts with its own acceptance criteria")
    if BUSINESS_TOPIC_RE.search(text) and BUSINESS_DECISION_CUE_RE.search(text):
        return Classification(BUSINESS, "an explicit customer-facing or commercial choice")
    return Classification(AMBIGUITY, "no positive evidence of a new business decision; keep it in automated triage")


# --- register ---------------------------------------------------------------------------------------------
def citation(decision: dict) -> str:
    when = decision["decided_at"].date().isoformat() if decision.get("decided_at") else ""
    who = f", {decision['decided_by']}" if decision.get("decided_by") else ""
    return f"Owner decision D{decision['id']} \"{decision['title']}\"{who}{', ' + when if when else ''}"


def cite(db, decision: dict) -> str:
    escalation_repository.increment_citation(db, decision["id"])
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
    while escalation_repository.topic_exists(db, topic):
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
    return escalation_repository.insert_decision(db, topic, title, question, decision, patterns, project, source, by)


def ask(ctx, ticket: str, project: str, text: str, why: str, options: list[str], recommended: str, topic: str,
        keywords: list[str]) -> dict:
    """A new kind of decision is asked once: a pending register entry carries the options and the recommended
    default. A decision already taken is applied, and a question already asked only adds the ticket to the wait.
    Matching and insertion share a transaction lock so concurrent triage runs cannot create duplicate asks."""
    db = ctx.db
    full = f"{text}\n{why}"
    question_key = normalize_question(text)
    with escalation_repository.owner_decision_transaction(db) as connection:
        return _ask_locked(connection, ticket, project, text, why, options, recommended, topic,
                            keywords, full, question_key)


def _ask_locked(db, ticket: str, project: str, text: str, why: str, options: list[str],
                recommended: str, topic: str, keywords: list[str], full: str, question_key: str) -> dict:
    decided = _decided_question(db, full, project, question_key)
    if decided:
        return {"action": "applied", "decision": decided, "citation": cite(db, decided)}
    pending = _pending_question(db, ticket, full, project, question_key)
    if pending:
        _validate_owner_question_handoff(text, pending.get("options") or [], pending.get("recommended") or "")
        escalation_repository.add_waiting_ticket(db, pending["id"], ticket)
        return {"action": "waiting", "decision": pending,
                "citation": f"Question D{pending['id']} \"{pending['title']}\""}
    return _insert_asked_question(db, ticket, text, why, options, recommended, topic, keywords, question_key)


def _decided_question(db, full: str, project: str, question_key: str) -> dict | None:
    exact = escalation_repository.decision_by_question_key(db, "decided", question_key, project)
    return exact or match_decision(db, full, project)


def _pending_question(db, ticket: str, full: str, project: str, question_key: str) -> dict | None:
    exact = escalation_repository.decision_by_question_key(db, "asked", question_key, project)
    return (exact or match_decision(db, full, project, "asked")
            or escalation_repository.pending_decision_for_ticket(db, ticket))


def _validate_owner_question_handoff(question: str, options: list[str], recommended: str) -> None:
    owner_question_comment(question, options, recommended)
    owner_question_comment(question, options, recommended, grouped=True)


def _insert_asked_question(db, ticket: str, text: str, why: str, options: list[str], recommended: str,
                           topic: str, keywords: list[str], question_key: str) -> dict:
    if not isinstance(options, list) or any(not isinstance(option, str) for option in options):
        raise ValueError("owner question options must be a list of strings")
    if not isinstance(recommended, str):
        raise ValueError("owner question recommendation must be a string")
    cleaned_options = [option.strip() for option in options if option.strip()]
    recommended = recommended.strip()
    if recommended and recommended not in cleaned_options:
        cleaned_options.append(recommended)
    _validate_owner_question_handoff(text, cleaned_options, recommended)
    row = escalation_repository.insert_asked_decision(
        db, unique_topic(db, topic or why[:60]), topic or why, f"{text}\n{why}".strip(), cleaned_options,
        recommended, keyword_patterns(keywords, topic or why), ticket, question_key)
    return {"action": "asked", "decision": row, "citation": f"Question D{row['id']} \"{row['title']}\""}


def resume_waiting(ctx, decision: dict, text: str, skip_ticket: str = "") -> list[str]:
    """Resume persisted owner answers. A failed Jira write leaves a `resuming` escalation for the next retry."""
    if not _verified_jira_answer_for_tickets(ctx, decision):
        return []
    return _resume_waiting_tickets(ctx, decision, text, skip_ticket)


def _resume_waiting_tickets(ctx, decision: dict, text: str, skip_ticket: str) -> list[str]:
    resumed = []
    pending = escalation_repository.resuming_tickets(ctx.db, decision["id"])
    keys = [row["ticket_key"] for row in pending]
    if not keys:  # compatibility for callers that have not yet persisted a resumption
        keys = list(decision.get("waiting_tickets") or [])
    for key in keys:
        try:
            _resume_waiting_ticket(ctx, decision, text, key, skip_ticket)
            resumed.append(key)
        except Exception as exc:  # noqa: BLE001 - one ticket must not keep the others waiting
            log.warning("resuming %s after decision %s failed: %s", key, decision["id"], exc)
    return resumed


def _resume_waiting_ticket(ctx, decision: dict, text: str, key: str, skip_ticket: str) -> None:
    if key != skip_ticket:
        ticket = TicketRepository(ctx.db).get(key)
        if not ticket:
            raise LookupError(f"ticket {key} is missing from the Jira mirror")
        clear_owner_labels(ctx, key)
        comment = (f"AI Flow · owner decision\nWhat: {decision['title']}\nResult: {text}\n"
                   f"Next: back to To Do; the developer continues with {citation(decision)}")
        try:
            comments = ctx.jira.recent_comments(key, limit=100)
        except Exception:  # noqa: BLE001 - comment history is only a duplicate guard
            comments = []
        if not any(str(item.get("body") or "").strip() == comment for item in comments):
            ctx.jira.comment(key, comment)
        ticket_lifecycle.transition(ctx, {"key": ticket.key, "project": ticket.project}, "verified-owner-answer")
    escalation_repository.finish_resumption(ctx.db, key, decision["id"], text)


def _verified_jira_answer_for_tickets(ctx, decision: dict) -> bool:
    requires_jira = escalation_repository.requires_jira_owner_answer(
        ctx.db, decision["id"], list(decision.get("waiting_tickets") or []))
    return not requires_jira or decision.get("decided_by") in escalation_repository.ACCEPTED_ANSWER_SOURCES


def clear_owner_labels(ctx, ticket: str) -> None:
    """Remove the public and compatibility labels once the owner has answered."""
    for label in ("needs-owner", "owner-escalated"):
        ctx.jira.remove_label(ticket, label)
        TicketRepository(ctx.db).remove_label(ticket, label)


def _store_owner_answer(db, decision_id: int, text: str, by: str) -> dict:
    """Save the owner's choice and mark every affected escalation retryable in one transaction."""
    return escalation_repository.store_owner_answer(db, decision_id, text, by)


def answer_decision(ctx, decision_id: int, choice: str, note: str, by: str) -> dict:
    row = escalation_repository.decision_by_id(ctx.db, decision_id)
    if not row or row["status"] not in ("asked", "decided"):
        raise LookupError("this question is not waiting for an answer")
    if by != escalation_repository.DEADLINE_SOURCE and escalation_repository.requires_jira_owner_answer(
            ctx.db, decision_id, list(row.get("waiting_tickets") or [])):  # only the deadline default may stand in
        raise JiraOwnerAnswerRequired(
            "answer this ticket question in Jira after the question comment, then move it to To Do")
    if row["status"] == "asked":
        chosen = row["recommended"] if choice.strip().lower() in ("", "recommended", "default") else choice.strip()
        text = chosen + (f" ({note.strip()})" if note.strip() else "")
        row = _store_owner_answer(ctx.db, decision_id, text, by)
    else:
        text = row["decision"]
        row = _store_owner_answer(ctx.db, decision_id, text, row["decided_by"] or by)
    resume_waiting(ctx, row, text)
    return escalation_repository.decision_by_id(ctx.db, decision_id)


def retry_resumptions(ctx) -> list[str]:
    """Retry tickets whose saved owner decision could not yet be written back to Jira."""
    decisions = escalation_repository.resuming_decisions(ctx.db, triage_claim_repository.VERIFIED_JIRA_OWNER_SOURCE)
    resumed = []
    for decision in decisions:
        keys = [row["ticket_key"] for row in escalation_repository.resuming_tickets(ctx.db, decision["id"])]
        resumed.extend(resume_waiting(ctx, {**decision, "waiting_tickets": keys}, decision["decision"]))
    return resumed


def retire_decision(db, decision_id: int) -> dict | None:
    source = triage_claim_repository.VERIFIED_JIRA_OWNER_SOURCE
    row = escalation_repository.retire_decision(db, decision_id, source)
    if row:
        return row
    current = escalation_repository.decision_by_id(db, decision_id)
    if not current:
        return None
    requires_answer = ((current["status"] == "asked"
                        or (current["status"] == "decided" and current["decided_by"] != source))
                       and escalation_repository.requires_jira_owner_answer(
                           db, decision_id, list(current.get("waiting_tickets") or [])))
    if requires_answer:
        raise JiraOwnerAnswerRequired(
            "ticket-linked decisions cannot be retired until the owner answers in Jira after the question comment")
    return None


def _comment_time(comment: dict) -> datetime | None:
    value = str(comment.get("created") or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _owner_question_body(row: dict) -> str:
    """Render the same complete Jira question comment the worker emits for timestamp recovery."""
    question = str(row.get("ticket_question") or "").strip()
    options = [str(option).strip() for option in (row.get("options") or []) if str(option).strip()]
    recommended = str(row.get("recommended") or "").strip()
    if not question or not options or not recommended:
        return ""
    return owner_question_comment(question, options, recommended, grouped=bool(row.get("grouped")))


def _owner_handoff_marker(decision_id: int) -> str:
    if not isinstance(decision_id, int) or decision_id < 1:
        return ""
    return f"AI Flow hand-off: D{decision_id}"


def _correlate_owner_question_body(body: str, decision_id: int) -> str:
    marker = _owner_handoff_marker(decision_id)
    if not marker:
        raise ValueError("owner question requires a decision id")
    return f"{body.rstrip()}\n\n{marker}"


def _matching_question_times(comments: list[dict], expected: str, decision_id: int,
                             allow_legacy: bool = False) -> list[datetime]:
    times = []
    marker = _owner_handoff_marker(decision_id)
    if not expected or not marker:
        return times
    expected_comments = {normalize_adf_text(f"{expected.rstrip()}\n\n{marker}")}
    if allow_legacy:
        expected_comments.add(normalize_adf_text(expected))
    for comment in comments:
        body = normalize_adf_text(str(comment.get("body") or ""))
        if body in expected_comments and (created := _comment_time(comment)):
            times.append(created)
    return times


def _owner_question_time(comments: list[dict], row: dict) -> datetime | None:
    """Recover a timestamp from a correlated hand-off, or from an exact legacy comment on an upgraded row."""
    expected = _owner_question_body(row)
    escalation_created = _comment_time({"created": row.get("escalation_created_at")})
    if not expected or not escalation_created:
        return None
    matching = _matching_question_times(comments, expected, row.get("id"),
                                        allow_legacy=bool(row.get("legacy_question_comment")))
    return min((created for created in matching if created >= escalation_created), default=None)


def _owner_comments(ctx, ticket: str) -> tuple[list[dict], list[tuple[datetime, str]]] | None:
    """Read Jira comments and authorize only comments from its configured owner account."""
    try:
        comments = ctx.jira.all_comments(ticket)
        account_id = str(ctx.jira.current_account_id() or "").strip()
    except Exception as exc:  # noqa: BLE001 - a Jira read failure must not fabricate an owner decision
        log.warning("comments of %s for owner decision failed: %s", ticket, type(exc).__name__)
        return None
    if not account_id:
        log.warning("Jira did not provide an authorized owner account id for %s", ticket)
        return None
    eligible = []
    for comment in comments:
        body = str(comment.get("body") or "").strip()
        author = str(comment.get("account_id") or "").strip()
        created = _comment_time(comment)
        if not body or not created or author != account_id or body.lower().startswith(("ai flow", "aiflow")):
            continue
        eligible.append((created, body))
    return comments, sorted(eligible, reverse=True)


def _question_timestamp(ctx, comments: list[dict], row: dict) -> datetime | None:
    """Use the saved Jira timestamp, or recover and persist it from the exact hand-off comment."""
    question_at = row.get("question_comment_at")
    if question_at:
        return question_at
    question_at = _owner_question_time(comments, row)
    escalation_id = row.get("escalation_id")
    if not question_at or not escalation_id:
        return None
    saved = escalation_repository.record_recovered_question_comment(ctx.db, escalation_id, question_at)
    return saved["question_comment_at"] if saved else None


def _answer_after_question(eligible: list[tuple[datetime, str]], question_at: datetime) -> str:
    question_at = question_at.astimezone(timezone.utc)
    return next((body for created, body in eligible if created > question_at), "")


def _record_owner_answer(ctx, ticket: str, row: dict, text: str) -> dict | None:
    """Persist an authorized answer and hand off the shared decision to its other waiting tickets."""
    try:
        decided = _store_owner_answer(ctx.db, row["id"], text,
                                      triage_claim_repository.VERIFIED_JIRA_OWNER_SOURCE)
    except LookupError:
        return None
    clear_owner_labels(ctx, ticket)
    raised = escalation_repository.latest_question_for_decision(ctx.db, ticket, row["id"])
    answer = decided["decision"]
    resume_waiting(ctx, {**decided, "waiting_tickets": row.get("waiting_tickets") or []}, answer, skip_ticket=ticket)
    return {"decision": decided, "answer": answer,
            "question": (raised or {}).get("question") or row["question"], "ticket": ticket}


def owner_answered_on_ticket(ctx, ticket: str) -> dict | None:
    """Capture the owner's Jira comment when they move an asked ticket back to work.

    A question comment posted by AI Flow is not an answer. If no later human comment is available, leave the
    decision pending instead of silently applying the recommended default.
    """
    rows = escalation_repository.pending_owner_decisions(ctx.db, ticket)
    if not rows or not ctx.jira:
        return None
    owner_comments = _owner_comments(ctx, ticket)
    if not owner_comments:
        return None
    comments, eligible = owner_comments
    for row in rows:
        question_at = _question_timestamp(ctx, comments, row)
        if not question_at:
            continue
        answer = _answer_after_question(eligible, question_at)
        if not answer:
            continue
        result = _record_owner_answer(ctx, ticket, row, answer[:3000])
        if result:
            return result
    return None


def post_owner_question_comment(ctx, ticket: str, decision_id: int, run_id: str, body: str) -> dict:
    """Post the registered question and persist Jira's timestamp before it can authorize an owner answer."""
    state = escalation_repository.owner_question_comment_state(ctx.db, ticket, decision_id, run_id)
    if not state:
        raise LookupError("this owner question is no longer pending for the triage run")
    expected = _owner_question_body(state)
    if not expected or body != expected:
        raise LookupError("submitted owner question does not match the registered decision")
    body = expected
    if state["question_comment_at"]:
        return {"comment_id": "", "created": state["question_comment_at"], "already_posted": True}
    existing = _recover_owner_question_comment(ctx, ticket, decision_id, body, state)
    if existing is None:
        return _post_owner_question_comment(ctx, ticket, decision_id, run_id, body)
    created = _owner_question_comment_time(existing, already_posted=True)
    return _save_owner_question_comment(ctx, ticket, decision_id, run_id, existing, created, already_posted=True)


def _recover_owner_question_comment(ctx, ticket: str, decision_id: int, body: str, state: dict) -> dict | None:
    legacy = _recover_legacy_owner_question_comment(ctx, ticket, body, state)
    if legacy is not None:
        return legacy
    correlated_body = _correlate_owner_question_body(body, decision_id)
    return _find_owner_question_comment(ctx, ticket, correlated_body)


def _recover_legacy_owner_question_comment(ctx, ticket: str, body: str, state: dict) -> dict | None:
    if not state.get("legacy_question_comment"):
        return None
    escalation_created = _comment_time({"created": state.get("escalation_created_at")})
    if not escalation_created:
        raise RuntimeError("cannot recover a legacy owner question without its escalation creation time")
    return _find_owner_question_comment(ctx, ticket, body, created_after=escalation_created)


def _post_owner_question_comment(ctx, ticket: str, decision_id: int, run_id: str, body: str) -> dict:
    correlated_body = _correlate_owner_question_body(body, decision_id)
    posted = ctx.jira.comment_with_metadata(ticket, correlated_body)
    created = _owner_question_comment_time(posted)
    return _save_owner_question_comment(ctx, ticket, decision_id, run_id, posted, created, already_posted=False)


def _owner_question_comment_time(comment: dict | None, already_posted: bool = False) -> datetime:
    if not comment or not str(comment.get("id") or "").strip():
        message = ("Jira found the owner question but returned incomplete comment metadata; retry" if already_posted
                   else "Jira did not return an owner question comment timestamp")
        raise RuntimeError(message)
    created = _comment_time(comment)
    if not created:
        message = ("Jira found the owner question but returned incomplete comment metadata; retry" if already_posted
                   else "Jira did not return an owner question comment timestamp")
        raise RuntimeError(message)
    return created


def _find_owner_question_comment(ctx, ticket: str, body: str, created_after: datetime | None = None) -> dict | None:
    try:
        return ctx.jira.find_comment(ticket, body, limit=100, created_after=created_after)
    except Exception as exc:  # noqa: BLE001 - uncertain lookup results must not permit a duplicate Jira write
        log.warning("owner question lookup for %s failed: %s", ticket, type(exc).__name__)
        raise RuntimeError("Jira owner question lookup failed; retry before posting") from exc


def _save_owner_question_comment(ctx, ticket: str, decision_id: int, run_id: str,
                                 comment: dict, created: datetime, already_posted: bool) -> dict:
    saved = escalation_repository.record_owner_question_comment(ctx.db, ticket, decision_id, run_id, created)
    if not saved:
        stage = "recovered" if already_posted else "posted"
        raise LookupError(f"this owner question stopped waiting while its comment was {stage}")
    return {"comment_id": str(comment["id"]), "created": saved["question_comment_at"],
            "already_posted": already_posted}


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
    row = TicketRepository(ctx.db).get(ticket)
    if not row or not criterion:
        return None
    existing = escalation_repository.amendment_for_ticket(ctx.db, ticket, criterion)
    if existing and existing["decision"] != "pending":  # the same completed criterion is amended once
        done = existing
        return {"decision": done, "summary": done["decision"], "citation": citation(done), "already": True}
    entry = existing or escalation_repository.insert_pending_amendment(
        ctx.db, unique_topic(ctx.db, f"amend {ticket} {criterion}"),
        f"{criterion.upper()} of {ticket} amended", reason, project, ticket, by)
    amended = amend_description(row.description, criterion, entry["id"], environment, narrowed)
    if not amended:
        if not existing:
            escalation_repository.delete_pending_amendment(ctx.db, entry["id"])
        return None
    text, summary = amended
    # Keep the pending register row if Jira is unavailable or the process stops here. A retry finds the same row
    # and replays this ticket update instead of treating the pending entry as a completed amendment.
    ctx.jira.set_description(ticket, text)
    TicketRepository(ctx.db).update_description(ticket, text)
    entry = escalation_repository.update_decision(ctx.db, entry["id"], f"{summary}: {reason}")
    return {"decision": entry, "summary": summary, "citation": citation(entry), "already": False}


# --- recording ----------------------------------------------------------------------------------------------
def record(db, ticket: str, project: str, source: str, run_id: str, cls: str, question: str, reason: str = "",
           resolution: str = "open", decision_id: int | None = None, answer: str = "",
           reached_person: bool = False) -> dict:
    return escalation_repository.record_escalation(db, ticket, project, source, run_id, cls, question, reason,
                                                   resolution, decision_id, answer, reached_person)


def raise_reviewer_question(db, ticket: str, project: str, run_id: str, question: str) -> dict:
    """The reviewer's latest question is the one triage decides: it replaces the question of an escalation that is
    still open (a newer question must never be settled against an older one), or opens a new escalation."""
    current = open_escalation(db, ticket)
    if not current:
        return record(db, ticket, project, "review", run_id, AMBIGUITY, question, "reviewer asked the AI")
    return escalation_repository.update_open_question(
        db, current["id"], run_id or current["run_id"], question, AMBIGUITY, "reviewer asked the AI")


def open_escalation(db, ticket: str) -> dict | None:
    return triage_claim_repository.open_escalation(db, ticket)


def settle(db, ticket: str, project: str, source: str, run_id: str, cls: str, question: str, reason: str,
           resolution: str, decision_id: int | None = None, answer: str = "", reached_person: bool = False) -> dict:
    """Close the open escalation of the ticket with its final class, or record a new one."""
    current = open_escalation(db, ticket)
    if not current:
        return record(db, ticket, project, source, run_id, cls, question, reason, resolution, decision_id, answer,
                      reached_person)
    return escalation_repository.update_open_escalation(
        db, current["id"], cls, reason, resolution, decision_id, answer, reached_person, run_id or current["run_id"])


def complete_owner_handoff(ctx, ticket: str, decision_id: int, run_id: str) -> dict:
    """Close a question escalation only after its Jira comment and required labels have been written."""
    row = escalation_repository.complete_owner_handoff(ctx.db, ticket, decision_id, run_id)
    if not row:
        raise LookupError("the owner question hand-off is no longer pending for this triage run")
    return {"ticket": ticket, "decision_id": decision_id, "resolution": row["resolution"]}


ENVIRONMENT_ANSWERS = {
    "server": "Servers, hosts, networks and accounts are never part of a ticket or asked of the owner. AI Flow gives the run "
              "its environment as environment variables (see 'Environment of this run' in the prompt); read them by "
              "name. If an item you need is not listed or not provided, report `RESULT: BLOCKED: MISSING-ENV: <NAME>` "
              "with a name from that list. A criterion that can only be proven on one particular machine is verified by "
              "AI Flow after the merge (canary, release checks); implement and test what runs here.",
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
        node = node_repository.tools_and_metrics(db, run["node"])
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
            text = (f"AI Flow cannot report {requirement} from a node heartbeat yet, so the requirement was not parked, "
                    "and it is not a reason to stop. Continue with the work: use what the machine provides, run the "
                    "checks that can run, and state in your result exactly which check could not run and why.")
            settle(db, ticket, project, "dev", str(run["id"]), c.cls, question, c.reason,
                   "unreportable_requirement", None, text)
            return {**base, "resolved": True, "outcome": "auto_answered", "answer": text}
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
    raised = open_escalation(db, ticket)
    if not raised:
        return {"action": "refused", "class": "", "reason": "there is no open raised escalation to decide"}
    # The class and question come from the fenced raise path. Triage output cannot relabel a question to
    # bypass the owner boundary or change which decision is being answered.
    cls = raised["class"]
    question = raised["question"]
    if cls not in CLASSES:
        return {"action": "refused", "class": cls, "reason": "the raised escalation has an invalid class"}
    if decision["decision"] == "ANSWER":
        return _answer_triage_question(ctx, ticket, project, run_id, cls, question, decision)
    if decision["decision"] == "AMEND":
        return _amend_triage_question(ctx, ticket, project, run_id, cls, question, decision)
    return _ask_owner_question(ctx, ticket, project, run_id, cls, question, decision)


def _answer_triage_question(ctx, ticket: str, project: str, run_id: str, cls: str,
                            question: str, decision: dict) -> dict:
    if cls in PERSON_CLASSES:
        return {"action": "refused", "class": cls,
                "reason": f"{LABELS[cls]} requires the owner; triage cannot answer it"}
    settle(ctx.db, ticket, project, "triage", run_id, cls, question, decision.get("why", ""), "triage", None,
           decision.get("answer", ""))
    if cls == TECHNICAL and decision.get("answer"):  # the choice becomes precedent for the next such question
        add_decision(ctx.db, (decision.get("topic") or question)[:200], decision["answer"], decision.get("keywords") or [],
                     project=project, by="sa", question=question, source="sa")
    return {"action": "answered", "class": cls}


def _amend_triage_question(ctx, ticket: str, project: str, run_id: str, cls: str,
                           question: str, decision: dict) -> dict:
    if cls != UNREACHABLE:
        return {"action": "refused", "class": cls,
                "reason": "only an unreachable acceptance criterion can be amended"}
    amended = amend_ticket(ctx, ticket, project, decision.get("criterion", ""), environment_of(question),
                           decision.get("why", ""), decision.get("narrowed", ""), by="triage")
    if not amended:
        criterion = decision.get("criterion") or "the criterion"
        return {"action": "refused", "class": UNREACHABLE,
                "reason": f"{criterion} is not a line of the ticket description"}
    settle(ctx.db, ticket, project, "triage", run_id, UNREACHABLE, question, decision.get("why", ""), "amendment",
           amended["decision"]["id"], amended["summary"])
    return {"action": "amended", "class": UNREACHABLE, "citation": amended["citation"],
            "summary": amended["summary"], "decision_id": amended["decision"]["id"]}


def _ask_owner_question(ctx, ticket: str, project: str, run_id: str, cls: str,
                        question: str, decision: dict) -> dict:
    if cls not in PERSON_CLASSES:
        return {"action": "refused", "class": cls,
                "reason": f"{LABELS.get(cls, cls)} never reaches a person: answer it (DECISION: ANSWER) or amend the ticket"}
    try:
        asked = ask(ctx, ticket, project, question, decision.get("why", ""), decision.get("options") or [],
                    decision.get("recommended", ""), decision.get("topic", ""), decision.get("keywords") or [])
    except ValueError as exc:
        return {"action": "refused", "class": cls, "reason": str(exc)}
    row = asked["decision"]
    if asked["action"] == "applied":
        settle(ctx.db, ticket, project, "triage", run_id, TAKEN, question, "decision already taken", "register", row["id"],
               row["decision"])
        return {"action": "applied", "class": TAKEN, "citation": asked["citation"], "answer": row["decision"],
                "decision_id": row["id"]}
    if asked["action"] == "waiting":
        resolution, reason = "waiting", "already asked; waiting"
    else:
        resolution, reason = "person", decision.get("why", "")
    prepared = escalation_repository.prepare_owner_handoff(ctx.db, ticket, run_id, cls, reason, row["id"])
    if not prepared:
        return {"action": "refused", "class": cls, "reason": "the open escalation changed during the owner hand-off"}
    return {"action": asked["action"], "class": cls, "citation": asked["citation"], "decision_id": row["id"],
            "options": row["options"], "recommended": row["recommended"], "resolution": resolution,
            "grouped": row.get("ticket_key") != ticket}


def pending_answer(db, ticket: str) -> dict | None:
    """The newest answer an escalation of this ticket received without a triage run (register, policy, machine,
    amendment): the next developer run gets the question and the answer in its prompt."""
    return escalation_repository.pending_answer(db, ticket)


# --- metrics (AC7) ----------------------------------------------------------------------------------------------
def week_start(value: datetime) -> datetime:
    utc = value.astimezone(timezone.utc)
    return datetime.combine(utc.date() - timedelta(days=utc.weekday()), datetime.min.time(), tzinfo=timezone.utc)


def weekly_metrics(db, weeks: int = ACTIVE_WINDOW_WEEKS, now: datetime | None = None) -> dict[str, Any]:
    """Escalations per week by class and project, the share resolved without a person and the top three causes."""
    now = now or datetime.now(timezone.utc)
    this_week = week_start(now)
    first = this_week - timedelta(weeks=weeks - 1)
    rows = escalation_repository.metrics_since(db, first)
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
    amendments = escalation_repository.recent_amendments(db, first)
    return {"weeks": trend, "classes": list(CLASSES), "labels": LABELS, "person_classes": list(PERSON_CLASSES),
            "this_week": current, "falling": falling, "top_causes": top,
            "amendments": [{**a, "created_at": a["created_at"].isoformat()} for a in amendments]}


def register(db) -> dict[str, list[dict]]:
    """The register for Command Center: decisions, open questions and amendments."""
    rows = escalation_repository.register_entries(db)
    def public(row: dict) -> dict:
        return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
    resuming = escalation_repository.resuming_register_entries(db)
    asked = [public({**row, "jira_answer_required": escalation_repository.requires_jira_owner_answer(
        db, row["id"], list(row.get("waiting_tickets") or []))})
        for row in rows if row["status"] == "asked"]
    return {"decisions": [public(r) for r in rows if r["kind"] == "decision" and r["status"] == "decided"],
            "asked": asked,
            "resuming": [public(r) for r in resuming],
            "amendments": [public(r) for r in rows if r["kind"] == "amendment"]}
