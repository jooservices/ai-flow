"""Read-only status of every AI Flow machine in one view.

    bin/aiflow-fleet --host wsl=local --host vm='ssh:-p 2261 ubuntu@172.23.64.1'

`name=local` runs the snapshot on this machine; `name=ssh:<ssh arguments>` runs it over SSH (key login, BatchMode). The snapshot only reads:
load, memory, disk, the aiflow systemd user units, AI processes and the error count of the last hour in the journal. A machine with a failed or
missing unit, a stale checkout or error lines is flagged."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from typing import Any

SNAPSHOT = r'''
set -u
echo "host=$(hostname)"
echo "load=$(cut -d' ' -f1 /proc/loadavg)"
echo "cpus=$(nproc)"
echo "mem_used_gb=$(free -g | awk '/^Mem:/{print $3}')"
echo "mem_total_gb=$(free -g | awk '/^Mem:/{print $2}')"
echo "disk_used_pct=$(df -P / | awk 'NR==2{gsub("%","",$5);print $5}')"
echo "units_active=$(systemctl --user list-units 'aiflow-*' --no-legend --all 2>/dev/null | awk '$3=="active"{n++} END{print n+0}')"
echo "units_failed=$(systemctl --user list-units 'aiflow-*' --no-legend --all 2>/dev/null | awk '$3=="failed"{n++} END{print n+0}')"
echo "control=$(systemctl --user is-active aiflow-control 2>/dev/null || true)"
echo "ai_processes=$(ps -eo args | grep -E '^(claude|codex|grok|opencode)( |$)' | wc -l)"
echo "errors_1h=$(journalctl --user -u 'aiflow-*' --since '-1h' --no-pager -q 2>/dev/null | grep -ciE 'traceback|worker.error|tick failed|ConfigError')"
echo "commit=$(git -C "$HOME/Workspaces/JOOservices/projects/ai-flow" log -1 --format=%h 2>/dev/null)"
'''


def parse(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def flags(snapshot: dict[str, str], newest_commit: str = "") -> list[str]:
    found = []
    if not snapshot:
        return ["no answer"]
    if int(snapshot.get("units_failed", "0") or 0):
        found.append("failed aiflow units")
    if snapshot.get("control") != "active":
        found.append(f"control is {snapshot.get('control') or 'missing'}")
    if int(snapshot.get("errors_1h", "0") or 0):
        found.append(f"{snapshot['errors_1h']} error lines in the last hour")
    if int(snapshot.get("disk_used_pct", "0") or 0) >= 85:
        found.append("disk almost full")
    if newest_commit and snapshot.get("commit") and snapshot["commit"] != newest_commit:
        found.append(f"code differs ({snapshot['commit']} vs {newest_commit})")
    return found


def collect(spec: str, runner=subprocess.run) -> dict[str, str]:
    name, _, how = spec.partition("=")
    if how == "local":
        command = ["bash", "-s"]
    elif how.startswith("ssh:"):
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *shlex.split(how[4:]), "bash", "-s"]
    else:
        raise ValueError(f"host `{spec}` must be name=local or name=ssh:<arguments>")
    result = runner(command, input=SNAPSHOT, capture_output=True, text=True, timeout=90)
    values = parse(result.stdout) if result.returncode == 0 else {}
    values["name"] = name
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only status of every AI Flow machine.")
    parser.add_argument("--host", action="append", required=True, help="name=local or name=ssh:<ssh arguments>")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    snapshots = [collect(spec) for spec in args.host]
    commits = [s.get("commit", "") for s in snapshots if s.get("commit")]
    newest = max(set(commits), key=commits.count) if commits else ""
    rows: list[dict[str, Any]] = [{"snapshot": s, "flags": flags({k: v for k, v in s.items() if k != "name"}, newest)} for s in snapshots]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            s = row["snapshot"]
            print(f"{s['name']:<8} load {s.get('load', '?')}/{s.get('cpus', '?')}  mem {s.get('mem_used_gb', '?')}/{s.get('mem_total_gb', '?')} GB  "
                  f"disk {s.get('disk_used_pct', '?')}%  units {s.get('units_active', '?')} active, {s.get('units_failed', '?')} failed  "
                  f"AI procs {s.get('ai_processes', '?')}  errors/1h {s.get('errors_1h', '?')}  commit {s.get('commit', '?')}")
            print("         " + ("OK" if not row["flags"] else "FLAGS: " + "; ".join(row["flags"])))
    return 1 if any(row["flags"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
