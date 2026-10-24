"""Run each criterion's configured check after its ticket is merged (docs/v4/environment.md)."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from ..adapters.base import RunSpec
from .client import ControlClient
from .dev import Claimed, Heartbeat, LeaseLost
from .gitops import Git
from .notes import project_env


def _redact(text: str, env: dict[str, str], secret_names: set[str] | None = None) -> str:
    secret_names = secret_names or set()
    values = {value for value in env.values() if len(value) >= 4}
    values.update(env[name] for name in secret_names if env.get(name))
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return re.sub(r"(?i)(token|password|secret|api[_-]?key)(\s*[=:]\s*)[^\s,;]+", r"\1\2[redacted]", text)


def run_post_merge(api: ControlClient, c: Claimed, srv_root: str, heartbeat_s: int,
                   run_as: list[str]) -> dict:
    """Execute only commands selected from the project's configured check list and report per-criterion results."""
    project = c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    env = project_env(project["key"], project.get("environment"), "post_merge")
    secret_names = {str(item.get("name") or "") for item in project.get("environment") or []
                    if item.get("kind") == "secret" and item.get("name")}
    report: dict = {"head_sha": "", "checks": [], "error": ""}
    try:
        criteria = c.project.get("post_merge_criteria") or []
        commands = list(dict.fromkeys(str(item.get("check") or "") for item in criteria if item.get("check")))
        configured = set(project.get("checks") or [])
        if not commands or any(command not in configured for command in commands):
            raise RuntimeError("a deferred criterion has no mapped configured post-merge check")
        hb.start()
        credential = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                               "repo": project["repo"]})
        mirror = git.mirror(project["repo"], credential["token"], credential.get("transport_url", ""))
        branch = project["base"]
        if not git.remote_branch_exists(mirror, branch):
            raise RuntimeError("the configured project branch is not available for post-merge checks")
        worktree = Path(srv_root) / "work" / project["key"] / f"{c.ticket['key']}--post-merge"
        head = git.detached(mirror, worktree, f"origin/{branch}")
        report["head_sha"] = head
        command_env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                       "HOME": str(Path(srv_root) / "cache"), "LANG": "C.UTF-8", **env}
        timeout_s = max(60, int((project.get("work_minutes") or {}).get("hard", 60)) * 60)
        for index, command in enumerate(commands, start=1):
            if hb.lost.is_set():
                raise LeaseLost()
            hb.progress = {"phase": "post-merge-check", "ticket": c.ticket["key"],
                           "check": index, "checks_total": len(commands)}
            if project.get("containers"):
                from .containers import ContainerRuntime
                spec = RunSpec(prompt="", cwd=str(worktree), account_home=command_env["HOME"], extra_env=command_env)
                with ContainerRuntime(spec, run_as) as runtime:
                    proc = subprocess.Popen(run_as + ["bash", "-c", command], cwd=worktree,
                                            env={**command_env, **spec.extra_env}, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
                    runtime.bind_process(proc.pid)
                    output, exit_code = _communicate(proc, timeout_s)
            else:
                proc = subprocess.Popen(run_as + ["bash", "-lc", command], cwd=worktree, env=command_env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        start_new_session=True)
                output, exit_code = _communicate(proc, timeout_s)
            report["checks"].append({"command": command, "passed": exit_code == 0, "exit_code": exit_code,
                                     "output": _redact(output[-12_000:], env, secret_names)})
        if hb.lost.is_set():
            raise LeaseLost()
        outcome = api.call("POST", "/v1/environment/post-merge/complete",
                           {"run_id": c.run_id, "fence": c.fence, **report})
        results = outcome.get("results") or []
        passed = bool(results) and all(item.get("status") == "passed" for item in results)
        return {"fence": c.fence, "outcome": "done" if passed else "failed",
                "summary": "per-criterion post-merge checks " + ("passed" if passed else "failed"),
                "head_sha": head, "details": {"post_merge_results": results, "checks": report["checks"]}}
    except LeaseLost:
        return {"fence": c.fence, "outcome": "lost", "summary": "post-merge lease lost; stopped without recording"}
    except Exception as exc:  # noqa: BLE001
        report["error"] = _redact(f"{type(exc).__name__}: {exc}", env, secret_names)[:1000]
        try:
            api.call("POST", "/v1/environment/post-merge/complete",
                     {"run_id": c.run_id, "fence": c.fence, **report})
        except Exception:
            pass
        return {"fence": c.fence, "outcome": "failed", "summary": f"post-merge check failed: {report['error']}"[:1000],
                "head_sha": report["head_sha"], "details": {"checks": report["checks"]}}
    finally:
        hb.stop.set()
        if hb.ident:
            hb.join(timeout=heartbeat_s + 1)


def _communicate(proc: subprocess.Popen, timeout_s: int) -> tuple[str, int]:
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        output = (output or "") + f"\nCheck exceeded {timeout_s} seconds."
    return output or "", int(proc.returncode or 0)
