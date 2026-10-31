"""Manage provider sign-in subprocesses and prepare their private account homes."""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import threading
import logging
from pathlib import Path
from urllib.parse import quote

from ..worker.client import ControlClient

log = logging.getLogger("aiflow4.node")

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
                    break  # macOS PTYs report EOF as an empty read rather than EIO.
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
