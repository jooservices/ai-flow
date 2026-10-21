"""Node agent (systemd unit aiflow4-node-agent, root): join, heartbeat with metrics, worker supervision.

Heartbeat every 15 s carries CPU, memory, disk, load, tool versions and the state of every worker on the
node; the control plane marks the node offline after missed heartbeats. The answer lists the accounts this
node should run; the agent starts and stops aiflow4-worker@<account> units to match.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .. import VERSION
from ..worker.client import ControlClient
from ..worker.loop import STATE_DIR

log = logging.getLogger("aiflow4.node")
TOOLS = {"claude": ["claude", "--version"], "codex": ["codex", "--version"], "grok": ["grok", "--version"],
         "opencode": ["opencode", "--version"], "docker": ["docker", "--version"], "git": ["git", "--version"],
         "node": ["node", "--version"], "uv": ["uv", "--version"], "gh": ["gh", "--version"]}


def tool_versions() -> dict[str, str]:
    out = {}
    for name, cmd in TOOLS.items():
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out[name] = (p.stdout or p.stderr).strip().splitlines()[0][:80] if (p.stdout or p.stderr) else "?"
        except (OSError, subprocess.TimeoutExpired):
            out[name] = "missing"
    return out


def _cpu_times() -> tuple[int, int]:
    parts = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    return sum(parts), idle


def metrics(prev: tuple[int, int] | None) -> tuple[dict, tuple[int, int]]:
    total, idle = _cpu_times()
    cpu = None
    if prev:
        dt, di = total - prev[0], idle - prev[1]
        cpu = round(100.0 * (1 - di / dt), 1) if dt > 0 else None
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        mem[k] = int(v.strip().split()[0]) * 1024
    limit = None
    for f in ("/sys/fs/cgroup/memory.max",):
        try:
            v = Path(f).read_text().strip()
            limit = int(v) if v.isdigit() else None
        except OSError:
            pass
    used = None
    try:
        used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
    except OSError:
        pass
    du = shutil.disk_usage("/srv/aiflow" if os.path.isdir("/srv/aiflow") else "/")
    load = os.getloadavg()
    m = {"cpu_pct": cpu, "load1": round(load[0], 2), "load5": round(load[1], 2), "cpus": os.cpu_count(),
         "mem_total": limit or mem.get("MemTotal"), "mem_used": used if used is not None else mem.get("MemTotal", 0) - mem.get("MemAvailable", 0),
         "disk_total": du.total, "disk_free": du.free, "disk_free_pct": round(100.0 * du.free / du.total, 1)}
    return m, (total, idle)


def worker_states() -> list[dict]:
    out = []
    if STATE_DIR.is_dir():
        for f in STATE_DIR.glob("*.json"):
            try:
                s = json.loads(f.read_text())
                s["alive"] = s.get("pid") and Path(f"/proc/{s['pid']}").exists()
                out.append(s)
            except (OSError, ValueError):
                pass
    return out


def systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], capture_output=True).returncode


def reconcile(accounts: list[str]) -> None:
    """Start workers for desired accounts that have signed-in credentials; stop the others."""
    for a in accounts:
        if systemctl("is-active", "--quiet", f"aiflow4-worker@{a}") != 0:
            if Path(f"/srv/aiflow/accounts/{a}").is_dir():
                systemctl("enable", "--now", f"aiflow4-worker@{a}")
                log.info("started worker %s", a)
    p = subprocess.run(["systemctl", "list-units", "--plain", "--no-legend", "aiflow4-worker@*"],
                       capture_output=True, text=True)
    for line in p.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        acc = unit.removeprefix("aiflow4-worker@").removesuffix(".service")
        if acc and acc not in accounts:
            systemctl("disable", "--now", unit)
            log.info("stopped worker %s (not desired here)", acc)


def run_agent(control_url: str, name: str, token: str, slots: int = 2, every_s: int = 15, manage_workers: bool = True,
              stop: threading.Event | None = None) -> None:
    api = ControlClient(control_url, token, timeout=15)
    stop = stop or threading.Event()
    tools, tools_at, prev = tool_versions(), time.time(), None
    while not stop.is_set():
        try:
            m, prev = metrics(prev)
            if time.time() - tools_at > 600:
                tools, tools_at = tool_versions(), time.time()
            ws = worker_states()
            used = sum(1 for w in ws if w.get("state") == "working" and w.get("alive"))
            desired = api.call("POST", f"/v1/nodes/{name}/heartbeat", {
                "slots_total": slots, "slots_used": used, "release_version": VERSION, "tools": tools,
                "metrics": {**m, "hostname": socket.gethostname()}, "workers": ws}, retries=2)["desired"]
            if manage_workers:
                reconcile(desired.get("accounts", []))
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat failed: %s", e)
        stop.wait(every_s)
