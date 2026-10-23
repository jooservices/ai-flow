"""Project stacks, locked tool versions and the tools a node needs for them.

A project's `stack` in `aiflow-control/projects/<KEY>.yaml` selects extra tools for every node that serves
that project. The node agent prepares and reports them in its heartbeat, the control plane gates project work
on a pinned-version preflight, and the watchdog warns when one is unhealthy.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Tools every node reports, whatever its projects use (reporting only; requirements are below).
NODE_TOOLS = ("claude", "codex", "codex-code-mode-host", "grok", "opencode", "docker", "git", "node", "uv", "gh")

# Shared folder for Playwright's browsers: installed by root, readable by the AI user, handed to runs through
# PLAYWRIGHT_BROWSERS_PATH (see worker/notes.py project_env).
PLAYWRIGHT_BROWSERS_DIR = "/usr/local/lib/aiflow4/playwright-browsers"

# Required on every worker node, and per AI provider of the accounts the node runs.
BASE_TOOLS = ("git", "uv")
PROVIDER_TOOLS: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex", "codex-code-mode-host"),
    "grok": ("grok",),
    "opencode": ("opencode",),
}

# Extra tools per project stack. Add a stack here when a project starts using it.
STACK_TOOLS: dict[str, tuple[str, ...]] = {
    "php-8.3": ("php", "composer", "wp"),
    "python-3.12": ("python3", "uv"),
    "node-24": ("node",),
    "playwright": ("node", "playwright", "playwright-browser"),
}

# `versions.lock` names used by the control repo. Keep aliases here so installation and verification use the
# same pins as the console's version display.
VERSION_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("claude_code", "claude-code", "claude"),
    "codex": ("codex",),
    "grok": ("grok",),
    "opencode": ("opencode",),
    "docker": ("docker",),
    "php": ("php",),
    "composer": ("composer",),
    "wp": ("wp", "wp_cli"),
    "git": ("git",),
    "node": ("node",),
    "uv": ("uv",),
    "gh": ("gh",),
    "python3": ("python3", "python"),
    "playwright": ("playwright",),
    "playwright-browser": ("playwright_browser", "playwright-browser", "playwright"),
}

# A tool that is installed but cannot work on this machine (e.g. Chromium without a system library) is reported
# as `broken: <reason>`; it is a gap of the machine's toolchain, never a ticket failure.
BROKEN_PREFIX = "broken:"

_VERSION = re.compile(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+){0,2})(?![A-Za-z0-9])")


def normalize_stacks(stacks: str | Iterable[str] | None) -> list[str]:
    """Normalize the legacy single stack and the multi-stack project YAML form."""
    if isinstance(stacks, str):
        return [stacks] if stacks else []
    return [str(stack) for stack in (stacks or ()) if str(stack)]


def pinned_version(versions: dict[str, object] | None, tool: str,
                   stacks: str | Iterable[str] | None = ()) -> str:
    """Version pin for a tool, using versions.lock first and the project's explicit stack version second."""
    if tool == "codex-code-mode-host":
        # The host binary must come from the same Codex release to speak its protocol.
        return pinned_version(versions, "codex")
    value = locked_version(versions, tool)
    if value:
        return value
    for stack in normalize_stacks(stacks):
        prefix, sep, version = str(stack).partition("-")
        if sep and ((tool == "php" and prefix == "php") or (tool == "python3" and prefix == "python")):
            return version
    return ""


def locked_version(versions: dict[str, object] | None, tool: str) -> str:
    """Pin explicitly stored in versions.lock, including aliases that share a package release."""
    if tool == "codex-code-mode-host":
        return locked_version(versions, "codex")
    locked = (versions or {}).get("tools") or {}
    if isinstance(locked, dict):
        for key in VERSION_KEYS.get(tool, (tool,)):
            value = locked.get(key)
            if isinstance(value, dict):
                value = value.get("version")
            if value not in (None, ""):
                return str(value)
    if tool == "playwright-browser":
        return locked_version(versions, "playwright")
    return ""


def tools_for_providers(providers: Iterable[str]) -> list[str]:
    """CLIs the AI providers of a node's accounts need (Codex also needs its code-mode host)."""
    out: list[str] = []
    for provider in providers:
        for tool in PROVIDER_TOOLS.get(provider, ()):
            if tool not in out:
                out.append(tool)
    return out


def tools_for_stack(stack: str) -> list[str]:
    """The base tools plus the extra tools for one stack; unknown stacks need only the base tools."""
    out = list(BASE_TOOLS)
    return tools_for_stacks_in_project([stack], out)


def tools_for_stacks_in_project(stacks: str | Iterable[str] | None,
                                initial: Iterable[str] = BASE_TOOLS) -> list[str]:
    """The base tools and stack tools for a project's one or more declared stacks."""
    out = list(initial)
    for stack in normalize_stacks(stacks):
        for tool in STACK_TOOLS.get(stack, ()):
            if tool not in out:
                out.append(tool)
    return out


def tools_for_stacks(stacks: Iterable[str]) -> list[str]:
    """The union of the reported node tools and the tools of every stack, in stable order (what a node reports)."""
    out = list(NODE_TOOLS)
    for stack in stacks:
        for tool in STACK_TOOLS.get(stack, ()):
            if tool not in out:
                out.append(tool)
    return out


def missing_tools(tools: dict[str, object] | None, required: Iterable[str]) -> list[str]:
    """Required tools whose reported value is absent or exactly `missing`."""
    reported = tools or {}
    return [tool for tool in required if str(reported.get(tool, "missing")) == "missing"
            or str(reported.get(tool)).startswith(BROKEN_PREFIX)]


def version_matches(actual: object, expected: str) -> bool:
    """Match a reported version against a pinned full version or major/minor release line.

    Lock pins may use `8.3`, `2.x`, or `2.8`; each checks the corresponding numeric prefix. Three
    numeric components check an exact release. A missing pin means presence is checked separately.
    """
    value = str(actual or "")
    if value in {"", "missing", "timeout"} or value.startswith(BROKEN_PREFIX):
        return False
    if not expected:
        return True
    pin = str(expected).strip().removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.(?:\d+|x)){0,2}", pin, re.IGNORECASE):
        return False
    wanted = pin.lower().split(".")
    wanted = wanted[:next((i for i, part in enumerate(wanted) if part == "x"), len(wanted))]
    if not wanted:
        return False
    found = _VERSION.search(value)
    if not found:
        return False
    got = found.group(1).removeprefix("v").split(".")
    return len(got) >= len(wanted) and got[:len(wanted)] == wanted


def toolchain_gaps(tools: dict[str, object] | None, required: Iterable[str],
                   versions: dict[str, object] | None = None,
                   stacks: str | Iterable[str] | None = (), *, require_pins: bool = False) -> list[str]:
    """Required tools absent, outside their lock pin, or unpinned for a declared stack."""
    reported = tools or {}
    stack_tools = {tool for stack in normalize_stacks(stacks) for tool in STACK_TOOLS.get(stack, ())}
    gaps = []
    for tool in required:
        if require_pins and tool in stack_tools and not locked_version(versions, tool):
            gaps.append(tool)
            continue
        actual = reported.get(tool, "missing")
        if str(actual) in {"", "missing", "timeout"} or str(actual).startswith(BROKEN_PREFIX):
            gaps.append(tool)
            continue
        expected = pinned_version(versions, tool, stacks)
        if expected and not version_matches(actual, expected):
            gaps.append(tool)
    return gaps


# --- missing requirements reported by a developer AI (docs/v4/missing-tools.md) ----------------------------------
MISSING_MARKER = re.compile(
    r"MISSING[-_ ](?:TOOL|SERVICE)S?\s*[:=]?\s*([A-Za-z0-9][\w.+-]*(?:\s*(?:,|\band\b)\s*[A-Za-z0-9][\w.+-]*)*)", re.I)
_ABSENT = r"(?:not installed|not found|command not found|is missing|missing|unavailable|not available|cannot be found)"


def known_requirements() -> list[str]:
    """Tool and service names AI Flow can tell apart in free text (what a node reports, plus test services)."""
    from .services import SERVICES
    names = list(NODE_TOOLS) + [t for tools in STACK_TOOLS.values() for t in tools] + list(SERVICES)
    return list(dict.fromkeys(names))


def parse_missing_requirements(text: str) -> list[str]:
    """Tools or services a developer AI says it lacks: the `MISSING-TOOL: a, b` marker first, then known names
    that the text calls missing ("php: command not found", "composer is not installed")."""
    marker = MISSING_MARKER.search(text or "")
    if marker:
        names = [n.lower() for n in re.split(r"\s*(?:,|\band\b)\s*", marker.group(1).strip(), flags=re.I) if n]
        return list(dict.fromkeys(names))
    found = []
    for name in known_requirements():
        quoted = re.escape(name)
        if re.search(rf"(?<![\w-]){quoted}(?![\w-])[^.\n]{{0,40}}{_ABSENT}|missing\s+{quoted}(?![\w-])",
                     text or "", re.I):
            found.append(name)
    return found


def tool_present(tools: dict[str, object] | None, name: str) -> bool:
    """True when a node reported the tool with a real version (not absent, `missing` or `timeout`)."""
    return str((tools or {}).get(name, "missing")) not in {"", "missing", "timeout"}
