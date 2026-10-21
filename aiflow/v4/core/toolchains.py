"""Project stacks and the tools a node needs for them.

A project's `stack` in `aiflow-control/projects/<KEY>.yaml` selects extra tools for every node that serves
that project. The node agent reports them in its heartbeat, the watchdog warns when one is missing, and the
Command Center drawer lists the missing ones.
"""
from __future__ import annotations

from collections.abc import Iterable

# Tools every node reports, whatever its projects use (reporting only; requirements are below).
NODE_TOOLS = ("claude", "codex", "codex-code-mode-host", "grok", "opencode", "docker", "git", "node", "uv", "gh")

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
}


def tools_for_providers(providers: Iterable[str]) -> list[str]:
    """CLIs the AI providers of a node's accounts need (Codex also needs its code-mode host)."""
    out: list[str] = []
    for provider in providers:
        for tool in PROVIDER_TOOLS.get(provider, ()):
            if tool not in out:
                out.append(tool)
    return out


def tools_for_stack(stack: str) -> list[str]:
    """The base tools plus the extra tools `stack` needs; an unknown or empty stack needs only the base tools."""
    out = list(BASE_TOOLS)
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
    return [tool for tool in required if str(reported.get(tool, "missing")) == "missing"]
