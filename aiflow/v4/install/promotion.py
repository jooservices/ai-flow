"""Promotion failure tracking for auto-deploy (docs/v4/upgrade-layout.md).

After a canary reports stable, the control-plane promotion can still fail (for example a missing host
file). The auto-deploy scripts record each failure's build, step and cause here so a CRITICAL alert can
name them, and so the same build is not retried forever when the cause does not change.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FAILURES_NAME = "PROMOTION_FAILURES.json"
MAX_SAME_CAUSE = 2
_MISSING_FILE = re.compile(
    r"(?:env file |open |stat )?(?P<path>/etc/aiflow/\S+?)(?:\s+not found|\s*:? not found|:\s*no such file)",
    re.IGNORECASE,
)


def normalize_cause(error: str) -> str:
    """Collapse a deploy error to a stable cause key; missing-file paths are the common upgrade fault."""
    text = " ".join((error or "").split())
    if not text:
        return "unknown deploy failure"
    match = _MISSING_FILE.search(text)
    if match:
        return f"missing file {match.group('path').rstrip('\"\'')}"
    return text[:240]


def load_failures(path: Path) -> dict:
    """Return the recorded promotion failure state, or an empty record when absent or invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_failure(path: Path, *, build: str, step: str, error: str) -> dict:
    """Record one failed promotion. Returns the updated state including whether the build is exhausted."""
    cause = normalize_cause(error)
    previous = load_failures(path)
    count = 1
    if previous.get("build") == build and previous.get("cause") == cause:
        count = int(previous.get("count") or 0) + 1
    state = {
        "build": build,
        "step": step or "deploy",
        "cause": cause,
        "error": " ".join((error or "").split())[:500],
        "count": count,
        "exhausted": count >= MAX_SAME_CAUSE,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state) + "\n", encoding="utf-8")
    tmp.replace(path)
    return state


def clear_failures(path: Path) -> None:
    """Drop promotion-failure state after a successful deploy or when a rejected build is superseded."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def alert_detail(*, build: str, step: str, error: str, not_live: int | str, previous: str,
                 count: int, exhausted: bool) -> str:
    """Body of the CRITICAL promotion-failure alert (step, error, stuck build, backlog)."""
    cause = normalize_cause(error)
    parts = [
        f"Failed step: {step or 'deploy'}.",
        f"Error: {cause}.",
        f"Stuck build: {build} (previous {previous or 'none'}).",
        f"Merged changes not live: {not_live}.",
    ]
    if exhausted:
        parts.append(
            f"Stopped retrying {build} after {MAX_SAME_CAUSE} failures with the same cause; "
            "the next build is tried only when the cause has changed.")
    else:
        parts.append(f"Failure {count} of {MAX_SAME_CAUSE} with this cause.")
    return " ".join(parts)


def should_try_build(*, rejected: str, target: str, failures: dict) -> tuple[bool, str]:
    """Whether auto-deploy may attempt `target`.

    The same rejected build stays blocked. After two failures with one cause, a newer build is tried only
    when that cause has changed (a missing-file cause clears once the file exists).
    """
    if target and target == rejected:
        return False, f"release {target} already failed its canary or promotion; waiting for a newer release"
    if not failures or not failures.get("exhausted"):
        return True, ""
    if failures.get("build") == target:
        return False, f"release {target} already failed promotion with the same cause; waiting for a change"
    cause = str(failures.get("cause") or "")
    if cause.startswith("missing file "):
        missing = Path(cause[len("missing file "):].strip())
        if not missing.is_file():
            return False, (f"waiting for cause to change before trying {target or 'the next build'}: {cause}")
    return True, ""
