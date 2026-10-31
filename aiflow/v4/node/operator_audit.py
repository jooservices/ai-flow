"""Parse and forward operator audit events with credential redaction."""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..worker.client import ControlClient

_AUDIT_EVENT_ID = re.compile(rb"msg=audit\((\d+(?:\.\d+)?):(\d+)\)")

_AUDIT_TYPE = re.compile(rb"type=([A-Z_]+)")

_AUDIT_AUID = re.compile(rb"\bauid=(\d+)\b")

_AUDIT_EXE = re.compile(rb'\bexe="((?:\\.|[^"\\])*)"')

_AUDIT_PATH = re.compile(rb'\bname="((?:\\.|[^"\\])*)"')

_AUDIT_ARG = re.compile(rb'\ba(\d+)="((?:\\.|[^"\\])*)"')

_AUDIT_ARGC = re.compile(rb'\bargc=(\d+)\b')

_SECRET_OPTION = re.compile(
    r"(?i)(?:token|password|passwd|passphrase|secret|api[-_]?key|client[-_]?secret|private[-_]?key|"
    r"credential|authorization|cookie|header|(?:^|[-_])key(?:$|[-_]))"
)

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)^([A-Za-z0-9_.-]*(?:token|password|passwd|passphrase|secret|api[-_]?key|client[-_]?secret|"
    r"private[-_]?key|credential|authorization|cookie|key)[A-Za-z0-9_.-]*\s*=).*$"
)

_SECRET_VALUE = re.compile(
    r"(?i)(?:\b(?:bearer|basic)\s+\S+|\b(?:gh[pousr]_|github_pat_|glpat-|xox[baprs]-|sk-|AKIA)[A-Za-z0-9_-]+|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)

def _audit_actor(text: bytes) -> str | None:
    """The login name behind an audit event, or None for an unset loginuid (systemd services are not operators)."""
    auid = _AUDIT_AUID.search(text)
    if not auid:
        return None
    uid = int(auid.group(1))
    if uid >= 4_294_967_295:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)

def _audit_interventions(raw: bytes) -> list[dict]:
    """Extract human commands and changed files, retaining only secret-redacted arguments and safe names."""
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
        actor = _audit_actor(text)
        if actor is None:
            continue
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
            args, argument_count = _safe_audit_arguments(lines)
            results.append({"source_key": f"{timestamp}:{serial}:shell", "source": "shell", "actor": actor,
                            "action": "execute", "target": command, "happened_at": happened_at,
                            "reason": f"Interactive host session invoked {command}; auditd captured secret-redacted arguments.",
                            "details": {"arguments": args, "argument_count": argument_count}})
        if b"PATH" in types and b"SYSCALL" in types:
            paths = [m.group(1).decode("utf-8", "replace") for line in lines if _AUDIT_TYPE.search(line)
                     and _AUDIT_TYPE.search(line).group(1) == b"PATH" for m in _AUDIT_PATH.finditer(line)]
            for path in sorted(set(path for path in paths if path)):
                filename = os.path.basename(path)
                path_id = hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16]
                results.append({"source_key": f"{timestamp}:{serial}:config:{path_id}", "source": "configuration",
                                "actor": actor, "action": "write", "target": filename[:120],
                                "happened_at": happened_at,
                                "reason": f"Interactive host session changed {filename}; auditd captured the file name."})
    return results

def _safe_audit_arguments(lines: list[bytes]) -> tuple[list[str], int]:
    """Return argv excluding argv[0], redacting credential options and values before persistence."""
    values: dict[int, str] = {}
    argc = 0
    for line in lines:
        count = _AUDIT_ARGC.search(line)
        if count:
            argc = max(argc, int(count.group(1)))
        for match in _AUDIT_ARG.finditer(line):
            values[int(match.group(1))] = match.group(2).decode("utf-8", "replace")
    argument_count = max(argc - 1, max(values, default=0))
    safe: list[str] = []
    redact_next = False
    for index in range(1, argument_count + 1):
        value = values.get(index, "[unavailable]")
        if redact_next:
            safe.append("[REDACTED]")
            redact_next = False
            continue
        option = value.split("=", 1)[0]
        short_inline_secret = re.match(r"(?i)^-(?:p|h).+", value)
        if short_inline_secret:
            safe.append(value[:2] + "[REDACTED]")
            continue
        if value.startswith("-") and (_SECRET_OPTION.search(option) or option in {"-p", "-P", "-H"}):
            if "=" in value:
                safe.append(f"{option[:2000]}=[REDACTED]")
            else:
                safe.append(value[:2000])
                redact_next = True
            continue
        safe.append(_redact_audit_argument(value))
    return safe, argument_count

def _redact_audit_argument(value: str) -> str:
    """Keep useful command context while stripping credentials, URL userinfo and URL query strings."""
    assignment = _SECRET_ASSIGNMENT.match(value)
    if assignment:
        return assignment.group(1) + "[REDACTED]"
    if _SECRET_VALUE.search(value):
        return "[REDACTED]"

    def redact_url(match: re.Match[str]) -> str:
        from urllib.parse import urlsplit, urlunsplit

        raw_url = match.group(0).rstrip(",;).")
        suffix = match.group(0)[len(raw_url):]
        try:
            parsed = urlsplit(raw_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            query = "[REDACTED]" if parsed.query else ""
            safe_url = urlunsplit((parsed.scheme, host, parsed.path, query, ""))
            return safe_url + suffix
        except ValueError:
            return "[REDACTED_URL]" + suffix

    return re.sub(r"https?://[^\s\"'<>]+", redact_url, value, flags=re.IGNORECASE)[:2000]

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
    if offset > stat.st_size:  # copy-truncate rotation can keep the inode while shortening the log
        offset = 0
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read()
    complete_length = data.rfind(b"\n") + 1
    complete = data[:complete_length]
    events = _audit_interventions(complete)
    for event in events:
        api.call("POST", f"/v1/nodes/{quote(node, safe='')}/operator-interventions", event, retries=1)
    tmp = cursor.with_suffix(".tmp")
    tmp.write_text(json.dumps({"inode": stat.st_ino, "offset": offset + complete_length}), encoding="utf-8")
    os.replace(tmp, cursor)
    return len(events)
