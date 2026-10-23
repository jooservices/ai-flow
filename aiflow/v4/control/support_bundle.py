"""Build reviewed support bundles from a bounded, allow-listed v4 snapshot.

Bundles are assembled in memory, scanned before they can be downloaded, and kept only briefly for the
owner to review their inventory. Raw secrets and common personal identifiers are removed before the scan.
"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import platform
import re
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from .. import VERSION

MAX_LOG_BYTES = 5 * 1024 * 1024
RETENTION = timedelta(hours=24)
MAX_INCIDENTS = 100
RUN_LOG_TTL_S = int(RETENTION.total_seconds()) + 3600

_log = logging.getLogger("aiflow4.support_bundle")

_SECRET_NAME = r"(?:[A-Za-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|session|cookie)[A-Za-z0-9_-]*|key)"
_SECRET_ASSIGNMENT = re.compile(
    rf"(?im)(?P<prefix>\b{_SECRET_NAME}\b[ \t]*\\?[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>\[REDACTED\]|\\?\"(?:\\.|[^\"\\\r\n])*?\\?\"|\\?'(?:\\.|[^'\\\r\n])*?\\?'|[^\r\n]*?)"
    r"(?P<suffix>(?:\\?[\"'{}\[\],][ \t]*)*)(?=\r?\n|\Z)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|sk-ant-[A-Za-z0-9_-]{20,}|"
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|SK[0-9a-fA-F]{32}|sk-[A-Za-z0-9_-]{16,})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_URL_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_PHONE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\d{10}|\(\d{2,4}\)[ .-]?\d{3,4}[ .-]?\d{4}|"
    r"\d{3}[ .-]\d{3}[ .-]\d{4}|\d{2,4}[ .-]\d{3,4}[ .-]\d{4})(?!\w)"
)
_SSN = re.compile(r"(?<!\d)\d{3}(?:[- ]?\d{2})(?:[- ]?\d{4})(?!\d)")
# Unlabeled names use capitalization; labeled person fields below also catch lowercase names.
_NAME = re.compile(r"\b[A-Z][A-Za-z'-]{1,30}\s+[A-Z][A-Za-z'-]{1,30}(?:\s+[A-Z][A-Za-z'-]{1,30})?\b")
_PERSON_FIELD = re.compile(
    r"(?i)(?P<prefix>\b(?:owner|assignee|reporter|contact|person|name|customer(?:[ _-]?name)?|"
    r"(?:first|last|full)[ _-]?name|user(?:[ _-]?name)?)"
    r"\b\s*\\?[\"']?\s*[:=]\s*\\?[\"']?)"
    r"(?P<value>[A-Za-z][A-Za-z'-]{0,30}(?:[._][A-Za-z][A-Za-z'-]{0,30})*"
    r"(?:[ \t]+[A-Za-z][A-Za-z'-]{0,30}){0,2})\b(?P<suffix>\\?[\"']?)"
)
_PERSON_MENTION = re.compile(
    r"(?i)(?P<prefix>\b(?:assigned\s+to|reported\s+by|contacted|called|spoke\s+with|"
    r"talked\s+to|met\s+with)\s+)"
    r"(?P<value>[a-z][a-z'-]{1,30}[ \t]+[a-z][a-z'-]{1,30})\b"
)
_DOB_FIELD = re.compile(
    r"(?i)(?P<prefix>\b(?:(?:customer|user|person)\s+)?(?:date[ _-]+of[ _-]+birth|"
    r"birth[ _-]?date|dob)\b[ \t]*\\?[\"']?[ \t]*[:=][ \t]*\\?[\"']?)"
    r"(?P<value>\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"(?P<suffix>\\?[\"']?)"
)
_ADDRESS = re.compile(r"(?i)\b\d{1,6}\s+(?:[A-Z0-9.'-]+\s+){1,5}(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|court|ct|way)\b")
_HOME_PATH = re.compile(r"(?i)(/(?:home|users)/)[^/\s]+")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def _redact_ip(match: re.Match[str]) -> str:
    value = match.group(0)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    return "[REDACTED_IP]"


_IP = re.compile(r"(?<![A-Fa-f0-9])(?:\d{1,3}\.){3}\d{1,3}(?![A-Fa-f0-9])|(?<![A-Fa-f0-9:])(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}(?![A-Fa-f0-9:])")


def sanitize_text(text: str) -> str:
    """Remove recognizable credentials and personal identifiers before writing bundle files."""
    text = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    text = _SECRET_ASSIGNMENT.sub(_redact_secret_assignment, text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KNOWN_TOKEN.sub("[REDACTED_TOKEN]", text)
    text = _JWT.sub("[REDACTED_TOKEN]", text)
    text = _URL_CREDENTIALS.sub("://[REDACTED]@", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    text = _SSN.sub("[REDACTED_ID]", text)
    text = _DOB_FIELD.sub(_redact_dob, text)
    text = _ADDRESS.sub("[REDACTED_ADDRESS]", text)
    text = _IP.sub(_redact_ip, text)
    text = _HOME_PATH.sub(r"\1[REDACTED_USER]", text)
    text = _NAME.sub(_redact_name, text)
    text = _PERSON_FIELD.sub(_redact_person_field, text)
    return _PERSON_MENTION.sub(_redact_person_field, text)


def _redact_name(match: re.Match[str]) -> str:
    return "[REDACTED_NAME]"


def _redact_person_field(match: re.Match[str]) -> str:
    return match.group("prefix") + "[REDACTED_NAME]" + match.groupdict().get("suffix", "")


def _redact_dob(match: re.Match[str]) -> str:
    return match.group("prefix") + "[REDACTED_DATE]" + match.group("suffix")


def _redact_secret_assignment(match: re.Match[str]) -> str:
    """Keep assignment syntax valid while replacing the complete quoted or unquoted value."""
    value = match.group("value")
    if value.startswith('\\"') and value.endswith('\\"'):
        redacted = '\\"[REDACTED]\\"'
    elif value.startswith("\\'") and value.endswith("\\'"):
        redacted = "\\'[REDACTED]\\'"
    elif value.startswith('"') and value.endswith('"'):
        redacted = '"[REDACTED]"'
    elif value.startswith("'") and value.endswith("'"):
        redacted = "'[REDACTED]'"
    else:
        redacted = "[REDACTED]"
    return match.group("prefix") + redacted + match.group("suffix")


def sensitive_findings(text: str) -> list[str]:
    """Return names of sensitive-data classes still present after sanitization."""
    findings = []
    if (_PRIVATE_KEY.search(text) or _BEARER.search(text) or _URL_CREDENTIALS.search(text)
            or _unredacted_secret_line(text)):
        findings.append("credential")
    if _KNOWN_TOKEN.search(text) or _JWT.search(text):
        findings.append("token")
    has_ip = any(_is_valid_ip(match.group(0)) for match in _IP.finditer(text))
    has_name = bool(_NAME.search(text) or _PERSON_FIELD.search(text) or _PERSON_MENTION.search(text))
    if (_EMAIL.search(text) or _PHONE.search(text) or _SSN.search(text) or _DOB_FIELD.search(text)
            or _ADDRESS.search(text) or has_ip or has_name):
        findings.append("personal_data")
    return findings


_SCAN_SECRET_LINE = re.compile(rf"(?i)\b{_SECRET_NAME}\b[ \t]*\\?[\"']?[ \t]*[:=][ \t]*(?P<rest>[^\r\n]*)")
_SCAN_REDACTED = re.compile(r"""(?:\\?["'])?\[REDACTED\](?:\\?["'])?(?:\\?["'{}\[\],]|[ \t])*""")


def _unredacted_secret_line(text: str) -> bool:
    """Line-by-line scan, independent of the sanitizer pattern: a secret name must be followed only by a redaction marker."""
    for line in text.splitlines():
        for match in _SCAN_SECRET_LINE.finditer(line):
            rest = match.group("rest").strip()
            if rest and not _SCAN_REDACTED.fullmatch(rest):
                return True
    return False


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def cap_log_bytes(text: str, max_bytes: int = MAX_LOG_BYTES) -> tuple[str, bool]:
    """Keep the newest bytes of a log file without cutting a UTF-8 code point."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return raw.decode("utf-8"), False
    if max_bytes <= 0:
        return "", True
    start = len(raw) - max_bytes
    while True:
        try:
            return raw[start:].decode("utf-8"), True
        except UnicodeDecodeError:
            # The encoded input is valid UTF-8; only the first code point can be split.
            start += 1


class SupportLogBuffer(logging.Handler):
    """Thread-safe in-process ring of control service logs, limited to the last 24 hours."""

    def __init__(self, max_records: int = 20_000) -> None:
        super().__init__(logging.INFO)
        self.records: deque[tuple[float, str]] = deque(maxlen=max_records)
        self.lock = threading.RLock()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
        formatter.converter = time.gmtime
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            with self.lock:
                self._prune(time.time())
                self.records.append((record.created, line))
        except Exception:
            self.handleError(record)

    def _prune(self, now: float) -> None:
        cutoff = now - RETENTION.total_seconds()
        while self.records and self.records[0][0] < cutoff:
            self.records.popleft()

    def text(self) -> str:
        with self.lock:
            self._prune(time.time())
            lines = [line for _, line in self.records]
        return "\n".join(lines) + ("\n" if lines else "")


_control_log_buffer = SupportLogBuffer()
_control_log_buffer_installed = False
_control_log_install_lock = threading.Lock()


def control_log_buffer(log_dir: str = "", source: str = "control") -> SupportLogBuffer:
    """Install the bounded ring and, when configured, persistent rotated logs on the v4 logger tree."""
    global _control_log_buffer_installed
    with _control_log_install_lock:
        logger = logging.getLogger("aiflow4")
        if not _control_log_buffer_installed:
            logger.addHandler(_control_log_buffer)
            _control_log_buffer_installed = True
        if log_dir:
            try:
                directory = Path(log_dir)
                directory.mkdir(parents=True, exist_ok=True)
                filename = re.sub(r"[^A-Za-z0-9_.-]", "_", source or "control")[:80] or "control"
                path = (directory / f"{filename}.log").resolve()
                if not any(getattr(handler, "_aiflow_support_path", None) == path for handler in logger.handlers):
                    handler = TimedRotatingFileHandler(path, when="midnight", interval=1, backupCount=2,
                                                       encoding="utf-8", utc=True)
                    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                                  "%Y-%m-%d %H:%M:%S")
                    formatter.converter = time.gmtime
                    handler.setFormatter(formatter)
                    handler._aiflow_support_path = path  # type: ignore[attr-defined]
                    logger.addHandler(handler)
            except OSError:
                logging.getLogger(__name__).warning("persistent support log capture is unavailable")
    return _control_log_buffer


def _persistent_control_logs(directory: str, now: datetime) -> str:
    if not directory:
        return ""
    root = Path(directory)
    if not root.is_dir():
        return ""
    cutoff = now - RETENTION
    output: list[str] = []
    for path in sorted(root.glob("*.log*")):
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[A-Za-z0-9_.-]+\.log(?:\.\d{4}-\d{2}-\d{2})?", path.name):
            continue
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                if size > MAX_LOG_BYTES:
                    stream.seek(-MAX_LOG_BYTES, 2)
                text = stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        current_line_is_recent = False
        recent: list[str] = []
        for line in text.splitlines():
            try:
                stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                if current_line_is_recent:
                    recent.append(line)
                continue
            current_line_is_recent = stamp >= cutoff
            if current_line_is_recent:
                recent.append(line)
        if recent:
            output.append(f"--- {path.name} ---")
            output.extend(recent)
    return "\n".join(output) + ("\n" if output else "")


def _run_log_key(ctx, run_id: str) -> str:
    return ctx.leases._k("support_run_log", str(run_id))


def store_run_log(ctx, run_id: str, text: str) -> bool:
    """Keep a sanitized, scanned run log in Redis for the 24 hour window; never in the runs table."""
    safe, _ = cap_log_bytes(sanitize_text(text))
    if sensitive_findings(safe):
        _log.warning("run log for %s dropped: sensitive content remained after sanitization", run_id)
        return False
    ctx.leases.r.set(_run_log_key(ctx, run_id), safe, ex=RUN_LOG_TTL_S)
    return True


def load_run_log(ctx, run_id: str) -> str | None:
    value = ctx.leases.r.get(_run_log_key(ctx, run_id))
    if value is None:
        return None
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _safe_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str, indent=2, sort_keys=True)
    return sanitize_text(raw) + "\n"


def _file_payloads(ctx, ticket_key: str | None, log_buffer: SupportLogBuffer) -> dict[str, bytes]:
    repo = ctx.repo()
    now = datetime.now(timezone.utc)
    version = str(ctx.settings.control_version or VERSION)
    nodes = ctx.db.all("SELECT name,role,status,slots_total,slots_used,release_version,last_heartbeat,tools"
                       " FROM nodes ORDER BY name")
    machines = []
    for row in nodes:
        heartbeat = row["last_heartbeat"]
        machines.append({"name": row["name"], "role": row["role"], "status": row["status"],
                         "slots_total": row["slots_total"], "slots_used": row["slots_used"],
                         "release": row["release_version"] or "unknown",
                         "heartbeat_age_seconds": int((now - heartbeat).total_seconds()) if heartbeat else None,
                         "tools": row["tools"] or {}})
    accounts = ctx.db.all("SELECT id,provider,node,tier,roles,status,updated_at FROM accounts ORDER BY id")
    account_states = [{"id": row["id"], "provider": row["provider"], "node": row["node"],
                       "tier": row["tier"], "roles": row["roles"], "status": row["status"],
                       "updated_at": _iso(row["updated_at"])} for row in accounts]
    incidents = ctx.db.all(
        "SELECT id,opened_at,closed_at,severity,kind,subject,title,detail FROM incidents"
        " WHERE opened_at >= now() - interval '7 days' ORDER BY opened_at DESC LIMIT %s", (MAX_INCIDENTS,))
    # Incident prose is owner-controlled free-form text. Heuristics cannot reliably identify
    # every personal name in it, so retain incident metadata but never export those fields.
    incident_data = [{"id": row["id"], "opened_at": _iso(row["opened_at"]), "closed_at": _iso(row["closed_at"]),
                      "severity": row["severity"], "kind": row["kind"],
                      "subject": "[REDACTED_FREEFORM]", "title": "[REDACTED_FREEFORM]",
                      "detail": "[REDACTED_FREEFORM]"} for row in incidents]

    files: dict[str, bytes] = {
        "versions.json": _safe_json({"aiflow": version, "python": platform.python_version(),
                                      "platform": platform.system().lower(),
                                      "nodes": [{"name": row["name"], "release": row["release_version"] or "unknown"}
                                                for row in nodes]}).encode("utf-8"),
        "settings.json": _safe_json({"environment": ctx.settings.env, "console_login_enabled": bool(ctx.settings.console_login),
                                      "control_version": version, "projects": sorted(repo.projects),
                                      "configured_nodes": sorted(repo.nodes),
                                      "configured_account_count": len(repo.accounts)}).encode("utf-8"),
        "machine-state.json": _safe_json(machines).encode("utf-8"),
        "account-state.json": _safe_json(account_states).encode("utf-8"),
        "incidents.json": _safe_json(incident_data).encode("utf-8"),
    }

    log_text = _persistent_control_logs(ctx.settings.support_log_dir, now) or log_buffer.text()
    log_text = log_text or "No control service log records were captured in the last 24 hours.\n"
    safe_log, _ = cap_log_bytes(sanitize_text(log_text))
    files["logs/control-service.log"] = safe_log.encode("utf-8")

    if ticket_key:
        ticket = ctx.db.one("SELECT key,project,status,type,summary FROM tickets WHERE key=%s", (ticket_key,))
        if not ticket:
            raise KeyError(ticket_key)
        ticket_data = dict(ticket)
        ticket_data["ticket_key"] = ticket_data.pop("key")
        files["ticket.json"] = _safe_json(ticket_data).encode("utf-8")
        rows = ctx.db.all("SELECT id,step,started_at FROM runs WHERE ticket_key=%s"
                          " AND started_at >= now() - interval '24 hours' ORDER BY started_at,id", (ticket_key,))
        for row in rows:
            run_log = load_run_log(ctx, row["id"])
            if run_log is None:
                continue
            safe_log, _ = cap_log_bytes(sanitize_text(run_log))
            files[f"logs/ticket-runs/{row['id']}-{row['step']}.log"] = safe_log.encode("utf-8")
    return files


def build_support_bundle(ctx, ticket_key: str | None = None, log_buffer: SupportLogBuffer | None = None) -> tuple[bytes, dict]:
    """Create and scan an in-memory zip; raise ValueError if anything sensitive remains."""
    if ticket_key and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,31}-[1-9][0-9]{0,8}", ticket_key):
        raise KeyError(ticket_key)
    files = _file_payloads(ctx, ticket_key, log_buffer or control_log_buffer())
    created_at = datetime.now(timezone.utc).isoformat()
    inventory = {"created_at": created_at, "ticket_key": ticket_key, "scan": "passed",
                 "files": [{"name": name, "bytes": len(content)} for name, content in files.items()],
                 "incident_count": len(json.loads(files["incidents.json"])),
                 "ticket_run_log_count": sum(name.startswith("logs/ticket-runs/") for name in files)}
    files["inventory.json"] = _safe_json(inventory).encode("utf-8")
    findings = [(name, item) for name, content in files.items()
                for item in sensitive_findings(content.decode("utf-8", errors="replace"))]
    if findings:
        raise ValueError("sensitive content remains after sanitization")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            if name.startswith("logs/") and len(content) > MAX_LOG_BYTES:
                raise ValueError("log file exceeds the configured size limit")
            archive.writestr(name, content)
    inventory["files"].append({"name": "inventory.json", "bytes": len(files["inventory.json"])})
    return output.getvalue(), inventory
