"""Triage a developer question from a detached, read-only checkout of the project's base branch."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..core import work_limits
from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, api_node, run_with_inbox, with_knowledge_context
from .gitops import Git
from .notes import note

log = logging.getLogger("aiflow4.triage")
DECISION = re.compile(r"^DECISION:\s*(ANSWER|ESCALATE|AMEND)\b", re.I | re.M)
FIELD = re.compile(r"^(ANSWER|WHY|CLASS|OPTIONS|RECOMMENDED|TOPIC|KEYWORDS|CRITERION|NARROWED):[ \t]*", re.I | re.M)
ESCALATION_CLASSES = ("missing_tool_or_environment", "policy_message", "unreachable_criterion", "ambiguity_repository",
                      "decision_already_taken", "new_business_decision", "irreversible_or_production")


def build_prompt(c: Claimed) -> str:
    """Render the ticket history and require a machine-readable, owner-safe decision."""
    tpl = (REPO_ROOT / "prompts" / "v4" / "triage.md").read_text(encoding="utf-8")
    comments = []
    for item in c.recent_comments or []:
        if isinstance(item, dict):
            comments.append(f"- {item.get('created', '')} {item.get('author', 'unknown')}: {item.get('body', '')}")
        else:
            comments.append(f"- {item}")
    prompt = tpl.format(key=c.ticket["key"], project=c.ticket["project"], summary=c.ticket["summary"],
                      description=c.ticket.get("description") or "(no description)", base=c.project["base"],
                      repo=c.project["repo"], question=c.last_dev_summary or "(no summary recorded)",
                      verify_findings=c.verify_findings or "(none)",
                      register="\n".join(f"- D{d['id']} {d['title']}: {d['decision']}" for d in c.owner_register)
                      or "(empty)", escalation_class=c.escalation_class or "not classified",
                      comments="\n".join(comments) or "(no recent comments)")
    return with_knowledge_context(prompt, c.context_pack)


def _fields(content: str) -> dict[str, str]:
    """Each `NAME:` field of the decision block up to the next field (the first occurrence wins)."""
    marks = list(FIELD.finditer(content))
    out: dict[str, str] = {}
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        out.setdefault(mark.group(1).lower(), content[mark.end():end].strip())
    return out


def _items(value: str) -> list[str]:
    """A list field: one item per line, or separated by `;` or `|`; bullets and numbers are dropped."""
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", part).strip() for part in re.split(r"[\n;|]", value or "")]
    return [i for i in items if i]


def parse_decision(text: str) -> dict | None:
    """Accept only the explicit format of the triage prompt:

    ANSWER    ANSWER + WHY
    AMEND     CRITERION + WHY (+ NARROWED): the criterion cannot be met here, so triage narrows or removes it
    ESCALATE  WHY + OPTIONS (at least two) + RECOMMENDED (+ CLASS, TOPIC, KEYWORDS): a new decision is asked once
    """
    decisions = list(DECISION.finditer(text or ""))
    if not decisions:
        return None
    decision = decisions[-1]
    kind = decision.group(1).upper()
    f = _fields((text or "")[decision.end():].strip())
    why = f.get("why", "")
    cls = f.get("class", "").strip().lower().replace(" ", "_")
    out = {"decision": kind, "answer": f.get("answer", ""), "why": why, "class": cls if cls in ESCALATION_CLASSES else "",
           "options": [], "recommended": "", "topic": f.get("topic", ""),
           "keywords": [k.strip() for k in re.split(r"[,;\n]", f.get("keywords", "")) if k.strip()],
           "criterion": "", "narrowed": f.get("narrowed", "")}
    if kind == "ANSWER":
        return out if out["answer"] and why else None
    if kind == "AMEND":
        criterion = re.search(r"AC\s?\d+", f.get("criterion", ""), re.I)
        out["criterion"] = criterion.group(0).replace(" ", "").upper() if criterion else ""
        return out if out["criterion"] and why else None
    out["options"], out["recommended"] = _items(f.get("options", "")), f.get("recommended", "").strip()
    return out if (out["class"] in ESCALATION_CLASSES and why and len(out["options"]) >= 2
                   and out["recommended"]) else None


def safe_directory_env(path: Path) -> dict[str, str]:
    """Git config for the AI's git commands in the root-owned checkout (command-line scope, like `git -c`)."""
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": str(path)}


def run_triage(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed"}
    mirror: Path | None = None
    wt = Path(srv_root) / "work" / project["key"] / f"{key}--triage-{c.run_id}"

    def jira(path: str, **body) -> dict:
        return api.call("POST", f"/v1/jira/{key}/{path}", {"run_id": c.run_id, "fence": c.fence, **body})

    try:
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                              "repo": project["repo"]})
        token = credential["token"]
        mirror = git.mirror(project["repo"], token, credential.get("transport_url", ""))
        head = git.readonly_worktree(mirror, wt, f"origin/{project['base']}")
        hb.progress = {"phase": "triage", "base": project["base"], "head": head[:10]}
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model,
                       effort=c.effort, timeout_s=int(project.get("work_minutes", {}).get("hard", work_limits.HARD)) * 60,
                       control_url=control_url, run_token=c.run_token,
                       log_path=str(logs / f"{key}-{c.run_id}.jsonl"), extra_env=safe_directory_env(wt))
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda s: run_as + base_argv(s)
        events = {"n": 0}

        def on_progress(_event: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "triage-ai", "events": events["n"],
                           "elapsed_s": int(time.time() - t0)}

        res = run_with_inbox(adapter, spec, hb, on_progress)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      quota_observed_at=res.quota_observed_at.isoformat() if res.quota_observed_at else None,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      head_sha=head)
        if hb.lost.is_set():
            raise LeaseLost()
        if res.error_kind in ("quota", "auth"):
            result.update(outcome="quota", summary=f"triage account {c.account} cannot work now ({res.error_kind})")
            return result
        if res.error_kind:
            outcome = "timeout" if res.error_kind == "timeout" else "failed"
            result.update(outcome=outcome, error_kind=res.error_kind,
                          summary=f"triage stopped before a decision ({res.error_kind}): {res.error_message[:400]}")
            return result
        decision = parse_decision(res.final_text or "")
        if not decision:
            result["summary"] = "triage returned no valid DECISION: ANSWER or DECISION: ESCALATE response"
            result["details"] = {"decision": "", "output": (res.final_text or "")[-1500:],
                                 "exit_code": res.exit_code, "log": res.log_path}
            return result

        result["details"] = {"decision": decision["decision"], "answer": decision["answer"],
                             "why": decision["why"], "exit_code": res.exit_code,
                             "duration_s": int(res.duration_s), "log": res.log_path,
                             "base": project["base"], "head_sha": head, "class": decision["class"]}
        question = c.last_dev_summary or "See the last dev run"
        reviewer_question = c.last_dev_summary.startswith("Reviewer question:")

        def post(text: str) -> None:
            jira("comment", body=text)
            if c.pr and c.pr.get("number"):
                api.call("POST", "/v1/github/comment", {"run_id": c.run_id, "fence": c.fence,
                                                          "number": c.pr["number"], "body": text, "kind": "triage"})

        if reviewer_question and decision["decision"] == "ANSWER":  # a reviewer's question is not an escalation
            answer_note = note(c, "answered reviewer question", what=f"The question was: {question}",
                               why=decision["why"], result=decision["answer"],
                               next_="Back to Human review; the reviewer can continue with the answer",
                               t0=t0, node=api_node(api))
            post(answer_note)
            jira("transition", to="Human review")
            result.update(outcome="answered", summary=decision["answer"][:1000])
            return result

        # the register decides what happens: a decision already taken is applied, a criterion is amended, a new
        # kind of decision is asked once with its options, and a class that never needs a person is refused
        verdict = api.call("POST", "/v1/escalations/triage", {
            "run_id": c.run_id, "fence": c.fence, "decision": decision["decision"], "question": question,
            "answer": decision["answer"], "why": decision["why"], "escalation_class": decision["class"],
            "options": decision["options"], "recommended": decision["recommended"], "topic": decision["topic"],
            "keywords": decision["keywords"], "criterion": decision["criterion"], "narrowed": decision["narrowed"]})
        action = verdict["action"]
        result["details"].update(action=action, escalation_class=verdict.get("class", ""))
        if action == "refused":
            result["summary"] = f"triage decision refused: {verdict.get('reason', '')}"[:1000]
            return result
        if action == "answered":
            post(note(c, "answered developer question", what=f"The question was: {question}",
                      why=decision["why"], result=decision["answer"],
                      next_="Back to To Do; development can continue with this answer", t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            result.update(outcome="answered", summary=decision["answer"][:1000])
        elif action in ("applied", "amended"):
            if action == "applied":
                outcome_text = verdict.get("answer", "")
                headline, why = "answered from the owner decision register", f"{verdict['citation']} already covers this question"
            else:
                outcome_text = (f"{verdict['summary']}: the criterion cannot be met in this environment. "
                                f"{decision['why']} The amendment is on the ticket and in the owner register.")
                headline, why = "criterion amended", f"{verdict['citation']}; it appears in the weekly report for review"
            post(note(c, headline, what=f"The question was: {question}", why=why, result=outcome_text,
                      next_="Back to To Do; development continues, no one is blocked", t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            result.update(outcome="answered", summary=outcome_text[:1000])
        else:
            options = "; ".join(verdict.get("options") or [])
            if action == "waiting":
                post(note(c, "waiting for the owner decision", what=f"The question was: {question}",
                          why=f"The same question was already asked once: {verdict['citation']}. It is not sent again.",
                          result=f"Options: {options}. Recommended default: {verdict.get('recommended', '')}",
                          next_="The ticket continues when the owner answers that question", t0=t0, node=api_node(api)))
            else:
                post(note(c, "owner decision required", what=f"The question was: {question}",
                          why=f"{decision['why']} ({verdict['citation']})",
                          result=f"Options: {options}. Recommended default: {verdict.get('recommended', '')}",
                          next_="The owner decides in Command Center (or comments on the ticket); the answer becomes a "
                                "register entry and is applied from then on", t0=t0, node=api_node(api)))
                api.call("POST", "/v1/notify/triage", {
                    "run_id": c.run_id, "fence": c.fence,
                    "detail": f"{decision['why']}\nOptions: {options}\nRecommended default: {verdict.get('recommended', '')}"})
            jira("fields", add_labels=["owner-escalated"])
            result.update(outcome="escalated", summary=decision["why"][:1000])
        return result
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during triage; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("triage step failed")
        result.update(outcome="failed", summary=f"triage worker error: {e}"[:1000])
        return result
    finally:
        hb.stop.set()
        if mirror is not None and wt.exists():
            try:
                git.remove_worktree(mirror, wt)
            except Exception as e:  # noqa: BLE001
                log.warning("could not remove triage checkout %s: %s", key, e)
