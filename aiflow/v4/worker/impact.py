"""The impact step: an intake_refiner account assesses a change request (label change-request, status Backlog) sent
from the Business view, before the owner decides whether it enters Intake.

The AI reads the request, the project's open work and docs/SPEC.md on the base branch, and answers with the affected
epics and tickets, a size (S, M or L), risks and conflicts with work under way. The worker posts that as one Jira
comment and reports it in the run details (details.impact), which the Business view shows. It never rewrites the
description and never moves the ticket: the control API refuses transitions and field writes of an impact run.

Outcomes:
  assessed  the comment is posted; the ticket stays in Backlog for the owner
  quota     the account cannot work now; another account assesses it later
  failed    no usable answer (retried after a short wait)
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import REPO_ROOT, Claimed, Heartbeat, LeaseLost, api_node, with_knowledge_context
from .gitops import Git
from .intake import read_spec
from .notes import note

log = logging.getLogger("aiflow4.impact")
DONE = re.compile(r"^IMPACT:\s*DONE\b", re.M)
SIZE = re.compile(r"^SIZE:\s*([SML])\b", re.M | re.I)
KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+")
BULLETS = r"^{0}:[ \t]*\n((?:[ \t]*[-*].*(?:\n|$))+)"


def _keys(text: str, label: str) -> list[str] | None:
    """Parse one required issue-key field; None means missing or malformed."""
    m = re.search(rf"^{label}:[ \t]*(.*?)[ \t]*$", text, re.M)
    if not m:
        return None
    value = m.group(1).strip()
    if value.lower() == "none":
        return []
    items = [item.strip() for item in value.split(",")]
    if not items or any(not KEY.fullmatch(item) for item in items):
        return None
    return list(dict.fromkeys(items))


def _bullets(text: str, label: str) -> list[str] | None:
    m = re.search(BULLETS.format(label), text, re.M)
    if not m:
        return None
    items = [re.sub(r"^[ \t]*[-*][ \t]*", "", line).strip() for line in m.group(1).splitlines()]
    return [i for i in items if i and i.lower().rstrip(".") != "none"]


def parse(text: str) -> dict | None:
    """AI answer -> {"size", "epics", "tickets", "risks", "conflicts"}, or None when the answer is not usable."""
    text = text or ""
    size = SIZE.search(text)
    epics, tickets = _keys(text, "EPICS"), _keys(text, "TICKETS")
    risks, conflicts = _bullets(text, "RISKS"), _bullets(text, "CONFLICTS")
    if not DONE.search(text) or not size or epics is None or tickets is None or risks is None or conflicts is None:
        return None
    return {"size": size.group(1).upper(), "epics": epics, "tickets": tickets,
            "risks": risks, "conflicts": conflicts}


def render(impact: dict) -> str:
    """The assessment as comment text (the same fields the Business view shows)."""
    def lines(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- none"
    return (f"Size: {impact['size']}\n"
            f"Affected epics: {', '.join(impact['epics']) or 'none'}\n"
            f"Affected tickets: {', '.join(impact['tickets']) or 'none'}\n"
            f"Risks:\n{lines(impact['risks'])}\n"
            f"Conflicts with work under way:\n{lines(impact['conflicts'])}")


def board(rows: list[dict]) -> str:
    return "\n".join(f"- {r['key']} · {r.get('type', '')} · {r.get('status', '')} · epic {r.get('parent_key') or '-'}"
                     f" · {r.get('summary', '')}" for r in rows) or "(no open work)"


def build_prompt(c: Claimed, spec: str) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "impact.md").read_text(encoding="utf-8")
    t = c.ticket
    prompt = tpl.format(key=t["key"], project=t["project"], summary=t["summary"], base=c.project["base"], spec=spec,
                      description=t["description"] or "(no description)", epic=t.get("parent_key") or "(none)",
                      board=board(c.project_tickets))
    return with_knowledge_context(prompt, c.context_pack)


def run_impact(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
               run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed"}
    try:
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence, "repo": project["repo"]})["token"]
        mirror = git.mirror(project["repo"], token)
        wt = Path(srv_root) / "work" / project["key"] / f"{key}--impact"
        head = git.detached(mirror, wt, f"origin/{project['base']}")
        hb.progress = {"phase": "impact", "head": head[:10]}
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c, read_spec(wt)), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                       timeout_s=int(project.get("work_minutes", {}).get("target", 30)) * 60, control_url=control_url,
                       run_token=c.run_token, log_path=str(logs / f"{key}-{c.run_id}.jsonl"))
        adapter = get_adapter(c.provider)
        base_argv = adapter.argv
        adapter.argv = lambda s: run_as + base_argv(s)
        res = adapter.run(spec, lambda ev: None, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      details={"exit_code": res.exit_code, "duration_s": int(res.duration_s), "log": res.log_path})
        if hb.lost.is_set():
            raise LeaseLost()
        if res.error_kind in ("quota", "auth"):
            result.update(outcome="quota", summary=f"{c.account} cannot work now ({res.error_kind}): {res.error_message}"[:500])
            return result
        impact = parse(res.final_text or "")
        if not impact:
            result["summary"] = (f"no usable assessment ({res.error_kind or 'missing IMPACT: DONE, SIZE, RISKS or CONFLICTS'}): "
                                 f"{(res.error_message or (res.final_text or '').strip())[-400:]}")[:1000]
            return result
        api.call("POST", f"/v1/jira/{key}/comment", {"run_id": c.run_id, "fence": c.fence, "body": note(
            c, "change request impact", what="The change request is assessed before the owner decides on it",
            result=render(impact), next_="The ticket stays in Backlog; the owner moves it to Intake to go ahead",
            t0=t0, node=api_node(api))})
        result.update(outcome="assessed", summary=f"impact {impact['size']}: {len(impact['epics'])} epics,"
                      f" {len(impact['tickets'])} tickets, {len(impact['conflicts'])} conflicts")
        result["details"]["impact"] = impact
        return result
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during the impact assessment; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("impact step failed")
        result.update(outcome="failed", summary=f"impact worker error: {e}"[:1000])
        return result
    finally:
        hb.stop.set()
