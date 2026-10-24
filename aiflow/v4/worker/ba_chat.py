"""Run a channel-neutral BA conversation turn through the assigned AI account."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..adapters import RunSpec, get_adapter
from .dev import REPO_ROOT


def build_prompt(job: dict) -> str:
    template = (REPO_ROOT / "prompts" / "v4" / "ba-chat.md").read_text(encoding="utf-8")
    return (template + "\n\nJira project: " + str(job.get("project_key") or "") +
            "\nCurrent proposal (empty when no proposal exists):\n" +
            json.dumps(job.get("proposal") or {}, ensure_ascii=False) +
            "\n\nConversation, oldest first; return one response for the latest owner message:\n" +
            json.dumps(job.get("history") or [], ensure_ascii=False))


def parse_output(text: str) -> dict | None:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(candidate[start:end + 1])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def run_turn(api, job: dict, account: str, srv_root: str, control_url: str,
             run_as: list[str] | None = None) -> dict:
    """Return the worker's small, non-secret result envelope for the control API."""
    spec_info = api.call("GET", f"/v1/accounts/{account}/spec")
    work = Path(srv_root) / "work" / ".ba-chat" / str(job["job_id"])
    work.mkdir(parents=True, exist_ok=True)
    if run_as:
        try:
            shutil.chown(work, "aiflow-worker", "aiflow-worker")
        except (LookupError, PermissionError):
            pass
    if not (work / ".git").exists():
        argv = run_as + ["git", "init", "-q", str(work)] if run_as else ["git", "init", "-q", str(work)]
        subprocess.run(argv, capture_output=True, timeout=30, check=False)
    spec = RunSpec(prompt=build_prompt(job), cwd=str(work),
                   account_home=str(Path(srv_root) / "accounts" / account), model=spec_info.get("model", ""),
                   effort=spec_info.get("effort", ""), timeout_s=600, idle_s=180,
                   control_url=control_url, sandbox="read-only")
    adapter = get_adapter(spec_info["provider"])
    if run_as:
        base_argv = adapter.argv
        adapter.argv = lambda current: run_as + base_argv(current)
    try:
        response = adapter.run(spec)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if response.error_kind:
        return {"account": account, "error_kind": response.error_kind, "output": {}}
    parsed = parse_output(response.final_text)
    if not parsed:
        return {"account": account, "error_kind": "invalid_response", "output": {}}
    return {"account": account, "error_kind": "", "output": parsed}
