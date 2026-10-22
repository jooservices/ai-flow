"""Run configured full project checks on a version-and-source-specific release branch."""
from __future__ import annotations

import logging
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

log = logging.getLogger("aiflow4.version_release")


def _redact(text: str, env: dict[str, str]) -> str:
    for value in sorted((v for v in env.values() if len(v) >= 4), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return re.sub(r"(?i)(token|password|secret|api[_-]?key)(\s*[=:]\s*)[^\s,;]+", r"\1\2[redacted]", text)


def run_version_release(api: ControlClient, c: Claimed, version: dict, tracker_key: str,
                        srv_root: str, heartbeat_s: int, run_as: list[str]) -> dict:
    """Check develop and ask the fenced control API to re-evaluate gates and publish the release."""
    project = c.project
    git = Git(srv_root, run_as=run_as)
    hb = Heartbeat(api, c, heartbeat_s)
    hb.start()
    started = time.monotonic()
    report: dict = {"head_sha": "", "source_sha": str(project.get("source_sha") or ""), "checks": [], "error": ""}
    env = project_env(project["key"])
    try:
        token = api.call("POST", "/v1/github/token", {"run_id": c.run_id, "fence": c.fence,
                                                        "repo": project["repo"]})["token"]
        mirror = git.mirror(project["repo"], token)
        branch = project.get("release_branch") or project["source_branch"]
        if not git.remote_branch_exists(mirror, branch):
            raise RuntimeError(f"configured release branch {branch} does not exist")
        wt = Path(srv_root) / "work" / project["key"] / f"version-release-{version['id']}"
        head = git.detached(mirror, wt, f"origin/{branch}")
        report["head_sha"] = head
        report["source_sha"] = report["source_sha"] or head
        commands = list(project.get("checks") or [])
        if not commands:
            raise RuntimeError("no full project checks are configured for this release")

        command_env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                       "HOME": str(Path(srv_root) / "cache"), "LANG": "C.UTF-8", **env}
        timeout_s = max(60, int((project.get("work_minutes") or {}).get("hard", 60)) * 60)
        for index, command in enumerate(commands, start=1):
            if hb.lost.is_set():
                raise LeaseLost()
            hb.progress = {"phase": "version-release-check", "version": version["name"], "head": head[:12],
                           "check": index, "checks_total": len(commands), "elapsed_s": int(time.monotonic() - started)}
            if project.get("containers"):
                from .containers import ContainerRuntime
                spec = RunSpec(prompt="", cwd=str(wt), account_home=str(Path(srv_root) / "cache"),
                               extra_env=dict(command_env))
                with ContainerRuntime(spec, run_as) as runtime:
                    proc = subprocess.Popen(run_as + ["bash", "-c", command], cwd=wt,
                                            env={**command_env, **spec.extra_env}, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
                    runtime.bind_process(proc.pid)
                    try:
                        output, _ = proc.communicate(timeout=timeout_s)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        output, _ = proc.communicate()
                        output = (output or "") + f"\nCheck exceeded {timeout_s} seconds."
            else:
                proc = subprocess.Popen(run_as + ["bash", "-lc", command], cwd=wt, env=command_env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        start_new_session=True)
                try:
                    output, _ = proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    output, _ = proc.communicate()
                    output = (output or "") + f"\nCheck exceeded {timeout_s} seconds."
            clean_output = _redact(output or "", env)[-12_000:]
            report["checks"].append({"command": command, "passed": proc.returncode == 0,
                                     "exit_code": proc.returncode, "output": clean_output})
        if hb.lost.is_set():
            raise LeaseLost()
        result = api.call("POST", f"/v1/version-releases/{version['id']}/complete",
                          {"run_id": c.run_id, "fence": c.fence, **report})
        return {"outcome": "done" if result.get("released") else "failed",
                "summary": f"version release {result.get('status', 'failed')}: {version['name']}",
                "head_sha": head, "details": {"tracker_key": tracker_key, "version": version,
                                                "checks": report["checks"], "release_result": result}}
    except LeaseLost:
        return {"outcome": "lost", "summary": "version release lease lost; stopped without publishing",
                "head_sha": report["head_sha"], "details": {"tracker_key": tracker_key}}
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, subprocess.TimeoutExpired):
            message = f"project check timed out: {exc}"
        else:
            message = f"{type(exc).__name__}: {exc}"
        message = _redact(message, env)[:2000]
        report["error"] = message
        try:
            result = api.call("POST", f"/v1/version-releases/{version['id']}/complete",
                              {"run_id": c.run_id, "fence": c.fence, **report})
        except Exception as report_exc:  # noqa: BLE001
            log.warning("version release check report failed: %s", type(report_exc).__name__)
            result = {"released": False, "status": "failed", "reason": message}
        return {"outcome": "failed", "summary": f"version release failed: {message}"[:1000],
                "head_sha": report["head_sha"], "details": {"tracker_key": tracker_key, "version": version,
                                                              "checks": report["checks"], "release_result": result}}
    finally:
        hb.stop.set()
        hb.join(timeout=heartbeat_s + 1)
