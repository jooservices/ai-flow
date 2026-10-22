"""Prepare the test services of the projects a node serves (docs/v4/test-services.md).

For every declared service the agent inspects its Docker container and fixes what it finds: a missing container is
created, a stopped one is started (or recreated when it will not start), then the service is health-checked. The
result per service is reported in each heartbeat; nothing here raises, so one broken service never stops the agent.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from ..core.services import HOST, SERVICES, endpoint

log = logging.getLogger("aiflow4.node")
Runner = Callable[..., Any]


def _docker(run: Runner, *args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        r = run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, type(exc).__name__
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _run_args(spec: dict[str, Any]) -> list[str]:
    args = ["run", "-d", "--name", spec["container"], "--restart", "unless-stopped",
            "--memory", spec["memory"], "-p", f"{HOST}:{spec['port']}:{spec['internal_port']}"]
    for key, value in spec["env"].items():
        args += ["-e", f"{key}={value}"]
    return args + [spec["image"], *spec["command"]]


def _healthy(run: Runner, name: str) -> tuple[bool, str]:
    spec = SERVICES[name]
    if name == "postgres":
        code, out = _docker(run, "exec", spec["container"], "pg_isready", "-U", "aiflow", "-d", "aiflow4", timeout=30)
        if code:
            return False, "PostgreSQL does not accept connections yet"
        # template1 carries the extension too, so every per-session test database created from it has it
        for database in ("aiflow4", "template1"):
            code, out = _docker(run, "exec", spec["container"], "psql", "-U", "aiflow", "-d", database,
                                "-v", "ON_ERROR_STOP=1", "-c", "CREATE EXTENSION IF NOT EXISTS vector", timeout=60)
            if code:
                return False, f"the vector extension is not available in {database}"
        return True, "accepting connections with the vector extension"
    code, out = _docker(run, "exec", spec["container"], "redis-cli", "ping", timeout=30)
    return (True, "answers PING") if code == 0 and out.endswith("PONG") else (False, "Redis does not answer PING")


def prepare_service(name: str, run: Runner = subprocess.run, wait_s: float = 30, sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    """Make one service run and report `{state, healthy, detail, endpoint}`.

    `state` is `running` (nothing to do), `started` (was stopped), `recreated` (was missing or would not start) or
    `unhealthy` (the container exists but the service fails its check, or Docker itself fails).
    """
    import time
    sleep = sleep or time.sleep
    spec = SERVICES[name]
    report: dict[str, Any] = {"container": spec["container"], "image": spec["image"], "endpoint": endpoint(name),
                              "state": "unhealthy", "healthy": False, "detail": ""}
    code, out = _docker(run, "inspect", "-f", "{{.State.Status}}", spec["container"], timeout=30)
    if code == 127:
        report["detail"] = "Docker is not available on this node"
        return report
    state = out.strip() if code == 0 else "missing"
    action = "running"
    if state == "missing":
        action = "recreated"
    elif state != "running":
        code, _ = _docker(run, "start", spec["container"], timeout=60)
        if code == 0:
            action = "started"
        else:
            _docker(run, "rm", "-f", spec["container"], timeout=60)
            action = "recreated"
    if action == "recreated":
        code, out = _docker(run, *_run_args(spec), timeout=600)
        if code:
            report["detail"] = f"docker could not create {spec['container']}: {out.splitlines()[-1][:160] if out else 'no output'}"
            return report
    deadline = time.monotonic() + wait_s
    while True:
        ok, detail = _healthy(run, name)
        if ok or time.monotonic() >= deadline:
            break
        sleep(1)
    report.update(state=action if ok else "unhealthy", healthy=ok, detail=detail if ok else
                  f"{detail} ({action})")
    return report


def prepare_services(names: list[str], run: Runner = subprocess.run, **kwargs: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        if name not in SERVICES:
            continue
        try:
            out[name] = prepare_service(name, run, **kwargs)
        except Exception as exc:  # noqa: BLE001 - report it instead of stopping the agent
            log.warning("service %s preparation failed: %s", name, type(exc).__name__)
            out[name] = {"container": SERVICES[name]["container"], "endpoint": endpoint(name), "state": "unhealthy",
                         "healthy": False, "detail": f"preparation failed: {type(exc).__name__}"}
    return out
