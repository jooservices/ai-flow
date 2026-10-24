"""Fixed host, systemd and AI Flow container log sources used by the Command Center viewer.

The control API constructs the visible inventory from registered nodes. Node agents receive only a source kind
and an identifier from that inventory, then map it to one of these fixed journalctl or Docker argv forms.
"""
from __future__ import annotations

import re
import subprocess
from typing import Any

MAX_LOG_LINES = 100
MAX_LOG_LINE_CHARS = 4_000
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_WORKER_UNIT = re.compile(r"aiflow4-worker@[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.service\Z")

# These are the fixed service containers in deploy/v4/control/compose.yaml and infra.yaml.
CONTROL_CONTAINERS = (
    "aiflow4-control-api-a", "aiflow4-control-api-b", "aiflow4-control-loops", "aiflow4-postgres",
    "aiflow4-redis", "aiflow4-traefik", "aiflow4-openobserve", "aiflow4-gatus", "aiflow4-svc-mailpit",
)


def source_id(node: str, kind: str, target: str) -> str:
    """Stable public identifier for an item in the server-built source inventory."""
    return f"{node}:{kind}:{target}"


def sources_for_node(node: str, role: str, accounts: list[Any], available: bool) -> list[dict[str, Any]]:
    """Describe the host journal, AI Flow units and fixed control containers for one registered node."""
    sources = [{"id": source_id(node, "host", "host"), "node": node, "kind": "host", "target": "host",
                "name": "Host journal", "available": available}]
    sources.append({"id": source_id(node, "unit", "aiflow4-node-agent.service"), "node": node, "kind": "unit",
                    "target": "aiflow4-node-agent.service",
                    "name": "Node agent · aiflow4-node-agent.service", "available": available})
    account_ids = sorted({str(account.id) for account in accounts if getattr(account, "node", "") == node
                          and _SAFE_ACCOUNT.fullmatch(str(getattr(account, "id", "")))})
    for account_id in account_ids:
        unit = f"aiflow4-worker@{account_id}.service"
        sources.append({"id": source_id(node, "unit", unit), "node": node, "kind": "unit", "target": unit,
                        "name": f"Worker · {account_id}", "available": available})
    if role == "control":
        for container in CONTROL_CONTAINERS:
            sources.append({"id": source_id(node, "container", container), "node": node, "kind": "container",
                            "target": container,
                            "name": f"Container · {container}", "available": available})
    return sources


def read_source(kind: str, target: str, lines: int = MAX_LOG_LINES) -> dict[str, Any]:
    """Read recent lines from one source shape; refuse every unit or container outside the fixed allowlist."""
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= MAX_LOG_LINES:
        return {"available": False, "lines": []}
    if kind == "host" and target == "host":
        command = ["journalctl", "--no-pager", "--output=short-iso", f"--lines={lines}"]
    elif kind == "unit" and (target == "aiflow4-node-agent.service" or _WORKER_UNIT.fullmatch(target)):
        command = ["journalctl", "--no-pager", "--output=short-iso", f"--lines={lines}", f"--unit={target}"]
    elif kind == "container" and target in CONTROL_CONTAINERS:
        command = ["docker", "logs", "--tail", str(lines), "--timestamps", target]
    else:
        return {"available": False, "lines": []}

    try:
        # Docker routes container stderr separately; combine it with stdout so service errors appear in the viewer.
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "lines": []}
    if result.returncode:
        return {"available": False, "lines": []}
    recent = (result.stdout or "").splitlines()[-lines:]
    return {"available": True, "lines": [line[:MAX_LOG_LINE_CHARS] for line in recent]}
