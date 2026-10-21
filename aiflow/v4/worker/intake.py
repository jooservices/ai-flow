"""The intake step: an intake_refiner account rewrites a ticket in the Intake status into its issue-type template
without losing the author's intent.

The AI reads the ticket and the project spec (docs/SPEC.md on the project's base branch, when present).

Outcomes:
  refined    original description kept as a comment, description replaced, aiflow-ready added, ticket in To Do
  questions  the AI needs answers first: questions in a comment, ticket in Needs owner
  quota      the account cannot work now; the ticket stays in Intake for another account
  failed     anything else (the ticket stays in Intake and is retried after a short wait)
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, api_node
from .gitops import Git
from .notes import note

log = logging.getLogger("aiflow4.intake")
KIND = re.compile(r"^INTAKE:\s*(REWRITE|QUESTIONS)\b", re.M)
DESCRIPTION = re.compile(r"^DESCRIPTION:\s*\n<<<\s*\n(.*?)\n>>>\s*$", re.M | re.S)
QUESTIONS = re.compile(r"^QUESTIONS:\s*\n((?:[ \t]*[-*].*(?:\n|$))+)", re.M)
SPEC_CHARS = 20_000
# The common template headings and the additions for each supported Jira issue type. Keep these in the same order
# as docs/v4/issue-types.md; unknown Jira types deliberately use the Story template without changing the Jira type.
COMMON_SECTIONS = ("Business requirement:", "Acceptance criteria:", "Technical notes:", "Definition of done:")
ISSUE_TYPE_SECTIONS = {
    "Epic": (),
    "Story": (),
    "Feature": (),
    "Bug": ("Steps to reproduce:", "Expected result:", "Actual result:", "Environment:"),
    "Hotfix": ("Steps to reproduce:", "Expected result:", "Actual result:", "Environment:"),
    "Tech Debt": ("Problem:", "Risk:"),
    "Chore": (),
    "Spike": ("Question:", "Timebox:", "Output:"),
    "Docs": ("Reader:",),
    "Security": ("Threat:", "Impact:"),
    "Sub-task": (),
}
ISSUE_TYPE_BY_CASEFOLD = {name.casefold(): name for name in ISSUE_TYPE_SECTIONS}
LABELS = (
    "aiflow-ready", "aiflow-hold", "rework", "require-human", "tier-senior", "ai-<provider>",
    "area-<core|theme|plugin|platform|flow>", "regression", "uat-bug", "blocked-external", "needs-design",
    "breaking-change", "perf", "a11y", "seo", "security",
)
KNOWN_FIXED_LABELS = frozenset(label for label in LABELS if "<" not in label)
AI_PROVIDER_LABEL = re.compile(r"^ai-[a-z0-9][a-z0-9-]*$", re.I)
AREA_LABELS = frozenset(f"area-{area}" for area in ("core", "theme", "plugin", "platform", "flow"))
CRITERIA = re.compile(r"^[ \t]*Acceptance criteria:(.*?)(?=^[ \t]*Technical notes:|\Z)", re.M | re.S | re.I)
AC1 = re.compile(r"^[ \t]*(?:[-*][ \t]*)?AC1\s*[:.)-]", re.M)


def effective_issue_type(issue_type: str | None) -> str:
    """Return a supported Jira type, falling back to Story for types such as the default Task."""
    return ISSUE_TYPE_BY_CASEFOLD.get(str(issue_type or "").strip().casefold(), "Story")


def required_sections(issue_type: str | None) -> tuple[str, ...]:
    """Ordered required headings for a Jira type (or the Story template for an unknown type)."""
    kind = effective_issue_type(issue_type)
    additions = ISSUE_TYPE_SECTIONS[kind]
    # Type-specific context follows the business requirement; the four shared sections retain their order.
    return (COMMON_SECTIONS[0], *additions, *COMMON_SECTIONS[1:])


def is_known_label(label: str) -> bool:
    """Recognize the AI Flow labels while allowing Jira's other free-form labels to remain untouched."""
    return label in KNOWN_FIXED_LABELS or label in AREA_LABELS or bool(AI_PROVIDER_LABEL.fullmatch(label))


def template_gaps(description: str, issue_type: str | None = "Story") -> list[str]:
    """What a rewrite lacks of the template (empty when it follows it)."""
    gaps, pos = [], 0
    for heading in required_sections(issue_type):
        m = re.search(r"^[ \t]*" + re.escape(heading), description[pos:], re.M | re.I)
        if not m:
            gaps.append(f"'{heading}' (missing or out of order)")
            continue
        pos += m.end()
    criteria = CRITERIA.search(description)
    if not criteria or not AC1.search(criteria.group(1)):
        gaps.append("acceptance criteria starting at AC1")
    return gaps


def parse(text: str, issue_type: str | None = "Story") -> tuple[str, str]:
    """AI answer -> ("rewrite", description) | ("questions", bullet list) | ("invalid", what the rewrite lacks)
    | ("", "") when neither is usable."""
    text = text or ""
    kinds = KIND.findall(text)
    kind = kinds[-1] if kinds else ""
    d, q = DESCRIPTION.search(text), QUESTIONS.search(text)
    if kind == "REWRITE" and d and d.group(1).strip():
        gaps = template_gaps(d.group(1).strip(), issue_type)
        if gaps:
            return "invalid", "the rewrite does not follow the template; it lacks " + ", ".join(gaps)
        return "rewrite", d.group(1).strip()
    if kind == "QUESTIONS" and q:
        return "questions", q.group(1).strip()
    return "", ""


def read_spec(worktree: Path | None) -> str:
    f = worktree / "docs" / "SPEC.md" if worktree else None
    if not f or not f.is_file():
        return "(the repository has no docs/SPEC.md)"
    text = f.read_text(encoding="utf-8", errors="replace").strip()
    return text[:SPEC_CHARS] + ("\n(truncated)" if len(text) > SPEC_CHARS else "")


def build_prompt(c: Claimed, spec: str) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "intake.md").read_text(encoding="utf-8")
    t = c.ticket
    comments = [f"- {x.get('created', '')} {x.get('author', 'unknown')}: {x.get('body', '')}" if isinstance(x, dict)
                else f"- {x}" for x in c.recent_comments or []]
    issue_type = effective_issue_type(t.get("type"))
    labels = list(t.get("labels") or [])
    label_context = ", ".join(f"{label} ({'recognized' if is_known_label(label) else 'unrecognized; preserve'})"
                               for label in labels) or "(none)"
    outline = "\n".join(_outline_section(section) for section in required_sections(issue_type))
    return tpl.format(key=t["key"], project=t["project"], summary=t["summary"], base=c.project["base"], spec=spec,
                      issue_type=issue_type, original_issue_type=t.get("type") or "Task",
                      template_outline=outline, label_context=label_context,
                      description=t["description"] or "(no description)",
                      comments="\n".join(comments) or "(no comments)")


def _outline_section(section: str) -> str:
    """Give the model concise guidance for the dynamic headings in its required template."""
    if section == "Business requirement:":
        return f"{section} <why the change is needed and for whom>"
    if section == "Acceptance criteria:":
        return f"{section}\n- AC1: <one observable, testable behaviour>\n- AC2: ..."
    if section == "Technical notes:":
        return f"{section} <code location, constraints and project rules>"
    if section == "Definition of done:":
        return f"{section}\n- <tests, checks and documentation needed for this ticket>"
    prompts = {
        "Steps to reproduce:": "<reproducible steps>", "Expected result:": "<what should happen>",
        "Actual result:": "<what happens now>", "Environment:": "<affected environment and versions>",
        "Question:": "<question the spike must answer>", "Timebox:": "<time limit>",
        "Output:": "<decision, findings or prototype expected>", "Problem:": "<existing maintainability problem>",
        "Risk:": "<risk of leaving the problem in place>", "Threat:": "<security threat or attack path>",
        "Impact:": "<security impact if exploited>", "Reader:": "<intended reader>",
    }
    return f"{section} {prompts[section]}"


def run_intake(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed"}

    def jira(path: str, **body) -> dict:
        return api.call("POST", f"/v1/jira/{key}/{path}", {"run_id": c.run_id, "fence": c.fence, **body})

    try:
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence, "repo": project["repo"]})["token"]
        mirror = git.mirror(project["repo"], token)
        wt = Path(srv_root) / "work" / project["key"] / f"{key}--intake"
        head = git.detached(mirror, wt, f"origin/{project['base']}")
        hb.progress = {"phase": "intake", "head": head[:10]}
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c, read_spec(wt)), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                       timeout_s=int(project.get("work_minutes", {}).get("target", 30)) * 60, control_url=control_url,
                       run_token=c.run_token, log_path=str(logs / f"{key}-{c.run_id}.jsonl"))
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda s: run_as + base_argv(s)
        events = {"n": 0}

        def on_progress(ev: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "intake-ai", "events": events["n"], "elapsed_s": int(time.time() - t0)}

        res = adapter.run(spec, on_progress, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      details={"exit_code": res.exit_code, "duration_s": int(res.duration_s), "log": res.log_path})
        if hb.lost.is_set():
            raise LeaseLost()
        if res.error_kind in ("quota", "auth"):
            result.update(outcome="quota", summary=f"{c.account} cannot work now ({res.error_kind}): {res.error_message}"[:500])
            return result
        kind, text = parse(res.final_text or "", c.ticket.get("type"))
        if kind == "rewrite":
            return refined(c, jira, result, text, t0, api_node(api))
        if kind == "questions":
            return questions(c, jira, result, text, t0, api_node(api))
        if kind == "invalid":
            result["summary"] = text[:1000]
            return result
        result["summary"] = (f"no usable answer ({res.error_kind or 'missing INTAKE line'}): "
                             f"{(res.error_message or (res.final_text or '').strip())[-400:]}")[:1000]
        return result
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during intake; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("intake step failed")
        result.update(outcome="failed", summary=f"intake worker error: {e}"[:1000])
        return result
    finally:
        hb.stop.set()


def refined(c: Claimed, jira, result: dict, description: str, t0: float | None, node: str) -> dict:
    """Keep the author's text as a comment first, then replace the description, mark it ready and queue it."""
    original = (c.ticket.get("description") or "").strip() or "(empty)"
    jira("comment", body=note(c, "original description kept", what="The ticket is rewritten into the AI Flow template",
                              why="The description lacked parts of the template; this comment keeps the author's words",
                              result="Original description:\n\n" + original, t0=t0, node=node))
    jira("description", body=description)
    jira("fields", add_labels=["aiflow-ready"])
    jira("transition", to="To Do")
    result.update(outcome="refined", summary=f"description rewritten ({len(description)} chars); ticket ready in To Do")
    result.setdefault("details", {})["description_chars"] = len(description)
    return result


def questions(c: Claimed, jira, result: dict, text: str, t0: float | None, node: str) -> dict:
    jira("comment", body=note(c, "question for the owner", what="The ticket could not be rewritten into the template without guessing",
                              result="Questions:\n" + text,
                              next_="Answer in the description (or a comment the description points to) and move the ticket back to Intake",
                              t0=t0, node=node))
    jira("transition", to="Needs owner")
    result.update(outcome="questions", summary=("questions for the owner:\n" + text)[:1500])
    result.setdefault("details", {})["questions"] = text
    return result
