"""Node agent (systemd unit aiflow4-node-agent, root): join, heartbeat with metrics, worker supervision.

Heartbeat every 15 s carries CPU, memory, disk, load, tool versions and the state of every worker on the
node; the control plane marks the node offline after missed heartbeats. The answer lists the accounts this
node should run; the agent starts and stops aiflow4-worker@<account> units to match.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .. import VERSION
from ..core.toolchains import BROKEN_PREFIX, STACK_TOOLS, tools_for_stacks
from ..worker.client import ControlClient
from ..worker.loop import STATE_DIR
from .policy_relay import PolicyRelay

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
         "playwright-browser": ["playwright", "install", "--dry-run", "chromium"]}
REPORTED_TOOLS = tools_for_stacks(STACK_TOOLS)


_MISSING_LIBRARY = re.compile(r"error while loading shared libraries:\s*([^\s:]+)", re.IGNORECASE)
_network_checked_at = 0.0
_github_network_ok: bool | None = None


def _github_network_available() -> bool:
    """Check the HTTPS route used to clone configured GitHub repositories without logging network details."""
    global _network_checked_at, _github_network_ok
    if time.time() - _network_checked_at >= 600 or _github_network_ok is None:
        try:
            with socket.create_connection(("github.com", 443), timeout=3):
                _github_network_ok = True
        except OSError:
            _github_network_ok = False
        _network_checked_at = time.time()
    return _github_network_ok


def _headless_launch_problem(binary: Path) -> str:
    """Start the browser headless once; return why it cannot start (a machine tool problem), or an empty string."""
    try:
        p = subprocess.run([str(binary), "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom", "about:blank"],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ""  # a busy node answers slowly; that says nothing about the browser
    except OSError as exc:
        return f"cannot start ({type(exc).__name__})"
    if p.returncode == 0:
        return ""
    library = _MISSING_LIBRARY.search(p.stderr or "")
    if library:
        return f"missing system library {library.group(1)}"
    return f"headless launch failed (exit {p.returncode})"


def _playwright_browser_version(dry_run: str, playwright_version: str) -> str:
    """Report the browser only when this Playwright release's expected Chromium build is installed and starts.

    A build that is installed but cannot start headless (usually a missing system library) is reported as
    `broken: <reason>`: a problem of this machine's toolchain, not of any ticket.
    """
    package = re.search(r"\d+(?:\.\d+){1,2}", playwright_version)
    location = re.search(r"^\s*Install location:\s*(.+?)\s*$", dry_run, re.MULTILINE | re.IGNORECASE)
    if not package or not location:
        return "missing"
    root = Path(location.group(1))
    binaries = (root / "chrome-linux" / "chrome", root / "chrome-linux64" / "chrome",
                root / "chrome-headless-shell-linux64" / "chrome-headless-shell")
    binary = next((path for path in binaries if path.is_file()), None)
    if binary is None:
        return "missing"
    problem = _headless_launch_problem(binary)
    if problem:
        return f"{BROKEN_PREFIX} {problem}"[:80]
    return f"Playwright Chromium {package.group(0)}"


def tool_versions(names: list[str] | None = None) -> dict[str, str]:
    """One version line per tool, or `missing`; names default to the node tools and every stack tool."""
    out = {}
    for name in names if names is not None else REPORTED_TOOLS:
        cmd = TOOLS.get(name)
        if not cmd:
            out[name] = "missing"
            continue
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = (p.stdout or p.stderr).strip()
            version = output.splitlines()[0][:80] if output else ""
            if name == "playwright-browser":
                # A dry run returns the expected directory for this package release; this rejects stale browsers.
                out[name] = (_playwright_browser_version(output, out.get("playwright", ""))
                             if p.returncode == 0 else "missing")
            else:
                out[name] = version if p.returncode == 0 and version else "missing"
        except subprocess.TimeoutExpired:  # a busy node answers slowly; that says nothing about the tool
            out[name] = "timeout"
        except OSError:
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
        if free >= 5 * 1024 ** 3:
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
        if not work.exists():
            work.mkdir(parents=True, mode=0o755)
            created = True
        if not state.exists():
            state.mkdir(parents=True, mode=0o750)
            created = True
        writable = os.access(work, os.W_OK | os.X_OK) and os.access(state, os.W_OK | os.X_OK)
    except OSError:
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


def _credential_conditions(states: list[dict]) -> dict[str, dict]:
    """Publish only validated credential presence/validity state, never provider output or display text."""
    checks: dict[str, dict] = {}
    for worker in states:
        account = str(worker.get("account") or "")
        if not account:
            continue
        probe = worker.get("probe") or {}
        if isinstance(probe, dict) and probe.get("ok"):
            status = {"present": True, "valid": True}
        elif worker.get("state") == "signed_out" or (isinstance(probe, dict) and probe.get("error_kind") == "auth"):
            status = {"present": True, "valid": False}
        elif not worker.get("alive", True):
            status = {"present": False, "valid": None}
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
            if Path(f"/srv/aiflow/accounts/{a.split('.')[0]}").is_dir():
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


_update_tried: dict[str, float] = {}
_last_clean = 0.0
_last_sweep = 0.0
_DEVELOP_BUILD = re.compile(r"[0-9a-f]{7,40}\Z")
_RELEASE_VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_RELEASE_WORKFLOW_IDENTITY = "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/{}"
_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
RELEASE_INCOMING_DIR = Path("/srv/aiflow/releases/incoming")


def clean_worktrees(api: ControlClient, node: str, srv_root: str = "/srv/aiflow", min_age_s: int = 600) -> int:
    """Remove the worktrees of finished tickets (Done) and spent per-run checkouts: the control plane decides
    which directories can go. Runs at most every ten minutes; returns how many were removed."""
    global _last_sweep
    if time.time() - _last_sweep < 600:
        return 0
    _last_sweep = time.time()
    work = Path(srv_root) / "work"
    found = {}
    for project in work.iterdir() if work.is_dir() else []:
        if not project.is_dir():
            continue
        for d in project.iterdir():
            try:
                if d.is_dir() and not d.is_symlink() and time.time() - d.stat().st_mtime > min_age_s:
                    found[f"{project.name}/{d.name}"] = d
            except OSError:
                pass
    if not found:
        return 0
    remove = api.call("POST", f"/v1/nodes/{node}/worktrees/sweep", {"dirs": sorted(found)}, retries=1).get("remove", [])
    removed = 0
    for entry in remove:
        path = found.get(entry)
        if path is None:
            continue
        try:
            for root, _dirs, _files in os.walk(path, topdown=True, followlinks=False):
                os.chmod(root, 0o700)  # read-only triage checkouts
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:
            log.warning("could not remove worktree %s: %s", entry, exc)
    if removed:
        for mirror in (Path(srv_root) / "mirrors").glob("*"):
            subprocess.run(["git", "-c", "safe.directory=*", "-C", str(mirror), "worktree", "prune", "--expire=now"],
                           capture_output=True)
        log.info("removed %d worktrees of finished tickets", removed)
    return removed


_last_testdb_sweep = 0.0


def _test_database_urls(projects_dir: str = "/etc/aiflow/projects") -> list[str]:
    """Test database URLs from the project env files on this node (never logged: they carry a password)."""
    urls: list[str] = []
    for f in sorted(Path(projects_dir).glob("*.env")):
        try:
            for line in f.read_text().splitlines():
                key, _, value = line.partition("=")
                value = value.strip().strip("'\"")
                if key.strip() == "AIFLOW4_TEST_DATABASE_URL" and value and value not in urls:
                    urls.append(value)
        except OSError:
            continue
    return urls


def sweep_test_databases(projects_dir: str = "/etc/aiflow/projects", every_s: int = 600) -> int:
    """Drop `aiflow4_t_*` test databases that no connection uses and that are over two hours old, at most every
    ten minutes. Returns how many were dropped."""
    global _last_testdb_sweep
    if time.time() - _last_testdb_sweep < every_s:
        return 0
    _last_testdb_sweep = time.time()
    from ..core import testdb
    dropped = 0
    for url in _test_database_urls(projects_dir):
        try:
            dropped += len(testdb.sweep(url))
        except Exception as e:  # noqa: BLE001 - the test server may be down; try again next time
            log.warning("test database sweep failed: %s", type(e).__name__)
    if dropped:
        log.info("dropped %s leftover test databases", dropped)
    return dropped


def clean_scratch(max_age_s: int = 6 * 3600) -> int:
    """Remove the AI user's temp files older than six hours from /tmp (RAM on the lab machines) and from the
    scratch tmp on disk. Runs at most once an hour; returns how many entries were removed."""
    global _last_clean
    if time.time() - _last_clean < 3600:
        return 0
    _last_clean = time.time()
    removed = 0
    try:
        import pwd
        uid = pwd.getpwnam("aiflow-worker").pw_uid
    except (KeyError, ImportError):
        return 0
    for base in (Path("/tmp"), Path("/srv/aiflow/cache/ai/tmp")):
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                st = entry.lstat()
                if st.st_uid != uid or time.time() - st.st_mtime < max_age_s:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("cleaned %s old AI temp entries", removed)
    return removed


def installed_release() -> str:
    try:
        return Path("/srv/aiflow/current/RELEASE").read_text().strip()
    except OSError:
        return ""


def verify_release_signature(tarball: Path, bundle: Path, version: str) -> tuple[bool, str]:
    """Verify a release bundle from the repository's tag-triggered GitHub Actions workflow."""
    if not _RELEASE_VERSION.fullmatch(version):
        return False, "release version is not a vX.Y.Z tag"
    command = ["cosign", "verify-blob", str(tarball), "--bundle", str(bundle),
               "--certificate-identity", _RELEASE_WORKFLOW_IDENTITY.format(version),
               "--certificate-oidc-issuer", _GITHUB_OIDC_ISSUER]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cosign verification could not run: {type(exc).__name__}"
    if result.returncode:
        return False, (result.stderr or result.stdout or "cosign rejected the release signature").strip()[:300]
    return True, ""


def _report_release_rejection(api: ControlClient, node: str, version: str, detail: str) -> None:
    try:
        api.call("POST", f"/v1/nodes/{node}/release-failures",
                 {"release_version": version, "detail": detail[:1000]}, retries=1)
    except Exception as exc:  # noqa: BLE001 - installation still fails closed if control is unreachable
        log.error("could not report rejected release %s: %s", version, str(exc)[:200])


def self_update_develop(api: ControlClient, sha: str) -> None:
    """Develop channel (lab only, docs/v4/develop-channel.md): install the control plane's unsigned develop build,
    named by its commit id, with the installer this node already has (never one taken from the new archive)."""
    if sha == installed_release() or time.time() - _update_tried.get(sha, 0) < 600:
        return
    _update_tried[sha] = time.time()
    RELEASE_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tar = RELEASE_INCOMING_DIR / f"aiflow4-{sha}.tar.gz"
    response = api.c.get(f"/v1/releases/{sha}.tar.gz", timeout=120)
    if response.status_code != 200:
        log.warning("develop build %s not downloadable: %s", sha, response.status_code)
        return
    tmp = tar.with_suffix(tar.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(tar)
    result = subprocess.run(["systemd-run", "--unit", f"aiflow4-self-update-{sha[:10]}-{int(time.time())}",
                             "--no-block", "--collect", "/usr/local/sbin/aiflow4-develop-install", str(tar), sha],
                            capture_output=True, text=True)
    log.info("develop self-update to %s started: %s %s", sha, result.returncode,
             (result.stderr or result.stdout).strip()[:200])


def self_update(api: ControlClient, version: str, node: str = "unknown") -> None:
    """Download, checksum and verify the control plane's tagged release before scheduling its installer."""
    if version and _DEVELOP_BUILD.fullmatch(version) and os.environ.get("AIFLOW4_CHANNEL", "release") == "develop":
        self_update_develop(api, version)
        return
    if not version or not _RELEASE_VERSION.fullmatch(version):
        if version:
            log.warning("ignoring unversioned release identifier %s", version[:80])
        return
    if version == installed_release() or time.time() - _update_tried.get(version, 0) < 600:
        return
    _update_tried[version] = time.time()
    inc = RELEASE_INCOMING_DIR
    inc.mkdir(parents=True, exist_ok=True)
    tar = inc / f"aiflow4-{version}.tar.gz"
    bundle = inc / f"aiflow4-{version}.tar.gz.sigstore.json"
    try:
        manifest_response = api.c.get(f"/v1/releases/{version}/manifest", timeout=30)
    except Exception as exc:  # noqa: BLE001 - an advertised release without its manifest is a verification failure
        detail = f"release manifest unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if manifest_response.status_code != 200:
        detail = f"release manifest unavailable (HTTP {manifest_response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    try:
        manifest = manifest_response.json()
        expected_sha256 = str(manifest["sha256"])
        tar_url, bundle_url = manifest["tarball"], manifest["signature"]
        if manifest.get("version") != version or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("release manifest has an invalid version or checksum")
        if tar_url != f"/v1/releases/{version}.tar.gz" or bundle_url != f"/v1/releases/{version}.tar.gz.sigstore.json":
            raise ValueError("release manifest points outside its versioned asset paths")
    except (KeyError, TypeError, ValueError) as exc:
        detail = f"invalid release manifest: {exc}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return

    try:
        response = api.c.get(tar_url, timeout=120)
    except Exception as exc:  # noqa: BLE001 - all required signed assets must exist before installation
        detail = f"release tarball unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if response.status_code != 200:
        detail = f"release tarball unavailable (HTTP {response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    try:
        signature_response = api.c.get(bundle_url, timeout=120)
    except Exception as exc:  # noqa: BLE001 - an advertised release without its signature cannot be installed
        detail = f"release signature bundle unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if signature_response.status_code != 200:
        detail = f"release signature bundle unavailable (HTTP {signature_response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    tar_tmp = tar.with_suffix(tar.suffix + ".tmp")
    bundle_tmp = bundle.with_suffix(bundle.suffix + ".tmp")
    tar_tmp.write_bytes(response.content)
    bundle_tmp.write_bytes(signature_response.content)
    tar_tmp.replace(tar)
    bundle_tmp.replace(bundle)

    if hashlib.sha256(tar.read_bytes()).hexdigest() != expected_sha256:
        detail = "SHA256SUMS checksum does not match the downloaded release tarball"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    valid, detail = verify_release_signature(tar, bundle, version)
    if not valid:
        detail = f"cosign signature verification failed: {detail}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return

    unit = f"aiflow4-self-update-{version.replace('.', '-')}-{int(time.time())}"
    command = ["systemd-run", "--unit", unit, "--no-block", "--collect",
               "/usr/local/sbin/aiflow4-node-install", str(tar), version, str(bundle), expected_sha256]
    result = subprocess.run(command, capture_output=True, text=True)
    log.info("self-update to %s started: %s %s", version, result.returncode,
             (result.stderr or result.stdout).strip()[:200])


def process_node_operation(api: ControlClient, node: str) -> None:
    """Claim and run at most one owner-approved, server-created whitelist operation."""
    operation = api.call("GET", f"/v1/nodes/{node}/operations/next", retries=1).get("operation")
    if not operation:
        return
    from .operations import OperationError, execute

    operation_id = operation["id"]
    try:
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


SERVICE_CHECK_S = 60  # how often declared test services are inspected, recreated when gone and reported


def _services_snapshot(state: dict[str, dict], lock: threading.Lock) -> dict[str, dict]:
    with lock:
        return {k: dict(v) for k, v in state.items()}


def run_agent(control_url: str, name: str, token: str, slots: int = 2, every_s: int = 15, manage_workers: bool = True,
              stop: threading.Event | None = None) -> None:
    api = ControlClient(control_url, token, timeout=15)
    stop = stop or threading.Event()
    relay = PolicyRelay(control_url) if os.environ.get("AIFLOW4_POLICY_RELAY", "1") == "1" else None
    if relay:
        relay.start()
    tools, tools_at, prev = tool_versions(), time.time(), None
    auto_fixed_tools: set[str] = set()
    auto_fixed_conditions: set[str] = set()
    pending_network_events: list[dict] = []
    operation_thread: threading.Thread | None = None
    toolchain_thread: threading.Thread | None = None
    toolchain_signature = ""
    toolchain_result: dict[str, object] = {}
    toolchain_lock = threading.Lock()
    service_thread: threading.Thread | None = None
    service_state: dict[str, dict] = {}
    service_names: list[str] = []
    service_at = 0.0
    service_lock = threading.Lock()
    try:
        while not stop.is_set():
            try:
                m, prev = metrics(prev)
                if time.time() - tools_at > 600:
                    tools, tools_at = tool_versions(), time.time()
                ws = worker_states()
                used = sum(1 for w in ws if w.get("state") == "working" and w.get("alive"))
                heartbeat = api.call("POST", f"/v1/nodes/{name}/heartbeat", {
                    "slots_total": slots, "slots_used": used, "release_version": installed_release() or VERSION,
                    "release_protocol": 1,
                    "tools": tools,
                    "services": _services_snapshot(service_state, service_lock),
                    "metrics": {**m, "hostname": socket.gethostname(),
                                "policy_relay": relay.health() if relay else {"enabled": False, "healthy": False}},
                    "workers": ws, "credential_expiries": credential_expiries(ws),
                    "system_conditions": _system_machine_conditions(m, auto_fixed_conditions),
                    "credential_checks": _credential_conditions(ws),
                    "auto_fixed_tools": sorted(auto_fixed_tools),
                    "auto_fixed_conditions": sorted(auto_fixed_conditions)}, retries=2)
                auto_fixed_tools.clear()
                auto_fixed_conditions.clear()
                desired = heartbeat["desired"]
            except Exception as e:  # noqa: BLE001 - keep heartbeat outages visible after connectivity returns
                if "control API unreachable" in str(e) or type(e).__module__.startswith("httpx"):
                    pending_network_events.append({"type": "node.heartbeat.error", "error_kind": "network",
                                                   "retry": True, "attempts": 2,
                                                   "reason": "node-agent could not reach the control API",
                                                   "occurred_at": time.time()})
                    pending_network_events = pending_network_events[-100:]
                log.warning("heartbeat failed: %s", e)
                stop.wait(every_s)
                continue
            stack_request = {"release": desired.get("release", ""),
                             "project_stacks": desired.get("project_stacks", {}),
                             "versions": desired.get("versions", {}), "tools": desired.get("tools", [])}
            signature = json.dumps(stack_request, sort_keys=True, separators=(",", ":"))
            if signature != toolchain_signature and (toolchain_thread is None or not toolchain_thread.is_alive()):
                from .operations import prepare_project_toolchains
                with toolchain_lock:
                    toolchain_result.clear()

                def prepare(request=stack_request):
                    try:
                        extra_tools = request.get("tools", [])
                        if extra_tools:
                            result = prepare_project_toolchains(request["project_stacks"], request["versions"],
                                                                extra_tools)
                        else:
                            result = prepare_project_toolchains(request["project_stacks"], request["versions"])
                        with toolchain_lock:
                            toolchain_result.clear()
                            toolchain_result.update(result)
                    except Exception as exc:  # noqa: BLE001 - failed preflight is reported as a project warning
                        log.warning("project toolchain preparation failed: %s", type(exc).__name__)

                toolchain_signature = signature
                toolchain_thread = threading.Thread(target=prepare, name=f"aiflow4-toolchain-{name}", daemon=True)
                toolchain_thread.start()
            if toolchain_thread is not None and not toolchain_thread.is_alive():
                # Apply the post-install probe once; later heartbeats rely on the regular tool_versions() refresh
                # so tools fixed by hand or by repair are reported again.
                with toolchain_lock:
                    prepared_tools = toolchain_result.pop("tools", None)
                    installed_tools = toolchain_result.pop("installed", [])
                if isinstance(prepared_tools, dict) and prepared_tools:
                    tools.update({str(k): str(v) for k, v in prepared_tools.items()})
                    tools_at = time.time()
                if isinstance(installed_tools, list):
                    auto_fixed_tools.update(str(tool) for tool in installed_tools)
            wanted = sorted({str(s) for services in (desired.get("project_services") or {}).values()
                             for s in services})
            if wanted != service_names:
                service_names = wanted
                service_at = 0.0
                with service_lock:  # a service no project declares any more is no longer reported
                    for stale in set(service_state) - set(wanted):
                        service_state.pop(stale, None)
            if wanted and time.time() - service_at >= SERVICE_CHECK_S and (
                    service_thread is None or not service_thread.is_alive()):
                from .services import prepare_services

                def check_services(names=list(wanted)):
                    result = prepare_services(names)
                    with service_lock:
                        service_state.update(result)

                service_at = time.time()
                service_thread = threading.Thread(target=check_services, name=f"aiflow4-services-{name}", daemon=True)
                service_thread.start()
            if pending_network_events:
                try:
                    api.call("POST", "/v1/events", {"events": list(pending_network_events)})
                    pending_network_events = []
                except Exception as e:  # noqa: BLE001 - retry the event delivery on the next heartbeat
                    log.warning("heartbeat error report not recorded: %s", type(e).__name__)
            if operation_thread is None or not operation_thread.is_alive():
                operation_thread = threading.Thread(
                    target=_process_node_operation_logged, args=(api, name),
                    name=f"aiflow4-node-operation-{name}")
                operation_thread.start()
            try:
                if manage_workers:
                    reconcile(desired.get("workers") or desired.get("accounts", []))
                    clean_scratch()
                    removed_worktrees = clean_worktrees(api, name)
                    if removed_worktrees and isinstance(m.get("disk_free"), (int, float)) and m["disk_free"] < 5 * 1024 ** 3:
                        auto_fixed_conditions.add("disk-space")
                    sweep_test_databases()
                if os.environ.get("AIFLOW4_SELF_UPDATE", "1") == "1":
                    self_update(api, desired.get("release", ""), name)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)
            stop.wait(every_s)
    finally:
        if relay:
            relay.stop()
