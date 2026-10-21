"""Triage a developer question from a detached, read-only checkout of the project's base branch."""
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

log = logging.getLogger("aiflow4.triage")
DECISION = re.compile(r"^DECISION:\s*(ANSWER|ESCALATE)\b", re.I | re.M)
ANSWER = re.compile(r"^ANSWER:\s*(.*?)(?=^WHY:|\Z)", re.I | re.M | re.S)
WHY = re.compile(r"^WHY:\s*(.*)\Z", re.I | re.M | re.S)


def build_prompt(c: Claimed) -> str:
    """Render the ticket history and require a machine-readable, owner-safe decision."""
    tpl = (REPO_ROOT / "prompts" / "v4" / "triage.md").read_text(encoding="utf-8")
    comments = []
    for item in c.recent_comments or []:
        if isinstance(item, dict):
            comments.append(f"- {item.get('created', '')} {item.get('author', 'unknown')}: {item.get('body', '')}")
        else:
            comments.append(f"- {item}")
    return tpl.format(key=c.ticket["key"], project=c.ticket["project"], summary=c.ticket["summary"],
                      description=c.ticket.get("description") or "(no description)", base=c.project["base"],
                      repo=c.project["repo"], question=c.last_dev_summary or "(no summary recorded)",
                      verify_findings=c.verify_findings or "(none)",
                      comments="\n".join(comments) or "(no recent comments)")


def parse_decision(text: str) -> dict[str, str] | None:
    """Accept only the explicit ANSWER/ESCALATE format from the triage prompt."""
    decisions = list(DECISION.finditer(text or ""))
    if not decisions:
        return None
    decision = decisions[-1]
    content = (text or "")[decision.end():].strip()
    why = WHY.search(content)
    if decision.group(1).upper() == "ANSWER":
        answer = ANSWER.search(content)
        if not answer or not answer.group(1).strip() or not why or not why.group(1).strip():
            return None
        return {"decision": "ANSWER", "answer": answer.group(1).strip(), "why": why.group(1).strip()}
    if not why or not why.group(1).strip():
        return None
    return {"decision": "ESCALATE", "answer": "", "why": why.group(1).strip()}


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
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                         "repo": project["repo"]})["token"]
        mirror = git.mirror(project["repo"], token)
        head = git.readonly_worktree(mirror, wt, f"origin/{project['base']}")
        hb.progress = {"phase": "triage", "base": project["base"], "head": head[:10]}
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model,
                       effort=c.effort, timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60,
                       control_url=control_url, run_token=c.run_token,
                       log_path=str(logs / f"{key}-{c.run_id}.jsonl"))
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda s: run_as + base_argv(s)
        events = {"n": 0}

        def on_progress(_event: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "triage-ai", "events": events["n"],
                           "elapsed_s": int(time.time() - t0)}

        res = adapter.run(spec, on_progress, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
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
                             "base": project["base"], "head_sha": head}
        if decision["decision"] == "ANSWER":
            jira("comment", body=note(c, "answered developer question",
                                       what=f"The developer asked: {c.last_dev_summary or 'See the last dev run'}",
                                       why=decision["why"], result=decision["answer"],
                                       next_="Back to To Do; development can continue with this answer",
                                       t0=t0, node=api_node(api)))
            jira("transition", to="To Do")
            result.update(outcome="answered", summary=decision["answer"][:1000])
        else:
            jira("comment", body=note(c, "owner decision required",
                                       what=f"The developer asked: {c.last_dev_summary or 'See the last dev run'}",
                                       why=decision["why"], result="No safe answer was found in the ticket, spec, or code.",
                                       next_="The owner must decide and comment on the ticket",
                                       t0=t0, node=api_node(api)))
            api.call("POST", "/v1/notify/triage", {"run_id": c.run_id, "fence": c.fence,
                                                     "detail": decision["why"]})
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
