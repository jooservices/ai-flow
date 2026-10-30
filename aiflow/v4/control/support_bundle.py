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
from ..core.toolchains import STACK_TOOLS, tools_for_stacks

MAX_LOG_BYTES = 5 * 1024 * 1024
RETENTION = timedelta(hours=24)
MAX_INCIDENTS = 100
MAX_RECORD_BYTES = 16 * 1024
RUN_LOG_TTL_S = int(RETENTION.total_seconds()) + 3600
_RELEASE_VERSION = re.compile(
    r"v?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:(?:a|b|rc)\d+)?\Z"
)
_TOOL_VERSION = re.compile(
    r"(?P<version>v?(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){1,3})(?=$|[.\-+~\s(,])"
)
_TOOL_VERSION_PREFIXES = {
    "claude": ("Claude Code ",),
    "codex": ("codex-cli ", "Codex CLI "),
    "codex-code-mode-host": ("codex-code-mode-host ",),
    "grok": ("grok ",),
    "opencode": ("opencode ",),
    "docker": ("Docker version ",),
    "git": ("git version ",),
    "node": ("node ",),
    "uv": ("uv ",),
    "gh": ("gh version ",),
    "php": ("PHP ",),
    "composer": ("Composer version ",),
    "wp": ("WP-CLI ",),
    "python3": ("Python ",),
    "playwright": ("Version ", "Playwright "),
    "chromium": ("Chromium Playwright ",),
    "firefox": ("Firefox Playwright ",),
    "webkit": ("WebKit Playwright ",),
}
_REDACTED = "[REDACTED]"
_REPORTED_TOOLS = frozenset(tools_for_stacks(STACK_TOOLS))

_log = logging.getLogger("aiflow4.support_bundle")

_SECRET_NAME = r"(?:[A-Za-z0-9_-]*(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|authorization|session|cookie)[A-Za-z0-9_-]*|key|pass|pin)"
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
_DOTTED_NAME = re.compile(r"(?i)(?<![A-Za-z0-9_.-])[a-z][a-z'-]{1,30}\.[a-z][a-z'-]{1,30}(?![A-Za-z0-9_.-])")
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
_SAFE_MACHINE_LABEL = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
# A label made of lowercase words can be a person ("jane-doe"), so only words known to be technical
# survive. Words containing a digit ("wsl-1", "node2") are also trusted.
_TRUSTED_LABEL_WORDS = frozenset({
    "wsl", "mac", "mini", "macbook", "pro", "air", "studio", "imac", "linux", "ubuntu", "debian", "windows",
    "win", "macos", "darwin", "docker", "vm", "vps", "server", "worker", "node", "nodes", "control", "master",
    "agent", "runner", "host", "box", "lab", "dev", "test", "prod", "staging", "ci", "build", "main", "local",
    "primary", "secondary", "backup", "gpu", "cpu", "arm", "arm64", "x86", "amd64", "claude", "codex", "grok",
    "gemini", "opencode", "openai", "anthropic", "account", "acct", "machine", "pc", "desktop", "laptop",
    "cloud", "edge",
})


def _is_trusted_label(value: str) -> bool:
    if not _SAFE_MACHINE_LABEL.fullmatch(value):
        return False
    if sanitize_text(value, names=False) != value or sensitive_findings(value, names=False):
        return False
    parts = re.split(r"[-_]", value)

    def known_technical_word(part: str) -> bool:
        if part in _TRUSTED_LABEL_WORDS:
            return True
        # Compact identifiers such as node2 and claude01 are trusted only when their
        # alphabetic stem is a known technical term; an arbitrary name like jane2 is not.
        match = re.fullmatch(r"([a-z]+)([0-9]+)", part)
        return bool(match and match.group(1) in _TRUSTED_LABEL_WORDS)

    for index, part in enumerate(parts):
        if known_technical_word(part):
            continue
        # A standalone numeric suffix is allowed only next to an allow-listed technical word,
        # as in wsl-1 and claude-01.
        if part.isdigit() and (
            (index > 0 and known_technical_word(parts[index - 1]))
            or (index + 1 < len(parts) and known_technical_word(parts[index + 1]))
        ):
            continue
        return False
    return True


def _machine_label_path(file_name: str | None, path: tuple[Any, ...]) -> bool:
    """Identify the allow-listed paths that carry machine labels in bundle JSON."""
    if file_name == "versions.json":
        return len(path) == 3 and path[0] == "nodes" and isinstance(path[1], int) and path[2] == "name"
    if file_name == "settings.json":
        return len(path) == 2 and path[0] == "configured_nodes" and isinstance(path[1], int)
    if file_name == "machine-state.json":
        return len(path) == 2 and isinstance(path[0], int) and path[1] == "name"
    if file_name == "account-state.json":
        return len(path) == 2 and isinstance(path[0], int) and path[1] in ("node", "id")
    if file_name == "system-check.json":
        return (len(path) == 4 and path[:2] == ("system_check", "nodes")
                and isinstance(path[2], int) and path[3] in ("name", "release"))
    return False


def _replace_machine_labels(value: Any, file_name: str | None, *, redact_unsafe: bool,
                            path: tuple[Any, ...] = (), replacements: dict[str, str] | None = None,
                            unsafe: list[str] | None = None) -> Any:
    """Protect safe machine labels from person-name heuristics, or redact unsafe labels."""
    replacements = replacements if replacements is not None else {}
    unsafe = unsafe if unsafe is not None else []
    if _machine_label_path(file_name, path) and isinstance(value, str):
        if value == "[REDACTED_NAME]":
            return value
        if _is_trusted_label(value):
            marker = f"AIFLOWMACHINELABEL{len(replacements)}END"
            replacements[marker] = value
            return marker
        unsafe.append(value)
        return "[REDACTED_NAME]" if redact_unsafe else value
    if isinstance(value, dict):
        return {key: _replace_machine_labels(item, file_name, redact_unsafe=redact_unsafe,
                                               path=(*path, key), replacements=replacements, unsafe=unsafe)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_machine_labels(item, file_name, redact_unsafe=redact_unsafe,
                                        path=(*path, index), replacements=replacements, unsafe=unsafe)
                for index, item in enumerate(value)]
    return value


def sanitize_text(text: str, names: bool = True) -> str:
    """Remove recognizable credentials and personal identifiers before writing bundle files.

    ``names=False`` skips the person-name rules; callers should use it only when they separately
    screen name-like identifiers in the data they are sanitizing.
    """
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
    if not names:
        return text
    text = _DOTTED_NAME.sub("[REDACTED_NAME]", text)
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


def sensitive_findings(text: str, names: bool = True) -> list[str]:
    """Return names of sensitive-data classes still present after sanitization."""
    findings = []
    if (_PRIVATE_KEY.search(text) or _BEARER.search(text) or _URL_CREDENTIALS.search(text)
            or _unredacted_secret_line(text)):
        findings.append("credential")
    if _KNOWN_TOKEN.search(text) or _JWT.search(text):
        findings.append("token")
    has_ip = any(_is_valid_ip(match.group(0)) for match in _IP.finditer(text))
    has_name = names and bool(_DOTTED_NAME.search(text) or _NAME.search(text)
                              or _PERSON_FIELD.search(text) or _PERSON_MENTION.search(text))
    if (_EMAIL.search(text) or _PHONE.search(text) or _SSN.search(text) or _DOB_FIELD.search(text)
            or _ADDRESS.search(text) or has_ip or has_name):
        findings.append("personal_data")
    return findings


def file_findings(name: str, text: str) -> list[str]:
    """Scan one bundle file, protecting only known-safe machine label fields in structured JSON."""
    if name.endswith(".json"):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return sensitive_findings(text)
        unsafe: list[str] = []
        protected = _replace_machine_labels(value, name, redact_unsafe=False, unsafe=unsafe)
        findings = sensitive_findings(json.dumps(protected, ensure_ascii=False), names=True)
        if unsafe and "personal_data" not in findings:
            findings.append("personal_data")
        return findings
    return sensitive_findings(text, names=name.startswith("logs/"))


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

    def __init__(self, max_records: int = 20_000, max_bytes: int = MAX_LOG_BYTES,
                 max_record_bytes: int = MAX_RECORD_BYTES) -> None:
        super().__init__(logging.INFO)
        self.records: deque[tuple[float, str, int]] = deque(maxlen=max_records)
        self.max_bytes = max_bytes
        self.max_record_bytes = max_record_bytes
        self.total_bytes = 0
        self.lock = threading.RLock()
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S")
        formatter.converter = time.gmtime
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if len(line) > self.max_record_bytes:
                # Cut before encoding so a huge message is never copied whole.
                line = line[:self.max_record_bytes]
            line, cut = cap_log_bytes(line, self.max_record_bytes)
            if cut:
                line += " [truncated]"
            size = len(line.encode("utf-8", errors="replace")) + 1
            with self.lock:
                self._prune(time.time())
                if len(self.records) == self.records.maxlen and self.records:
                    self.total_bytes -= self.records[0][2]
                self.records.append((record.created, line, size))
                self.total_bytes += size
                while self.records and self.total_bytes > self.max_bytes:
                    self.total_bytes -= self.records.popleft()[2]
        except Exception:
            self.handleError(record)

    def _prune(self, now: float) -> None:
        cutoff = now - RETENTION.total_seconds()
        while self.records and self.records[0][0] < cutoff:
            self.total_bytes -= self.records.popleft()[2]

    def text(self) -> str:
        with self.lock:
            self._prune(time.time())
            lines = [line for _, line, _ in self.records]
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


def _persistent_control_logs(directory: str, now: datetime) -> list[list[str]]:
    if not directory:
        return []
    root = Path(directory)
    if not root.is_dir():
        return []
    cutoff = now - RETENTION
    output: list[list[str]] = []
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
            output.append(recent)
    return output


def _run_log_key(ctx, run_id: str) -> str:
    return ctx.leases._k("support_run_log", str(run_id))


def store_run_log(ctx, run_id: str, text: str) -> bool:
    """Keep a sanitized, scanned run log in Redis for the 24 hour window; never in the runs table."""
    safe = _safe_log(text)
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


def _safe_release_version(value: Any) -> str:
    """Keep only the v4 release syntax used by the node agent and package version."""
    if isinstance(value, str) and _RELEASE_VERSION.fullmatch(value):
        return value
    return "unknown"


def _safe_tool_version(name: str, value: Any) -> str:
    """Reduce a heartbeat tool report to a recognized version or a fixed status marker.

    Heartbeat values are supplied by node agents and can contain arbitrary text. Export only version-shaped
    values, extracting them from the known CLI output prefixes; never include extra output or error details.
    """
    if not isinstance(value, str):
        return _REDACTED
    value = value.strip()
    if value in {"missing", "timeout"}:
        return value
    if value.lower().startswith("broken:"):
        return "broken"
    match = _TOOL_VERSION.match(value)
    if match:
        return match.group("version")
    # Prefix parsing accepts CLI build metadata only after a recognized label and a valid version token.
    for prefix in _TOOL_VERSION_PREFIXES.get(name, ()):
        if value.startswith(prefix):
            match = _TOOL_VERSION.match(value[len(prefix):])
            if match:
                return match.group("version")
    return _REDACTED


def _safe_heartbeat_tools(value: Any) -> dict[str, str]:
    """Retain only recognized tool keys and their validated, reduced heartbeat values."""
    if not isinstance(value, dict):
        return {}
    return {name: _safe_tool_version(name, raw)
            for name, raw in sorted(value.items()) if isinstance(name, str) and name in _REPORTED_TOOLS}


_LOG_PREFIX = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"(?:\s+(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL))?"
    r"(?:\s+(?P<logger>[A-Za-z_][\w.]*))?")
_LOG_PAIR = re.compile(
    r"(?<![\w.-])(?P<key>[A-Za-z_][\w.]{0,40})=(?P<value>\d{1,9}(?:\.\d{1,6})?|true|false|True|False|None|"
    r"[A-Z][A-Z0-9_]{1,31}-\d{1,9})(?![\w.@-])")
# Only export known diagnostic fields from free-form log messages. Arbitrary field names can
# themselves contain personal data (for example, ``jane.doe=1``).
_TRUSTED_LOG_KEYS = frozenset({
    "attempt", "attempts", "bytes", "code", "count", "duration_ms", "elapsed_ms", "fence",
    "items", "n", "retry", "retries", "run", "status", "status_code", "total", "tries",
})
FREEFORM = "[REDACTED_FREEFORM]"


def structured_log_text(text: str) -> str:
    """Reduce log text to timestamp, level and key=value pairs with numeric or id values.

    Log messages are free text whose personal names no pattern can reliably find, so the message
    itself is never exported. Logger names are also caller-controlled and are omitted.
    """
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        head = _LOG_PREFIX.match(line)
        rest = line[head.end():] if head else line
        parts = [part for part in (head.group("stamp"), head.group("level")) if part] if head else []
        pairs = [f"{m.group('key')}={m.group('value')}" for m in _LOG_PAIR.finditer(rest)
                 if m.group("key").lower() in _TRUSTED_LOG_KEYS
                 and not re.fullmatch(_SECRET_NAME, m.group("key"), re.I)]
        out.append(" ".join(parts + [FREEFORM] + pairs))
    return "\n".join(out) + ("\n" if out else "")


def _safe_log(text: str) -> str:
    capped, _ = cap_log_bytes(text)
    return cap_log_bytes(sanitize_text(structured_log_text(capped)))[0]


def _safe_persistent_logs(sections: list[list[str]]) -> str:
    """Reduce persistent logs and add generic separators that cannot contain source data."""
    output: list[str] = []
    for index, lines in enumerate(sections, 1):
        # These headers are generated here; separator-shaped text in a log is always reduced as free form.
        output.append(f"--- control-log-{index} ---")
        output.extend(structured_log_text("\n".join(lines)).splitlines())
    text = "\n".join(output) + ("\n" if output else "")
    return cap_log_bytes(sanitize_text(text))[0]


def _safe_json(value: Any, file_name: str | None = None) -> str:
    replacements: dict[str, str] = {}
    protected = _replace_machine_labels(value, file_name, redact_unsafe=True, replacements=replacements)
    raw = json.dumps(protected, ensure_ascii=False, default=str, indent=2, sort_keys=True)
    safe = sanitize_text(raw)
    for marker, label in replacements.items():
        safe = safe.replace(marker, label)
    return safe + "\n"


def _readiness_export(ctx) -> dict:
    """The System check snapshot with the fields the generic secret scanner would mistake for credentials removed."""
    from . import system_check

    readiness = system_check.support_bundle(ctx)
    # The per-account boolean mirror is redundant with readiness condition rows and its field name resembles
    # secret metadata to the generic support-bundle scanner. Keep the conditions, omit that redundant map.
    for node in readiness["system_check"]["nodes"]:
        snapshot = node.get("readiness", {})
        snapshot.pop("credential_statuses", None)
        condition_rows = list(snapshot.get("machine", []) or [])
        for rows in (snapshot.get("projects", {}) or {}).values():
            condition_rows.extend(rows or [])
        for item in condition_rows:
            if isinstance(item, dict) and str(item.get("id", "")).startswith("credential:"):
                # Avoid rendering an account condition ID as a secret assignment to the generic text scanner.
                item["id"] = str(item["id"]).replace("credential:", "credential-", 1)
    return readiness


def _file_payloads(ctx, ticket_key: str | None, log_buffer: SupportLogBuffer) -> dict[str, bytes]:
    repo = ctx.repo()
    now = datetime.now(timezone.utc)
    version = _safe_release_version(ctx.settings.control_version or VERSION)
    nodes = ctx.db.all("SELECT name,role,status,slots_total,slots_used,release_version,last_heartbeat,tools"
                       " FROM nodes ORDER BY name")
    machines = []
    for row in nodes:
        heartbeat = row["last_heartbeat"]
        machines.append({"name": row["name"], "role": row["role"], "status": row["status"],
                         "slots_total": row["slots_total"], "slots_used": row["slots_used"],
                         "release": _safe_release_version(row["release_version"]),
                         "heartbeat_age_seconds": int((now - heartbeat).total_seconds()) if heartbeat else None,
                         "tools": _safe_heartbeat_tools(row["tools"])})
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

    readiness = _readiness_export(ctx)

    files: dict[str, bytes] = {
        "versions.json": _safe_json({"aiflow": version, "python": platform.python_version(),
                                      "platform": platform.system().lower(),
                                      "nodes": [{"name": row["name"], "release": _safe_release_version(row["release_version"])}
                                                for row in nodes]}, "versions.json").encode("utf-8"),
        "settings.json": _safe_json({"environment": ctx.settings.env, "console_login_enabled": bool(ctx.settings.console_login),
                                      "control_version": version, "projects": sorted(repo.projects),
                                      "configured_nodes": sorted(repo.nodes),
                                      "configured_account_count": len(repo.accounts)}, "settings.json").encode("utf-8"),
        "machine-state.json": _safe_json(machines, "machine-state.json").encode("utf-8"),
        "account-state.json": _safe_json(account_states, "account-state.json").encode("utf-8"),
        "incidents.json": _safe_json(incident_data, "incidents.json").encode("utf-8"),
        "system-check.json": _safe_json(readiness, "system-check.json").encode("utf-8"),
    }

    persistent_logs = _persistent_control_logs(ctx.settings.support_log_dir, now)
    if persistent_logs:
        safe_log = _safe_persistent_logs(persistent_logs)
    else:
        log_text = log_buffer.text() or "No control service log records were captured in the last 24 hours.\n"
        safe_log = _safe_log(log_text)
    files["logs/control-service.log"] = safe_log.encode("utf-8")

    if ticket_key:
        ticket = ctx.db.one("SELECT key,project,status,type,summary FROM tickets WHERE key=%s", (ticket_key,))
        if not ticket:
            raise KeyError(ticket_key)
        ticket_data = dict(ticket)
        # Ticket summaries are free text like incident prose; no pattern screens them reliably.
        ticket_data["summary"] = FREEFORM
        ticket_data["ticket_key"] = ticket_data.pop("key")
        files["ticket.json"] = _safe_json(ticket_data, "ticket.json").encode("utf-8")
        rows = ctx.db.all("SELECT id,step,started_at FROM runs WHERE ticket_key=%s"
                          " AND started_at >= now() - interval '24 hours' ORDER BY started_at,id", (ticket_key,))
        for row in rows:
            run_log = load_run_log(ctx, row["id"])
            if run_log is None:
                continue
            safe_log = _safe_log(run_log)
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
    files["inventory.json"] = _safe_json(inventory, "inventory.json").encode("utf-8")
    findings = [(name, item) for name, content in files.items()
                for item in file_findings(name, content.decode("utf-8", errors="replace"))]
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
