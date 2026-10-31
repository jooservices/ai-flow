"""Node agent (systemd unit aiflow4-node-agent, root): join, heartbeat with metrics, worker supervision.

Heartbeat every 15 s carries CPU, memory, disk, load, tool versions and the state of every worker on the
node; the control plane marks the node offline after missed heartbeats. The answer lists the accounts this
node should run; the agent starts and stops aiflow4-worker@<account> units to match.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .. import VERSION
from ..core.settings import read_env_file
from ..core.toolchains import (BROKEN_PREFIX, CONTAINER_RUNTIME_TOOL, PLAYWRIGHT_BROWSERS_DIR, STACK_TOOLS,
                               tools_for_stacks)
from ..install.secrets import read_env_timestamps, update_env
from ..worker.client import ControlClient
from ..worker.loop import STATE_DIR
from .policy_relay import PolicyRelay
from . import cleanup as _cleanup
from .operator_audit import (_audit_interventions as _audit_interventions,
                             forward_operator_audit as forward_operator_audit)
from .provider_sign_in import (ensure_account_home as ensure_account_home,
                               run_provider_sign_in as run_provider_sign_in,
                               sync_provider_sign_ins as sync_provider_sign_ins)
from .cleanup import (_run_disk_cleanup as _run_disk_cleanup, _test_database_urls as _test_database_urls,
                      clean_scratch as clean_scratch, clean_worktrees as clean_worktrees,
                      sweep_test_databases as sweep_test_databases)
from .release import (_RELEASE_WORKFLOW_IDENTITY as _RELEASE_WORKFLOW_IDENTITY,
                     installed_release as installed_release,
                     self_update as self_update, verify_release_signature as verify_release_signature)
# Tests clear this shared dict in place, so the shared reference stays current.
from .release import _update_tried as _update_tried

log = logging.getLogger("aiflow4.node")
TOOLS = {"claude": ["claude", "--version"], "codex": ["codex", "--version"],
         "codex-code-mode-host": ["sh", "-c", 'h="$(dirname "$(readlink -f "$(command -v codex)")")/codex-code-mode-host";'
                                  ' test -x "$h" && { if test -r "$h.version"; then'
                                  ' printf "codex-code-mode-host %s\\n" "$(head -n1 "$h.version")";'
                                  ' else echo "present $h"; fi; }'], "grok": ["grok", "--version"],
         "opencode": ["opencode", "--version"], "docker": ["docker", "--version"], "git": ["git", "--version"],
         "node": ["node", "--version"], "uv": ["uv", "--version"], "gh": ["gh", "--version"],
         "php": ["php", "--version"], "composer": ["composer", "--version"], "wp": ["wp", "--version", "--allow-root"],
         "python3": ["python3", "--version"], "playwright": ["playwright", "--version"],
         "gitleaks": ["gitleaks", "version"],
         "xdebug": ["php", "-r", "echo extension_loaded('xdebug') ? 'Xdebug ' . phpversion('xdebug') : 'missing';"],
         "cosign": ["cosign", "version"], "podman": ["podman", "--version"]}
REPORTED_TOOLS = tools_for_stacks(STACK_TOOLS)


def notify_systemd(message: str) -> bool:
    """Send a readiness/status message to systemd without an extra dependency."""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.connect(address)
            connection.sendall(message.encode("utf-8"))
        return True
    except OSError:
        return False


_network_checked_at = 0.0
_github_network_ok: bool | None = None


def _github_network_available() -> bool:
    """Verify GitHub HTTPS with certificate checks and the node's configured proxy settings."""
    global _network_checked_at, _github_network_ok
    if time.time() - _network_checked_at >= 600 or _github_network_ok is None:
        try:
            with httpx.Client(timeout=3, follow_redirects=True, trust_env=True) as client:
                response = client.get("https://github.com")
                response.raise_for_status()
            _github_network_ok = True
        except (httpx.HTTPError, OSError):
            _github_network_ok = False
        _network_checked_at = time.time()
    return _github_network_ok


def tool_versions(names: list[str] | None = None) -> dict[str, str]:
    """One version line per tool, or `missing`; names default to the node tools and every stack tool."""
    out = {}
    for name in names if names is not None else REPORTED_TOOLS:
        if name in {"chromium", "firefox", "webkit"}:
            out[name] = _playwright_browser_status(name, out.get("playwright", ""))
            continue
        if name == CONTAINER_RUNTIME_TOOL:
            from ..worker.containers import safe_container_runtime_version
            out[name] = safe_container_runtime_version()
            continue
        cmd = TOOLS.get(name)
        if not cmd:
            out[name] = "missing"
            continue
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = (p.stdout or p.stderr).strip()
            version = output.splitlines()[0][:80] if output else ""
            out[name] = version if p.returncode == 0 and version else "missing"
        except subprocess.TimeoutExpired:  # a busy node answers slowly; that says nothing about the tool
            out[name] = "timeout"
        except OSError:
            out[name] = "missing"
    return out


def _playwright_browser_status(name: str, playwright_version: str) -> str:
    """Launch one installed browser as the node agent and report its browser name on a machine gap."""
    browser = {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}[name]
    package = re.search(r"\d+(?:\.\d+){1,2}", playwright_version)
    executable = shutil.which("playwright")
    if not package or not executable or not shutil.which("node"):
        return "missing"
    package_dir = Path(executable).resolve().parent
    script = ("const p=require(process.argv[1]); const b=p[process.argv[2]]; "
              "(async()=>{const x=await b.launch({headless:true}); await x.close();})()"
              ".catch(e=>{console.error(e.message||String(e));process.exit(1)})")
    try:
        result = subprocess.run(["node", "-e", script, str(package_dir), name], capture_output=True, text=True,
                                timeout=45, env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_DIR})
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "missing"
    if result.returncode == 0:
        return f"{browser} Playwright {package.group(0)}"
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    message = detail[-1][:160] if detail else "could not launch"
    if "Executable doesn't exist" in message or "Please run" in message:
        return "missing"
    return f"{BROKEN_PREFIX} {browser} cannot start: {message}"[:200]


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
    try:  # working set, like kubectl/cAdvisor: page cache the kernel can drop is not "used"
        used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
        stat = dict(line.split() for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
        used = max(0, used - int(stat.get("inactive_file", 0)))
    except (OSError, ValueError):
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


def runtime_settings_inventory(projects_dir: str | Path = "/etc/aiflow/projects") -> dict[str, list[dict]]:
    """Report project env key names, set state, and per-key change time; never include values in the heartbeat."""
    out: dict[str, list[dict]] = {}
    root = Path(projects_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.env")):
        project = path.stem
        if not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", project) or path.is_symlink() or not path.is_file():
            continue
        try:
            entries = read_env_file(path)
            timestamps = read_env_timestamps(path, entries)
        except OSError:
            continue
        out[project] = [{"name": key, "is_set": bool(value), "changed_at": timestamps.get(key, "")}
                        for key, value in sorted(entries.items()) if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)]
    return out


def apply_runtime_settings_bootstrap(node: str, entries: list[dict],
                                     projects_dir: str | Path = "/etc/aiflow/projects") -> None:
    """Install encrypted project settings supplied at heartbeat before the node starts assigned workers."""
    from ..core.runtime_settings_crypto import decrypt_value

    root = Path(projects_dir)
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("runtime setting bootstrap entry is invalid")
        project, key = str(entry.get("project") or ""), str(entry.get("name") or "")
        changed_at = str(entry.get("changed_at") or "")[:40]
        if (not re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", project)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
                or not changed_at or not isinstance(entry.get("envelope"), dict)):
            raise ValueError("runtime setting bootstrap entry is invalid")
        context = f"runtime-settings-bootstrap:{node}:{project}:{key}:{changed_at}"
        value = decrypt_value(entry["envelope"], context)
        if not re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
            raise ValueError("runtime setting bootstrap value is invalid")
        path = root / f"{project}.env"
        if path.is_symlink():
            raise ValueError("runtime setting bootstrap path is invalid")
        update_env(path, force={key: value}, header="Managed from the AI Flow Settings screen",
                   track_timestamps=True, timestamps={key: changed_at})


def credential_expiries(states: list[dict]) -> dict[str, str | None]:
    """Forward explicit provider CLI expiry metadata from worker probes; absent values stay unknown."""
    out: dict[str, str | None] = {}
    for state in states:
        account = str(state.get("account") or "")
        probe = state.get("probe") or {}
        if account and "credential_expires_at" in probe:
            value = probe.get("credential_expires_at")
            current = out.get(account)
            if account not in out or (value and (not current or value < current)):
                out[account] = value
    return out


def _system_machine_conditions(metrics_data: dict, auto_fixed_conditions: set[str] | None = None,
                               work_path: Path | None = None, state_path: Path | None = None) -> list[dict]:
    """Probe host assumptions that do not depend on any one project."""
    from ..core.system_check import condition

    out = []
    free = metrics_data.get("disk_free")
    if isinstance(free, (int, float)):
        gib = int(free / (1024 ** 3))
        if free >= _cleanup.MIN_WORK_DISK_FREE:
            result = "fixed_automatically" if "disk-space" in (auto_fixed_conditions or set()) else "ready"
            detail = f"The node freed space automatically; {gib} GiB is available." if result == "fixed_automatically" \
                else f"{gib} GiB is available."
            out.append(condition("disk-space", "Free disk space", result, detail))
        else:
            out.append(condition("disk-space", "Free disk space", "needs_action",
                                 f"Only {gib} GiB is available; work needs at least 5 GiB.",
                                 "Free at least 5 GiB on /srv/aiflow."))

    work = Path(work_path or "/srv/aiflow/work")
    state = Path(state_path or STATE_DIR)
    created = False
    try:
        import pwd

        worker = pwd.getpwnam("aiflow-worker")
        if not work.exists():
            work.mkdir(parents=True, mode=0o770)
            os.chown(work, worker.pw_uid, worker.pw_gid)
            os.chmod(work, 0o770)
            created = True
        if not state.exists():
            state.mkdir(parents=True, mode=0o770)
            os.chown(state, worker.pw_uid, worker.pw_gid)
            os.chmod(state, 0o770)
            created = True
        writable = _worker_can_access_directory(work, worker.pw_uid) \
            and _worker_can_access_directory(state, worker.pw_uid)
    except (KeyError, OSError, ImportError):
        writable = False
    if writable:
        result = "fixed_automatically" if created else "ready"
        detail = "The node created its work directory and can write its state." if created else \
            "The node can write its work and state directories."
        out.append(condition("workspace-permissions", "Work directory permissions", result, detail))
    else:
        out.append(condition("workspace-permissions", "Work directory permissions", "needs_action",
                             "The node cannot write its work and state directories.",
                             "Restore write access to /srv/aiflow/work and /run/aiflow4/workers for the aiflow service account."))
    # A successful authenticated heartbeat is itself the control API network check.
    out.append(condition("control-network", "Control API network", "ready",
                         "The machine reached the control API and sent this heartbeat."))
    if _github_network_available():
        out.append(condition("repository-network", "Repository network", "ready",
                             "The machine can reach GitHub over HTTPS."))
    else:
        out.append(condition("repository-network", "Repository network", "needs_action",
                             "The machine cannot reach GitHub over HTTPS.",
                             "Allow outbound HTTPS access to github.com on port 443 from this machine."))
    return out


def _worker_can_access_directory(path: Path, worker_uid: int) -> bool:
    """Check directory write and traversal access using the same identity as the worker's setpriv commands."""
    if os.geteuid() == worker_uid:
        return os.access(path, os.W_OK | os.X_OK)
    if os.geteuid() != 0:
        return False
    runuser = shutil.which("runuser")
    if not runuser:
        return False
    try:
        result = subprocess.run([runuser, "-u", "aiflow-worker", "--", "sh", "-c",
                                 'test -w "$1" && test -x "$1"', "aiflow4-permission-check", str(path)],
                                capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _credential_conditions(states: list[dict]) -> dict[str, dict]:
    """Publish each account's primary worker probe, never provider output or display text.

    Extra sessions share the account ID but deliberately do not probe credentials. They must not overwrite the
    primary session's result with an unknown status.
    """
    by_account: dict[str, list[dict]] = {}
    for worker in states:
        account = str(worker.get("account") or "")
        if account:
            by_account.setdefault(account, []).append(worker)

    checks: dict[str, dict] = {}
    for account, workers in by_account.items():
        # New workers publish their concrete instance name. Treat an unnamed state as primary only when it is the
        # sole session, so multiple legacy states cannot make the result depend on directory iteration order.
        primary = next((worker for worker in workers if worker.get("instance") == account), None)
        if primary is None and len(workers) == 1 and "instance" not in workers[0]:
            primary = workers[0]
        if primary is None:
            # Never infer credential validity from an extra session when the primary state is unavailable.
            checks[account] = {"present": True, "valid": None}
            continue

        probe = primary.get("probe") or {}
        if isinstance(probe, dict) and probe.get("ok") is True:
            status = {"present": True, "valid": True}
        elif primary.get("state") == "signed_out" or (isinstance(probe, dict) and
                                                         probe.get("error_kind") == "auth"):
            status = {"present": True, "valid": False}
        elif not primary.get("alive", True):
            # A stopped process cannot report on its credentials. Do not turn a crash into a sign-in request.
            status = {"present": True, "valid": None}
        else:
            status = {"present": True, "valid": None}
        checks[account] = status
    return checks


def systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], capture_output=True).returncode


def reconcile(accounts: list[str]) -> None:
    """Start worker instances (account or account.N) whose account has signed-in credentials; stop the others."""
    for a in accounts:
        if systemctl("is-active", "--quiet", f"aiflow4-worker@{a}") != 0:
            if ensure_account_home(a):
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


def process_node_operation(api: ControlClient, node: str) -> None:
    """Claim and run at most one server-created whitelist operation."""
    operation = api.call("GET", f"/v1/nodes/{node}/operations/next", retries=1).get("operation")
    if not operation:
        return
    from .operations import OperationError, execute

    operation_id = operation["id"]
    try:
        if operation.get("action") == "read_logs":
            from ..core.log_sources import read_source
            payload = operation.get("payload") or {}
            result = read_source(str(payload.get("kind") or ""), str(payload.get("target") or ""),
                                 payload.get("lines", 100))
        else:
            payload = operation.get("payload") or {}
            if payload.get("action") in {"runtime_setting_project", "runtime_setting_control"}:
                def checkpoint(envelope):
                    response = api.call("POST", f"/v1/nodes/{node}/operations/{operation_id}/checkpoint",
                                        {"rollback_envelope": envelope}, retries=2)
                    saved = response.get("rollback_envelope")
                    if not isinstance(saved, dict):
                        raise OperationError("runtime setting snapshot could not be checkpointed")
                    return saved
                result = execute(operation, checkpoint=checkpoint)
            else:
                result = execute(operation)
        body = {"status": "completed", "result": result}
    except OperationError as exc:
        log.warning("node operation %s failed: %s", operation_id, str(exc)[:300])
        body = {"status": "failed", "result": {"healthy": False, "error": str(exc)[:300]}}
    except Exception as exc:  # noqa: BLE001 - installer exceptions may include command details
        log.error("node operation %s failed with %s", operation_id, type(exc).__name__)
        body = {"status": "failed", "result": {"healthy": False, "error": type(exc).__name__}}
    api.call("POST", f"/v1/nodes/{node}/operations/{operation_id}/result", body, retries=2)


def _process_node_operation_logged(api: ControlClient, node: str) -> None:
    try:
        process_node_operation(api, node)
    except Exception as operation_error:  # noqa: BLE001 - keep polling failures out of the heartbeat thread
        log.warning("node operation poll failed: %s", type(operation_error).__name__)


PROBE_RETRY_S = 900  # a project check probe that did not report is asked for again after this long


def _has_worker_user() -> bool:
    try:
        import pwd
        pwd.getpwnam("aiflow-worker")
        return True
    except (KeyError, ImportError):
        return False


SERVICE_CHECK_S = 60 # how often declared test services are inspected, recreated when gone and reported


def _services_snapshot(state: dict[str, dict], lock: threading.Lock) -> dict[str, dict]:
    with lock:
        return {k: dict(v) for k, v in state.items()}


@dataclass
class _AgentLoopState:
    tools: dict[str, str]
    tools_at: float
    previous_metrics: tuple[int, int] | None = None
    project_environment: dict[str, list[str]] = field(default_factory=dict)
    pending_network_events: list[dict] = field(default_factory=list)
    operation_thread: threading.Thread | None = None
    toolchain_thread: threading.Thread | None = None
    toolchain_signature: str = ""
    toolchain_result: dict[str, object] = field(default_factory=dict)
    toolchain_lock: threading.Lock = field(default_factory=threading.Lock)
    service_thread: threading.Thread | None = None
    service_state: dict[str, dict] = field(default_factory=dict)
    service_names: list[str] = field(default_factory=list)
    service_at: float = 0.0
    service_lock: threading.Lock = field(default_factory=threading.Lock)
    container_cleanup_thread: threading.Thread | None = None
    container_cleanup_at: float = 0.0
    sign_in_processes: dict[str, tuple[str, threading.Event, threading.Thread]] = field(default_factory=dict)
    probe_thread: threading.Thread | None = None
    probe_tried: dict[tuple[str, str], float] = field(default_factory=dict)
    auto_fixed_tools: set[str] = field(default_factory=set)
    auto_fixed_conditions: set[str] = field(default_factory=set)


def collect_heartbeat(state: _AgentLoopState, name: str, slots: int, relay: PolicyRelay | None,
                       runtime_settings_public_key: str) -> dict:
    """Collect the node metrics, tools, workers and settings reported in each heartbeat."""
    node_metrics, state.previous_metrics = metrics(state.previous_metrics)
    if time.time() - state.tools_at > 600:
        state.tools, state.tools_at = tool_versions(), time.time()
    workers = worker_states()
    used = sum(1 for worker in workers if worker.get("state") == "working" and worker.get("alive"))
    environment_items = {}
    for project, names in state.project_environment.items():
        local = read_env_file(f"/etc/aiflow/projects/{project}.env")
        present = [name for name in names if local.get(name)]
        if present:
            environment_items[project] = present
    return {
        "slots_total": slots,
        "slots_used": used,
        "release_version": installed_release() or VERSION,
        "release_protocol": 1,
        "tools": state.tools,
        "services": _services_snapshot(state.service_state, state.service_lock),
        "metrics": {**node_metrics, "hostname": socket.gethostname(),
                    "policy_relay": relay.health() if relay else {"enabled": False, "healthy": False}},
        "workers": workers,
        "credential_expiries": credential_expiries(workers),
        "system_conditions": _system_machine_conditions(node_metrics, state.auto_fixed_conditions),
        "credential_checks": _credential_conditions(workers),
        "auto_fixed_tools": sorted(state.auto_fixed_tools),
        "auto_fixed_conditions": sorted(state.auto_fixed_conditions),
        "runtime_settings": runtime_settings_inventory(),
        "runtime_settings_public_key": runtime_settings_public_key,
        "environment_items": environment_items,
    }


def send_heartbeat(api: ControlClient, name: str, heartbeat: dict, state: _AgentLoopState | None = None) -> dict:
    """Send one collected heartbeat and return the desired node state from control."""
    desired = api.call("POST", f"/v1/nodes/{name}/heartbeat", heartbeat, retries=2)["desired"]
    if state is not None:  # fixes are reported once, after control has received them
        state.auto_fixed_tools.clear()
        state.auto_fixed_conditions.clear()
    return desired


def _record_heartbeat_failure(state: _AgentLoopState, error: Exception) -> None:
    if "control API unreachable" in str(error) or type(error).__module__.startswith("httpx"):
        state.pending_network_events.append({"type": "node.heartbeat.error", "error_kind": "network",
                                             "retry": True, "attempts": 2,
                                             "reason": "node-agent could not reach the control API",
                                             "occurred_at": time.time()})
        state.pending_network_events = state.pending_network_events[-100:]
    log.warning("heartbeat failed: %s", error)


def _apply_runtime_settings_bootstrap(name: str, desired: dict, stop: threading.Event, every_s: int) -> bool:
    bootstrap = desired.get("runtime_settings_bootstrap") or []
    if not bootstrap:
        return True
    try:
        apply_runtime_settings_bootstrap(name, bootstrap)
    except Exception as exc:  # noqa: BLE001 - do not start workers without backed-up settings applied
        log.warning("runtime settings bootstrap failed: %s", type(exc).__name__)
        stop.wait(every_s)
        return False
    return True


def _apply_toolchains(desired: dict, state: _AgentLoopState, name: str) -> None:
    stack_request = {"release": desired.get("release", ""),
                     "project_stacks": desired.get("project_stacks", {}),
                     "versions": desired.get("versions", {}),
                     "required_tools": desired.get("tools", []),
                     "requested_tools": desired.get("tool_repairs", [])}
    signature = json.dumps(stack_request, sort_keys=True, separators=(",", ":"))
    if signature != state.toolchain_signature and (state.toolchain_thread is None or
                                                    not state.toolchain_thread.is_alive()):
        from .operations import prepare_project_toolchains

        with state.toolchain_lock:
            state.toolchain_result.clear()

        def prepare(request=stack_request):
            try:
                required_tools = request["required_tools"]
                requested_tools = request["requested_tools"]
                if requested_tools:
                    result = prepare_project_toolchains(request["project_stacks"], request["versions"],
                                                        required_tools, requested_tools=requested_tools)
                elif required_tools:
                    result = prepare_project_toolchains(request["project_stacks"], request["versions"],
                                                        required_tools)
                else:
                    result = prepare_project_toolchains(request["project_stacks"], request["versions"])
                with state.toolchain_lock:
                    state.toolchain_result.clear()
                    state.toolchain_result.update(result)
            except Exception as exc:  # noqa: BLE001 - failed preflight is reported as a project warning
                log.warning("project toolchain preparation failed: %s", type(exc).__name__)

        state.toolchain_signature = signature
        state.toolchain_thread = threading.Thread(target=prepare, name=f"aiflow4-toolchain-{name}", daemon=True)
        state.toolchain_thread.start()
    if state.toolchain_thread is None or state.toolchain_thread.is_alive():
        return
    # The post-install probe is reported once; later heartbeats use the normal tool_versions() refresh.
    with state.toolchain_lock:
        prepared_tools = state.toolchain_result.pop("tools", None)
        installed_tools = state.toolchain_result.pop("installed", [])
        request_gaps = state.toolchain_result.pop("request_gaps", [])
    if isinstance(prepared_tools, dict) and prepared_tools:
        state.tools.update({str(key): str(value) for key, value in prepared_tools.items()})
        state.tools_at = time.time()
    if isinstance(installed_tools, list):
        state.auto_fixed_tools.update(str(tool) for tool in installed_tools)
    if request_gaps:
        state.toolchain_signature = ""


def _apply_services(desired: dict, state: _AgentLoopState, name: str) -> None:
    wanted = sorted({str(service) for services in (desired.get("project_services") or {}).values()
                     for service in services})
    if wanted != state.service_names:
        state.service_names = wanted
        state.service_at = 0.0
        with state.service_lock:  # services no project declares any more are no longer reported
            for stale in set(state.service_state) - set(wanted):
                state.service_state.pop(stale, None)
    if not wanted or time.time() - state.service_at < SERVICE_CHECK_S or (
            state.service_thread is not None and state.service_thread.is_alive()):
        return
    from .services import prepare_services

    def check_services(names=list(wanted)):
        result = prepare_services(names)
        with state.service_lock:
            state.service_state.update(result)

    state.service_at = time.time()
    state.service_thread = threading.Thread(target=check_services, name=f"aiflow4-services-{name}", daemon=True)
    state.service_thread.start()


def _apply_probes(api: ControlClient, name: str, desired: dict, state: _AgentLoopState,
                  manage_workers: bool) -> None:
    asked = [probe for probe in desired.get("check_probes") or []
             if time.time() - state.probe_tried.get((probe["project"], probe["signature"]), 0.0) > PROBE_RETRY_S]
    if not asked or not manage_workers or (state.probe_thread is not None and state.probe_thread.is_alive()):
        return
    from .probes import run_check_probes
    from ..worker.loop import RUN_AS

    for probe in asked:
        state.probe_tried[(probe["project"], probe["signature"])] = time.time()

    def probe_checks(items=asked):
        try:
            run_check_probes(api, name, items, "/srv/aiflow", RUN_AS if _has_worker_user() else [])
        except Exception as exc:  # noqa: BLE001 - control asks again after PROBE_RETRY_S
            log.warning("project check probe failed: %s", type(exc).__name__)

    state.probe_thread = threading.Thread(target=probe_checks, name=f"aiflow4-check-probe-{name}", daemon=True)
    state.probe_thread.start()


def _apply_queued_work(api: ControlClient, name: str, state: _AgentLoopState) -> None:
    if state.pending_network_events:
        try:
            api.call("POST", "/v1/events", {"events": list(state.pending_network_events)})
            state.pending_network_events = []
        except Exception as exc:  # noqa: BLE001 - retry the event delivery on the next heartbeat
            log.warning("heartbeat error report not recorded: %s", type(exc).__name__)
    if state.operation_thread is None or not state.operation_thread.is_alive():
        state.operation_thread = threading.Thread(
            target=_process_node_operation_logged, args=(api, name), name=f"aiflow4-node-operation-{name}")
        state.operation_thread.start()


def _apply_worker_state(api: ControlClient, name: str, desired: dict, state: _AgentLoopState,
                        manage_workers: bool) -> None:
    if manage_workers:
        reconcile(desired.get("workers") or desired.get("accounts", []))
        _run_disk_cleanup(clean_scratch, state.auto_fixed_conditions)
        _run_disk_cleanup(lambda: clean_worktrees(api, name), state.auto_fixed_conditions)
        sweep_test_databases()
        if (state.tools.get(CONTAINER_RUNTIME_TOOL, "missing") != "missing"
                and time.time() - state.container_cleanup_at >= 24 * 60 * 60
                and (state.container_cleanup_thread is None or not state.container_cleanup_thread.is_alive())):
            from ..worker.containers import prune_image_cache

            state.container_cleanup_at = time.time()
            state.container_cleanup_thread = threading.Thread(
                target=prune_image_cache, name=f"aiflow4-container-prune-{name}", daemon=True)
            state.container_cleanup_thread.start()
    if os.environ.get("AIFLOW4_SELF_UPDATE", "1") == "1":
        self_update(api, desired.get("release", ""), name)


def apply_heartbeat(api: ControlClient, name: str, desired: dict, state: _AgentLoopState,
                    manage_workers: bool, stop: threading.Event, every_s: int) -> bool:
    """Apply control's settings, sign-ins, repairs, checks, worker assignments and release instruction."""
    if not _apply_runtime_settings_bootstrap(name, desired, stop, every_s):
        return False
    state.project_environment = desired.get("project_environment", {})
    requests = [*(desired.get("sign_ins") or []), *(desired.get("signin") or [])]
    sync_provider_sign_ins(api, name, requests, state.sign_in_processes)
    _apply_toolchains(desired, state, name)
    _apply_services(desired, state, name)
    _apply_probes(api, name, desired, state, manage_workers)
    _apply_queued_work(api, name, state)
    try:
        _apply_worker_state(api, name, desired, state, manage_workers)
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat failed: %s", exc)
    return True


def run_agent(control_url: str, name: str, token: str, slots: int = 2, every_s: int = 15, manage_workers: bool = True,
              stop: threading.Event | None = None) -> None:
    from ..core.runtime_settings_crypto import node_public_key

    api = ControlClient(control_url, token, timeout=15)
    runtime_settings_public_key = node_public_key()
    stop = stop or threading.Event()
    relay = PolicyRelay(control_url) if os.environ.get("AIFLOW4_POLICY_RELAY", "1") == "1" else None
    if relay:
        relay.start()
    state = _AgentLoopState(tools=tool_versions(), tools_at=time.time())
    ready_notified = False
    try:
        while not stop.is_set():
            try:
                heartbeat = collect_heartbeat(state, name, slots, relay, runtime_settings_public_key)
                desired = send_heartbeat(api, name, heartbeat, state)
                if not ready_notified:
                    ready_notified = notify_systemd("READY=1\nSTATUS=First control heartbeat succeeded")
            except Exception as exc:  # noqa: BLE001 - keep heartbeat outages visible after connectivity returns
                _record_heartbeat_failure(state, exc)
                stop.wait(every_s)
                continue
            try:
                forward_operator_audit(api, name)
            except Exception as exc:  # noqa: BLE001 - retain the cursor so the next heartbeat retries delivery
                log.warning("host audit forwarding failed (%s)", type(exc).__name__)
            if not apply_heartbeat(api, name, desired, state, manage_workers, stop, every_s):
                continue
            stop.wait(every_s)
    finally:
        for _request_id, cancel, _thread in state.sign_in_processes.values():
            cancel.set()
        if relay:
            relay.stop()
