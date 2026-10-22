"""Run a project's check commands once on a clean checkout (docs/v4/missing-tools.md).

The control plane asks for a run when a project is added, when this machine joins and when the commands change
(`desired.check_probes` in the heartbeat answer). The node clones the base branch into a scratch directory with a
short-lived read-only token, runs every command as the unprivileged AI user and reports for each one whether it
could run. A command that failed its own assertions still ran; only a missing executable "cannot run", and the
Projects screen shows it before any ticket is started.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..worker.gitops import Git
from ..worker.notes import project_env

COMMAND_TIMEOUT_S = 900
NOT_FOUND = re.compile(r"(?:^|[\s:])([\w.+/-]+): (?:command )?not found|exec: \"([^\"]+)\": executable file not found",
                       re.M)
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def command_tool(command: str) -> str:
    """The executable a shell command starts with (environment assignments skipped); '' when it is not a tool."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return ""
    for word in words:
        if ASSIGNMENT.match(word):
            continue
        return "" if "/" in word else word  # ./vendor/bin/phpunit exists only after the install step ran
    return ""


def probe_command(command: str, cwd: Path, env: dict[str, str], run_as: list[str]) -> dict[str, Any]:
    tool = command_tool(command)
    if tool and tool not in {"cd", "export", "source", "set", "test", "echo", "true", "false"} \
            and shutil.which(tool, path=env.get("PATH")) is None:
        return {"command": command, "status": "cannot_run", "tool": tool, "detail": f"{tool} is not installed"}
    try:
        done = subprocess.run(run_as + ["bash", "-c", command], cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"command": command, "status": "ran", "detail": f"still running after {COMMAND_TIMEOUT_S} s"}
    missing = NOT_FOUND.search(done.stderr or "")
    if done.returncode == 127 or (missing and done.returncode != 0):
        name = (missing.group(1) or missing.group(2)) if missing else tool
        return {"command": command, "status": "cannot_run", "tool": os.path.basename(name or tool),
                "detail": ((done.stderr or "").strip().splitlines() or ["command not found"])[-1][:200]}
    return {"command": command, "status": "ran", "detail": f"exit {done.returncode}"}


def run_check_probe(api, node: str, item: dict[str, Any], srv_root: str, run_as: list[str]) -> list[dict[str, Any]]:
    """Check every repository of the project on a fresh clone; returns one result per command."""
    git = Git(srv_root, run_as=run_as)
    results: list[dict[str, Any]] = []
    base = Path(srv_root) / "work" / ".probe"
    git.mkdirs(base)
    for repo in item["repositories"]:
        scratch = Path(tempfile.mkdtemp(prefix="checks-", dir=str(base)))
        if run_as:
            try:
                shutil.chown(scratch, "aiflow-worker", "aiflow-worker")
            except (LookupError, PermissionError):
                pass
        try:
            token = api.call("POST", f"/v1/nodes/{node}/check-probes/token",
                             {"project": item["project"], "repo": repo["repo"]})["token"]
            checkout = scratch / "checkout"
            git.run("clone", "--quiet", "--depth", "1", "--branch", repo["base"],
                    f"https://github.com/{repo['repo']}.git", str(checkout), token=token)
            env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(scratch),
                   "LANG": "C.UTF-8", "TERM": "dumb", "CI": "1", **project_env(item["project"])}
            for command in repo["checks"]:
                results.append({"repo": repo["repo"], **probe_command(command, checkout, env, run_as)})
        except Exception as exc:  # noqa: BLE001 - the checkout failed: report the commands as not run
            for command in repo["checks"]:
                results.append({"repo": repo["repo"], "command": command, "status": "not_run",
                                "detail": f"clean checkout failed: {type(exc).__name__}"})
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return results


def run_check_probes(api, node: str, items: list[dict[str, Any]], srv_root: str, run_as: list[str]) -> list[str]:
    """Run and report each requested probe; returns the projects whose report reached the control plane."""
    reported = []
    for item in items:
        results = run_check_probe(api, node, item, srv_root, run_as)
        if all(r.get("status") == "not_run" for r in results):
            continue  # nothing could be checked (no clone): ask again later instead of recording an empty result
        api.call("POST", f"/v1/nodes/{node}/check-probes",
                 {"project": item["project"], "signature": item["signature"], "results": results}, retries=2)
        reported.append(item["project"])
    return reported
