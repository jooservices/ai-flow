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
import pty
import pwd
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import select
from urllib.parse import quote
from pathlib import Path
from datetime import datetime, timezone

from .. import VERSION
from ..core.toolchains import (BROKEN_PREFIX, CONTAINER_RUNTIME_TOOL, PLAYWRIGHT_BROWSERS_DIR, STACK_TOOLS,
                               tools_for_stacks)
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
         "gitleaks": ["gitleaks", "version"],
         "xdebug": ["php", "-r", "echo extension_loaded('xdebug') ? 'Xdebug ' . phpversion('xdebug') : 'missing';"],
         "cosign": ["cosign", "version"], "podman": ["podman", "--version"]}
REPORTED_TOOLS = tools_for_stacks(STACK_TOOLS)
SIGN_IN_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login", "--device-auth"],
    "opencode": ["opencode", "auth", "login", "opencode", "--method", "device"],
    "grok": ["grok", "login", "--device-auth"],
}
_SIGN_IN_URL = re.compile(r"https://[^\s\x1b\"'<>]+", re.IGNORECASE)
_SIGN_IN_CODE = re.compile(
    r"(?i)(?:enter|use|type|your|device|user|verification|authorization)?\s*(?:the\s+)?"
    r"(?:device\s+|user\s+|verification\s+|authorization\s+)?code\s*[:=]?\s*"
    r"([A-Z0-9][A-Z0-9-]{3,31})\b")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SIGN_IN_RUN_AS = ["setpriv", "--reuid=aiflow-worker", "--regid=aiflow-worker", "--init-groups", "--"]
_AUDIT_EVENT_ID = re.compile(rb"msg=audit\((\d+(?:\.\d+)?):(\d+)\)")
_AUDIT_TYPE = re.compile(rb"type=([A-Z_]+)")
_AUDIT_AUID = re.compile(rb"\bauid=(\d+)\b")
_AUDIT_EXE = re.compile(rb'\bexe="((?:\\.|[^"\\])*)"')
_AUDIT_PATH = re.compile(rb'\bname="((?:\\.|[^"\\])*)"')


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


def _audit_interventions(raw: bytes) -> list[dict]:
    """Extract human shell/config changes without retaining command arguments or file contents."""
    groups: dict[bytes, list[bytes]] = {}
    current = None
    for line in raw.splitlines():
        match = _AUDIT_EVENT_ID.search(line)
        if not match:
            continue
        event_id = match.group(0)
        if current is not None and event_id != current:
            groups.setdefault(current, []).append(b"")
        groups.setdefault(event_id, []).append(line)
        current = event_id
    results = []
    for event_id, lines in groups.items():
        text = b"\n".join(lines)
        auid = _AUDIT_AUID.search(text)
        if not auid:
            continue
        uid = int(auid.group(1))
        if uid >= 4_294_967_295:
            continue  # unset loginuid: systemd services are not operator sessions
        try:
            actor = pwd.getpwuid(uid).pw_name
        except KeyError:
            actor = str(uid)
        stamp = _AUDIT_EVENT_ID.search(event_id)
        if not stamp:
            continue
        timestamp, serial = stamp.group(1).decode(), stamp.group(2).decode()
        try:
            happened_at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        types = {m.group(1) for line in lines if (m := _AUDIT_TYPE.search(line))}
        exe = _AUDIT_EXE.search(text)
        if b"EXECVE" in types and exe:
            command = os.path.basename(exe.group(1).decode("utf-8", "replace").replace(r"\x22", '"'))[:120]
            results.append({"source_key": f"{timestamp}:{serial}:shell", "source": "shell", "actor": actor,
                            "action": "execute", "target": command, "happened_at": happened_at,
                            "reason": f"Interactive host session invoked {command}; auditd captured the executable name."})
        if b"PATH" in types and b"SYSCALL" in types:
            paths = [m.group(1).decode("utf-8", "replace") for line in lines if _AUDIT_TYPE.search(line)
                     and _AUDIT_TYPE.search(line).group(1) == b"PATH" for m in _AUDIT_PATH.finditer(line)]
            for filename in sorted(set(os.path.basename(path) for path in paths if path)):
                results.append({"source_key": f"{timestamp}:{serial}:config:{filename}", "source": "configuration",
                                "actor": actor, "action": "write", "target": filename[:120],
                                "happened_at": happened_at,
                                "reason": f"Interactive host session changed {filename}; auditd captured the file name."})
    return _group_interventions(results)


def _group_interventions(events: list[dict]) -> list[dict]:
    """One intervention per actor and source in a batch: an SSH session or a deploy is one intervention, not one
    per command or file written. The first event names the group; the others are counted and sampled."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for event in sorted(events, key=lambda e: e["happened_at"]):
        groups.setdefault((event["actor"], event["source"]), []).append(event)
    out = []
    for members in groups.values():
        first = dict(members[0])
        targets = list(dict.fromkeys(m["target"] for m in members))
        first["details"] = {"events": len(members), "targets": targets[:10], "last_at": members[-1]["happened_at"]}
        if len(members) > 1:
            first["action"] = "session"
            first["target"] = ", ".join(targets[:3])[:300]
        out.append(first)
    return out


def forward_operator_audit(api: ControlClient, node: str, audit_path: str = "/var/log/audit/audit.log",
                           cursor_path: str = "/var/lib/aiflow4/operator-audit.cursor") -> int:
    """Forward auditd records created by human login sessions, keeping only an inode and byte cursor."""
    path, cursor = Path(audit_path), Path(cursor_path)
    if not path.is_file():
        return 0
    cursor.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(cursor.read_text(encoding="utf-8")) if cursor.exists() else {}
    except (OSError, ValueError):
        state = {}
    stat = path.stat()
    offset = int(state.get("offset", 0)) if int(state.get("inode", -1)) == stat.st_ino else 0
    with path.open("rb") as stream:
        stream.seek(min(offset, stat.st_size))
        data = stream.read()
        end = stream.tell()
    complete_length = data.rfind(b"\n") + 1
    complete = data[:complete_length]
    events = _audit_interventions(complete)
    for event in events:
        api.call("POST", f"/v1/nodes/{quote(node, safe='')}/operator-interventions", event, retries=1)
    tmp = cursor.with_suffix(".tmp")
    tmp.write_text(json.dumps({"inode": stat.st_ino, "offset": offset + complete_length}), encoding="utf-8")
    os.replace(tmp, cursor)
    return len(events)


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


def systemctl(*args: str) -> int:
    return subprocess.run(["systemctl", *args], capture_output=True).returncode


def ensure_account_home(instance: str, accounts_root: str = "/srv/aiflow/accounts") -> bool:
    """Create a newly console-assigned account's private home before systemd starts its worker."""
    account = re.sub(r"\.\d+$", "", instance)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", account):
        return False
    try:
        import pwd
        worker = pwd.getpwnam("aiflow-worker")
        root = Path(accounts_root)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        home = root / account
        if home.is_symlink():
            return False
        home.mkdir(mode=0o700, exist_ok=True)
        info = home.stat(follow_symlinks=False)
        if not home.is_dir() or home.resolve(strict=True).parent != root:
            return False
        if info.st_uid != worker.pw_uid or info.st_gid != worker.pw_gid or info.st_mode & 0o777 != 0o700:
            os.chown(home, worker.pw_uid, worker.pw_gid)
            os.chmod(home, 0o700)
        return True
    except (OSError, KeyError, ImportError):
        log.warning("could not prepare account home for %s", account)
        return False


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


def provider_sign_in_command(provider: str) -> list[str] | None:
    """Return the provider CLI's own login command; authorization codes come from that command."""
    command = SIGN_IN_COMMANDS.get(provider)
    return list(command) if command else None


def provider_sign_in_details(output: str) -> tuple[str, str]:
    """Extract only the provider authorization URL and device code from terminal output."""
    clean = _ANSI.sub("", output)
    url_match = _SIGN_IN_URL.search(clean)
    code_match = _SIGN_IN_CODE.search(clean)
    url = url_match.group(0).rstrip(".,;:)") if url_match else ""
    code = code_match.group(1).strip() if code_match else ""
    return url, code


def _post_sign_in_status(api: ControlClient, node: str, account: str, request_id: str, status: str,
                         sign_in_url: str = "", code: str = "", error: str = "") -> None:
    api.call("POST", f"/v1/accounts/{quote(account, safe='')}/sign-in/status",
             {"request_id": request_id, "status": status, "sign_in_url": sign_in_url,
              "code": code, "error": error[:300]}, retries=1)


def _post_shared_sign_in_prompt(api: ControlClient, account: str, sign_in_url: str, code: str) -> None:
    """Relay only the provider's short-lived device handoff; the machine keeps the resulting credentials."""
    api.call("POST", f"/v1/accounts/{quote(account, safe='')}/signin-prompt",
             {"link": sign_in_url, "code": code, "expires_in_s": 900}, retries=1)


def _post_shared_sign_in_complete(api: ControlClient, account: str, request_id: str) -> None:
    """Report the provider CLI's successful login so the node can start its normal credential probe."""
    api.call("POST", f"/v1/accounts/{quote(account, safe='')}/signin-complete",
             {"request_id": request_id}, retries=1)


def run_provider_sign_in(api: ControlClient, node: str, request: dict, cancel: threading.Event,
                         accounts_root: str = "/srv/aiflow/accounts") -> None:
    """Run a provider login inside its private account home and relay its device handoff to the console."""
    account = str(request.get("account") or "")
    request_id = str(request.get("request_id") or "")
    provider = str(request.get("provider") or "")
    shared_account = bool(request.get("shared_account"))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", account) or (not request_id and not shared_account):
        return
    command = provider_sign_in_command(provider)
    if not command:
        try:
            if shared_account:
                return
            _post_sign_in_status(api, node, account, request_id, "failed", error="Provider sign-in is unsupported.")
        except Exception:  # noqa: BLE001 - do not log provider output or auth details
            pass
        return

    try:
        if not shared_account:
            _post_sign_in_status(api, node, account, request_id, "starting")
        if not ensure_account_home(account, accounts_root):
            if not shared_account:
                _post_sign_in_status(api, node, account, request_id, "failed",
                                     error="Could not prepare the private account home on this machine.")
            return
        home = str(Path(accounts_root) / account)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": home,
               "USER": "aiflow-worker", "LOGNAME": "aiflow-worker", "TERM": "xterm-256color",
               "CODEX_HOME": str(Path(home) / ".codex"),
               "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude"),
               "XDG_DATA_HOME": str(Path(home) / ".local" / "share")}
        master_fd, slave_fd = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(_SIGN_IN_RUN_AS + command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                       env=env, close_fds=True, start_new_session=True)
            os.close(slave_fd)
            slave_fd = -1
            output = ""
            last_details = ("", "")
            copied_auth_url = False
            while True:
                if cancel.is_set():
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    return
                readable, _, _ = select.select([master_fd], [], [], 0.25)
                if not readable:
                    if process.poll() is not None:
                        break
                    continue
                try:
                    chunk = os.read(master_fd, 4096).decode("utf-8", errors="replace")
                except OSError:  # Linux PTYs report EIO once the child has closed its slave.
                    break
                if not chunk:
                    continue
                output = (output + chunk)[-16_000:]
                clean_output = _ANSI.sub("", output)
                if not copied_auth_url and re.search(
                        r"(?i)(?:press|type)\s+c\b.{0,80}(?:copy|url|browser)|"
                        r"(?:copy|show).{0,50}auth(?:entication)?\s+url.{0,40}\bc\b", clean_output):
                    os.write(master_fd, b"c")
                    copied_auth_url = True
                details = provider_sign_in_details(clean_output)
                if details[0] and details != last_details and (not shared_account or details[1]):
                    if shared_account:
                        _post_shared_sign_in_prompt(api, account, details[0], details[1])
                    else:
                        _post_sign_in_status(api, node, account, request_id, "waiting",
                                             sign_in_url=details[0], code=details[1])
                    last_details = details
            return_code = process.wait(timeout=10)
            if cancel.is_set():
                return
            if not shared_account:
                if return_code == 0:
                    _post_sign_in_status(api, node, account, request_id, "completed",
                                         sign_in_url=last_details[0], code=last_details[1])
                else:
                    _post_sign_in_status(api, node, account, request_id, "failed",
                                         sign_in_url=last_details[0], code=last_details[1],
                                         error=f"Provider sign-in exited with status {return_code}.")
            elif return_code == 0:
                # The next heartbeat starts the worker, whose first action is the usual credential probe. This
                # completion report breaks the pending-assignment/worker-probe cycle without moving credentials.
                _post_shared_sign_in_complete(api, account, request_id)
        finally:
            if slave_fd >= 0:
                os.close(slave_fd)
            os.close(master_fd)
    except Exception as exc:  # noqa: BLE001 - never include CLI output, tokens, or paths in logs or API errors
        if cancel.is_set():
            return
        try:
            if not shared_account:
                _post_sign_in_status(api, node, account, request_id, "failed",
                                     error=f"Provider sign-in could not start ({type(exc).__name__}).")
        except Exception:  # noqa: BLE001
            pass
        log.warning("provider sign-in failed for %s (%s)", account, type(exc).__name__)


def sync_provider_sign_ins(api: ControlClient, node: str, requests: list[dict],
                           active: dict[str, tuple[str, threading.Event, threading.Thread]]) -> None:
    """Keep one provider login process per account and cancel replaced or removed handoffs."""
    desired = {str(item.get("account") or ""): item for item in requests
               if isinstance(item, dict) and item.get("account")}
    for account, (request_id, cancel, thread) in list(active.items()):
        current = desired.get(account)
        if not current or current.get("request_id") != request_id:
            if thread.is_alive():
                cancel.set()
            else:
                del active[account]
    for account, item in desired.items():
        request_id = str(item.get("request_id") or "")
        current = active.get(account)
        if current and current[0] == request_id:
            continue
        if current and current[2].is_alive():
            continue
        cancel = threading.Event()
        thread = threading.Thread(target=run_provider_sign_in, args=(api, node, item, cancel),
                                  name=f"aiflow4-sign-in-{account}", daemon=True)
        active[account] = (request_id, cancel, thread)
        thread.start()


_update_tried: dict[str, float] = {}
_last_clean = 0.0
_last_sweep = 0.0
_DEVELOP_BUILD = re.compile(r"[0-9a-f]{7,40}\Z")
_RELEASE_VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_RELEASE_WORKFLOW_IDENTITY = "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/{}"
_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
RELEASE_INCOMING_DIR = Path("/srv/aiflow/releases/incoming")
_WORKTREE_CLEANUP_IMAGE = "busybox:1.36.1"


def _remove_e2e_artifacts_as_root(worktree: Path) -> None:
    """Use a disposable root container to clear generated artifacts before host-side worktree removal.

    The container sees only this worktree and removes its artifacts directory, which is disposable because
    the worktree itself has already been approved for deletion. Removing the directory inside the container
    also handles root-owned, read-only directory entries that the node agent cannot chmod or unlink.
    """
    artifacts = worktree / "artifacts"
    e2e = artifacts / "reports" / "e2e"
    if not e2e.is_dir() or any(part.is_symlink() for part in (artifacts, artifacts / "reports", e2e)):
        return

    source = str(worktree.resolve(strict=True))
    if "," in source:
        raise OSError("worktree path contains a comma and cannot be mounted safely")
    command = [
        "docker", "run", "--rm", "--pull=missing", "--user=0:0", "--network=none", "--read-only",
        "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
        "--mount", f"type=bind,source={source},target=/worktree",
        _WORKTREE_CLEANUP_IMAGE, "rm", "-rf", "--", "/worktree/artifacts",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise OSError("root cleanup container timed out after 120 seconds") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit status {result.returncode}").strip().splitlines()
        raise OSError(f"root cleanup container failed: {detail[0][:240] if detail else result.returncode}")


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
            _remove_e2e_artifacts_as_root(path)
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
    """Claim and run at most one server-created whitelist operation."""
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


def run_agent(control_url: str, name: str, token: str, slots: int = 2, every_s: int = 15, manage_workers: bool = True,
              stop: threading.Event | None = None) -> None:
    api = ControlClient(control_url, token, timeout=15)
    stop = stop or threading.Event()
    relay = PolicyRelay(control_url) if os.environ.get("AIFLOW4_POLICY_RELAY", "1") == "1" else None
    if relay:
        relay.start()
    tools, tools_at, prev = tool_versions(), time.time(), None
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
    container_cleanup_thread: threading.Thread | None = None
    container_cleanup_at = 0.0
    sign_in_processes: dict[str, tuple[str, threading.Event, threading.Thread]] = {}
    probe_thread: threading.Thread | None = None
    probe_tried: dict[tuple[str, str], float] = {}  # (project, signature) -> when the run started
    ready_notified = False
    try:
        while not stop.is_set():
            try:
                m, prev = metrics(prev)
                if time.time() - tools_at > 600:
                    tools, tools_at = tool_versions(), time.time()
                ws = worker_states()
                used = sum(1 for w in ws if w.get("state") == "working" and w.get("alive"))
                desired = api.call("POST", f"/v1/nodes/{name}/heartbeat", {
                    "slots_total": slots, "slots_used": used, "release_version": installed_release() or VERSION,
                    "release_protocol": 1,
                    "tools": tools,
                    "services": _services_snapshot(service_state, service_lock),
                    "metrics": {**m, "hostname": socket.gethostname(),
                                "policy_relay": relay.health() if relay else {"enabled": False, "healthy": False}},
                    "workers": ws, "credential_expiries": credential_expiries(ws)}, retries=2)["desired"]
                if not ready_notified:
                    ready_notified = notify_systemd("READY=1\nSTATUS=First control heartbeat succeeded")
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
            try:
                forward_operator_audit(api, name)
            except Exception as e:  # noqa: BLE001 - retain the cursor so the next heartbeat retries delivery
                log.warning("host audit forwarding failed (%s)", type(e).__name__)
            sign_in_requests = [*(desired.get("sign_ins") or []), *(desired.get("signin") or [])]
            sync_provider_sign_ins(api, name, sign_in_requests, sign_in_processes)
            stack_request = {"release": desired.get("release", ""),
                             "project_stacks": desired.get("project_stacks", {}),
                             "versions": desired.get("versions", {}),
                             "requested_tools": desired.get("tool_repairs", [])}
            signature = json.dumps(stack_request, sort_keys=True, separators=(",", ":"))
            if signature != toolchain_signature and (toolchain_thread is None or not toolchain_thread.is_alive()):
                from .operations import prepare_project_toolchains
                with toolchain_lock:
                    toolchain_result.clear()

                def prepare(request=stack_request):
                    try:
                        if request["requested_tools"]:
                            result = prepare_project_toolchains(request["project_stacks"], request["versions"],
                                                                request["requested_tools"])
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
                    request_gaps = toolchain_result.pop("request_gaps", [])
                if isinstance(prepared_tools, dict) and prepared_tools:
                    tools.update({str(k): str(v) for k, v in prepared_tools.items()})
                    tools_at = time.time()
                if request_gaps:
                    # A failed install is retried at the next heartbeat while the control plane still requests it.
                    toolchain_signature = ""
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
            # check commands of the projects on a clean checkout: once per project and set of commands (control asks)
            asked = [p for p in desired.get("check_probes") or []
                     if time.time() - probe_tried.get((p["project"], p["signature"]), 0.0) > PROBE_RETRY_S]
            if asked and manage_workers and (probe_thread is None or not probe_thread.is_alive()):
                from .probes import run_check_probes
                from ..worker.loop import RUN_AS
                for p in asked:
                    probe_tried[(p["project"], p["signature"])] = time.time()

                def probe_checks(items=asked):
                    try:
                        run_check_probes(api, name, items, "/srv/aiflow", RUN_AS if _has_worker_user() else [])
                    except Exception as exc:  # noqa: BLE001 - control asks again after PROBE_RETRY_S
                        log.warning("project check probe failed: %s", type(exc).__name__)

                probe_thread = threading.Thread(target=probe_checks, name=f"aiflow4-check-probe-{name}", daemon=True)
                probe_thread.start()
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
                    clean_worktrees(api, name)
                    sweep_test_databases()
                    if (tools.get(CONTAINER_RUNTIME_TOOL, "missing") != "missing"
                            and time.time() - container_cleanup_at >= 24 * 60 * 60
                            and (container_cleanup_thread is None or not container_cleanup_thread.is_alive())):
                        from ..worker.containers import prune_image_cache

                        container_cleanup_at = time.time()
                        container_cleanup_thread = threading.Thread(
                            target=prune_image_cache, name=f"aiflow4-container-prune-{name}", daemon=True)
                        container_cleanup_thread.start()
                if os.environ.get("AIFLOW4_SELF_UPDATE", "1") == "1":
                    self_update(api, desired.get("release", ""), name)
            except Exception as e:  # noqa: BLE001
                log.warning("heartbeat failed: %s", e)
            stop.wait(every_s)
    finally:
        for _request_id, cancel, _thread in sign_in_processes.values():
            cancel.set()
        if relay:
            relay.stop()
