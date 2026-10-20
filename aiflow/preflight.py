"""Read-only readiness check of THIS machine for AI Flow.

    bin/aiflow-preflight [--skip-ai] [--lock FILE] [--json]

Checks: the worktree directory of each company, the configuration files, a repository clone for every project file, reachability of Redis,
OpenObserve and the memory service, the tools (and their versions against an optional lock file of `name=version` lines) and the sign-in of
the AI CLIs. It changes nothing; the exit code is 1 when a check fails."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from . import audit_common as common

TOOLS = ("git", "gh", "node", "uv", "claude", "codex")


def reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def version(tool: str) -> str:
    try:
        out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out[0] if out else ""


def version_matches(wanted: str, text: str) -> bool:
    """The locked version as a whole number group: `2.5` matches `2.5.1` but not `2.53.0`."""
    return re.search(rf"(?<![\d.]){re.escape(wanted)}(?!\d)", text) is not None


def read_lock(path: Path) -> dict[str, str]:
    wanted = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" in raw and not raw.strip().startswith("#"):
            name, value = raw.split("=", 1)
            wanted[name.strip()] = value.strip()
    return wanted


def ai_login(tool: str) -> tuple[bool, str]:
    commands = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"]}
    try:
        result = subprocess.run(commands[tool], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"cannot run: {error.__class__.__name__}"
    text = result.stdout + result.stderr
    ok = result.returncode == 0 and ('"loggedIn": true' in text or "Logged in" in text)
    return ok, "signed in" if ok else "not signed in"


def run(root: Path, skip_ai: bool = False, lock: Path | None = None, check_reach=reachable) -> list[dict]:
    results: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": ok, "detail": detail})

    env, runtime, projects = common.env_values(root), common.runtime(root), common.projects(root)
    cfg = common.config_dir(root)
    add("config: aiflow.toml", (cfg / "aiflow.toml").is_file(), str(cfg / "aiflow.toml"))
    add("config: project files", bool(projects), ", ".join(p["jira_key"] for p in projects) or "none found")
    for company in sorted({p.get("company", "JOOservices") for p in projects}):
        add(f"worktree directory: {company}", (root / company / "worktree").is_dir(), str(root / company / "worktree"))
    for project in projects:
        if not project.get("repo"):
            add(f"clone of {project['jira_key']}", False, "project file has no `repo` key")
            continue
        clone = root / project.get("company", "JOOservices") / "projects" / project["repo"]
        add(f"clone of {project['jira_key']} ({project['repo']})", (clone / ".git").exists(), str(clone))
    redis_host, redis_port = env.get("AIFLOW_REDIS_HOST", "127.0.0.1"), int(env.get("AIFLOW_REDIS_PORT", "16382") or 16382)
    add("Redis reachable", check_reach(redis_host, redis_port), f"{redis_host}:{redis_port}")
    for name, key in (("OpenObserve", "obs_url"), ("memory service", "memory_url")):
        url = runtime.get(key, "")
        if not url:
            add(f"{name} configured", False, f"{key} is empty")
            continue
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        add(f"{name} reachable", check_reach(parsed.hostname or "", port), f"{parsed.hostname}:{port}")
    wanted = read_lock(lock) if lock else {}
    for tool in TOOLS:
        found = shutil.which(tool) is not None
        text = version(tool) if found else ""
        ok = found and (tool not in wanted or version_matches(wanted[tool], text))
        add(f"tool: {tool}", ok, (text or "missing") + (f" (lock wants {wanted[tool]})" if tool in wanted and not ok and found else ""))
    if not skip_ai:
        for tool in ("claude", "codex"):
            if shutil.which(tool):
                ok, detail = ai_login(tool)
                add(f"AI login: {tool}", ok, detail)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only readiness check of this machine for AI Flow.")
    parser.add_argument("--skip-ai", action="store_true", help="do not call the AI CLIs to check their sign-in")
    parser.add_argument("--lock", type=Path, help="file of name=version lines the tools must match")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = run(common.workspaces_root(), args.skip_ai, args.lock)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for item in results:
            print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['check']:<42} {item['detail']}")
        failed = [item for item in results if not item["ok"]]
        print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
