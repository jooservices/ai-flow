"""The intake step: an intake_refiner account rewrites a ticket in the Intake status into the template (business
requirement, acceptance criteria AC1.., technical notes, definition of done) without losing the author's intent.

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
# The template a rewrite must follow: these headings at the start of a line, in this order, and AC1 onwards.
SECTIONS = ("Business requirement:", "Acceptance criteria:", "Technical notes:", "Definition of done:")
CRITERIA = re.compile(r"^[ \t]*Acceptance criteria:(.*?)(?=^[ \t]*Technical notes:|\Z)", re.M | re.S | re.I)
AC1 = re.compile(r"^[ \t]*(?:[-*][ \t]*)?AC1\s*[:.)-]", re.M)


def template_gaps(description: str) -> list[str]:
    """What a rewrite lacks of the template (empty when it follows it)."""
    gaps, pos = [], 0
    for heading in SECTIONS:
        m = re.search(r"^[ \t]*" + re.escape(heading), description[pos:], re.M | re.I)
        if not m:
            gaps.append(f"'{heading}' (missing or out of order)")
            continue
        pos += m.end()
    criteria = CRITERIA.search(description)
    if not criteria or not AC1.search(criteria.group(1)):
        gaps.append("acceptance criteria starting at AC1")
    return gaps


def parse(text: str) -> tuple[str, str]:
    """AI answer -> ("rewrite", description) | ("questions", bullet list) | ("invalid", what the rewrite lacks)
    | ("", "") when neither is usable."""
    text = text or ""
    kinds = KIND.findall(text)
    kind = kinds[-1] if kinds else ""
    d, q = DESCRIPTION.search(text), QUESTIONS.search(text)
    if kind == "REWRITE" and d and d.group(1).strip():
        gaps = template_gaps(d.group(1).strip())
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
    return tpl.format(key=t["key"], project=t["project"], summary=t["summary"], base=c.project["base"], spec=spec,
                      description=t["description"] or "(no description)")


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
        kind, text = parse(res.final_text or "")
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
