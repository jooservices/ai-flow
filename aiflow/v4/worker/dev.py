"""The dev step: worktree from the latest base, AI run, then the worker commits, pushes and opens a draft PR.

Outcomes:
  done     changes committed and pushed, draft PR open, ticket in In Verify
  blocked  the AI reported a requirement conflict: ticket to Needs owner with the question
  quota    the account cannot work now: ticket back to To Do, account cooling down (control API)
  failed   anything else: ticket back to To Do with the reason (the branch keeps the work for the next run)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from ..core.text import slug
from .client import Conflict, ControlClient
from .gitops import Git

log = logging.getLogger("aiflow4.dev")
REPO_ROOT = Path(__file__).resolve().parents[3]
SUMMARY = re.compile(r"^SUMMARY:\s*(.+)$", re.M)
TESTS = re.compile(r"^TESTS:\s*(.+)$", re.M)
PROVIDER_NAME = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "opencode": "OpenCode"}


@dataclass
class Claimed:
    run_id: str
    fence: int
    run_token: str
    account: str
    provider: str
    model: str
    effort: str
    ticket: dict
    project: dict
    identity: tuple[str, str]


class LeaseLost(Exception):
    pass


class Heartbeat(threading.Thread):
    def __init__(self, api: ControlClient, run: Claimed, every_s: int) -> None:
        super().__init__(daemon=True, name=f"hb-{run.ticket['key']}")
        self.api, self.run_, self.every = api, run, every_s
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.progress: dict = {}

    def run(self) -> None:
        while not self.stop.wait(self.every):
            try:
                self.api.call("POST", f"/v1/runs/{self.run_.run_id}/heartbeat",
                              {"fence": self.run_.fence, "progress": self.progress})
            except Conflict:
                self.lost.set()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)


def build_prompt(c: Claimed, branch: str) -> str:
    tpl = (REPO_ROOT / "prompts" / "v4" / "dev.md").read_text(encoding="utf-8")
    checks = "\n".join(f"  - {x}" for x in c.project.get("checks") or []) or "  - the project's test suite"
    t = c.ticket
    return tpl.format(key=t["key"], project=t["project"], summary=t["summary"], repo=c.project["repo"], branch=branch,
                      base=c.project["base"], checks=checks, description=t["description"] or "(no description)")


def commit_message(c: Claimed, summary: str) -> str:
    title = c.ticket["summary"].strip().rstrip(".")
    title = title[0].upper() + title[1:] if title else c.ticket["key"]
    kind = "fix" if c.ticket.get("type") == "Bug" else "feat"
    head = f"{kind}: {title}"[:100]
    return (f"{head}\n\n{summary}\n\nRefs: {c.ticket['key']}\nAI-Run: {c.run_id} ({c.account}, {c.provider}"
            f"{'/' + c.model if c.model else ''})\n")


def run_dev(api: ControlClient, c: Claimed, srv_root: str, control_url: str, heartbeat_s: int,
            run_as: list[str]) -> dict:
    key, project = c.ticket["key"], c.project
    repo = project["repo"]
    branch = f"{key}-{slug(c.ticket['summary'])}"
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    t0 = time.time()
    result: dict = {"fence": c.fence, "outcome": "failed", "branch": branch}

    def jira(path: str, **body) -> dict:
        return api.call("POST", f"/v1/jira/{key}/{path}", {"run_id": c.run_id, "fence": c.fence, **body})

    try:
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence, "repo": repo})["token"]
        mirror = git.mirror(repo, token)
        wt = Path(srv_root) / "work" / project["key"] / branch
        start = git.worktree(mirror, wt, branch, project["base"])
        hb.progress = {"phase": "worktree", "start": start}
        jira("transition", to="In Progress")
        jira("fields", fields={"Workspace": f"{c.account}@{api_node(api)}",
                               "Agent (AI)": {"value": PROVIDER_NAME.get(c.provider, c.provider)}})
        jira("comment", body=f"AI Flow: {c.account} ({c.provider}{'/' + c.model if c.model else ''}) started on branch "
                             f"{branch} from {start}. Run {c.run_id}, fencing token {c.fence}.")
        logs = Path(srv_root) / "logs" / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = RunSpec(prompt=build_prompt(c, branch), cwd=str(wt),
                       account_home=str(Path(srv_root) / "accounts" / c.account), model=c.model, effort=c.effort,
                       timeout_s=int(project.get("work_minutes", {}).get("hard", 60)) * 60, control_url=control_url,
                       run_token=c.run_token, log_path=str(logs / f"{key}-{c.run_id}.jsonl"))
        adapter = get_adapter(c.provider)
        adapter_argv = adapter.argv
        adapter.argv = lambda s: run_as + adapter_argv(s)  # the AI runs as the unprivileged AI user

        events = {"n": 0}

        def on_progress(ev: dict) -> None:
            events["n"] += 1
            hb.progress = {"phase": "ai", "events": events["n"], "last": str(ev.get("type", ""))[:40],
                           "elapsed_s": int(time.time() - t0)}

        res = adapter.run(spec, on_progress, stop=hb.lost)
        result.update(tokens_in=res.tokens_in, tokens_out=res.tokens_out, cost_usd=res.cost_usd, quota=res.quota,
                      error_kind=res.error_kind, reset_at=res.reset_at.isoformat() if res.reset_at else None,
                      details={"verdict": res.verdict, "permission_denials": res.permission_denials,
                               "exit_code": res.exit_code, "duration_s": int(res.duration_s), "log": res.log_path})
        if hb.lost.is_set():
            raise LeaseLost()
        summary_m, tests_m = SUMMARY.search(res.final_text or ""), TESTS.search(res.final_text or "")
        summary = summary_m.group(1).strip() if summary_m else ((res.final_text or "").strip()[-600:] or res.error_message)
        tests = tests_m.group(1).strip() if tests_m else ""
        result["summary"] = summary

        if res.error_kind in ("quota", "auth"):
            result["outcome"] = "quota"
            jira("comment", body=f"AI Flow: {c.account} cannot work now ({res.error_kind}). The ticket goes back to To Do"
                                 f" for another account.")
            jira("transition", to="To Do")
            return result
        if res.verdict == "BLOCKED":
            result["outcome"] = "blocked"
            jira("comment", body=f"AI Flow: the developer stopped with a question.\n\n{res.verdict_reason}")
            jira("transition", to="Needs owner")
            return result
        changed = git.changed(wt)
        if res.error_kind or not changed:
            reason = res.error_kind or ("no changes" if not changed else "")
            result["outcome"] = "failed"
            result["summary"] = f"{reason}: {summary}"[:1000]
            jira("comment", body=f"AI Flow: the run ended without a usable change ({reason}). Back to To Do."
                                 + (f"\n\nLast output:\n{summary[:800]}" if summary else ""))
            jira("transition", to="To Do")
            return result
        hb.progress = {"phase": "commit", "files": len(changed)}
        sha = git.commit_all(wt, commit_message(c, summary), c.identity)
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence, "repo": repo})["token"]
        git.push(wt, branch, token)
        body = (f"Jira: {key}\n\n## Summary\n{summary}\n\n## Tests\n{tests or 'see the run log'}\n\n"
                f"## Run\n- account: {c.account} ({c.provider}{'/' + c.model if c.model else ''})\n"
                f"- run: {c.run_id}, fence {c.fence}\n- files changed: {len(changed)}\n\n"
                "Opened as a draft by AI Flow v4; verification runs next (In Verify).")
        pr = api.call("POST", "/v1/github/pr", {"run_id": c.run_id, "fence": c.fence, "repo": repo, "head": branch,
                                                "base": project["base"], "title": f"{key}: {c.ticket['summary']}"[:120],
                                                "body": body, "draft": True})
        jira("comment", body=f"AI Flow: work pushed ({sha[:10]}, {len(changed)} files) and draft PR opened: {pr['url']}\n\n"
                             f"{summary}\n\nTests: {tests or 'not reported'}")
        jira("transition", to="In Verify")
        result.update(outcome="done", pr_url=pr["url"], head_sha=sha)
        return result
    except LeaseLost:
        result.update(outcome="lost", summary="lease lost during the run; stopped without writing")
        return result
    except Conflict:
        result.update(outcome="lost", summary="fencing token refused; another run owns the ticket")
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("dev step failed")
        result.update(outcome="failed", summary=f"worker error: {e}"[:1000])
        try:
            jira("comment", body=f"AI Flow: the worker hit an error and put the ticket back in To Do: {str(e)[:500]}")
            jira("transition", to="To Do")
        except Exception:  # noqa: BLE001
            pass
        return result
    finally:
        hb.stop.set()


def api_node(api: ControlClient) -> str:
    return getattr(api, "node_name", "node")
