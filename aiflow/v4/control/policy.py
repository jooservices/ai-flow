"""Policy decisions for AI tool calls (the PreToolUse hook of Claude, the run wrapper of other CLIs).

Rules come from the control repo (policy/default.yaml, overridable per project). Deny wins over allow; a
command that matches no rule is denied with a reason, so a gap in the policy shows up as a denial that the
question router can pick up, never as a silent approval.
"""
from __future__ import annotations

import fnmatch
import os
import shlex
from dataclasses import dataclass
from typing import Any

READ_ONLY = ("ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "stat", "file", "tree",
             "git status", "git diff", "git log", "git show", "git branch", "git rev-parse", "which", "env", "date",
             "sort", "uniq", "cut", "jq", "diff", "true")
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Read-only runs (intake) may run only these commands, whatever the project's allowlist says; arguments that
# make one of them write or run another program are refused.
INSPECT_ONLY = ("ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "stat", "file", "tree", "cut",
                "jq", "diff", "true", "date", "which", "sort", "git status", "git diff", "git log", "git show",
                "git rev-parse")
WRITING_ARGS = {"find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
                "sort": ("-o", "--output"), "git diff": ("--output",), "git log": ("--output",),
                "git show": ("--output",), "rg": ("--pre",), "tree": ("-o",)}
SHELL_TRICKS = (">", "`", "$(", "<(")


@dataclass
class Decision:
    decision: str  # allow | deny
    reason: str

    def hook_output(self) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": self.decision,
                                       "permissionDecisionReason": self.reason}}


def _segments(command: str) -> list[str]:
    """Split a shell line on && || ; | so every part is checked (a deny anywhere denies the whole line)."""
    parts, cur, i, quote = [], "", 0, ""
    while i < len(command):
        ch = command[i]
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur += ch
        elif command.startswith(("&&", "||"), i):
            parts.append(cur); cur = ""; i += 1
        elif ch in ";|\n":
            parts.append(cur); cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _within(path: str, root: str) -> bool:
    if not path:
        return False
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    base = os.path.realpath(root)
    return full == base or full.startswith(base + os.sep)


def decide(policy: dict[str, Any], tool: str, tool_input: dict[str, Any], worktree: str) -> Decision:
    cmds = policy.get("commands") or {}
    allow = list(cmds.get("allow") or [])
    deny = list(cmds.get("deny") or [])
    files = policy.get("files") or {}
    deny_read = list(files.get("deny_read") or [])

    if tool == "Bash":
        command = str(tool_input.get("command", "")).strip()
        whole_denied = next((p for p in deny if fnmatch.fnmatch(command, p)), None)
        if whole_denied:
            return Decision("deny", f"policy denies '{whole_denied}'")
        for seg in _segments(command):
            hit = next((p for p in deny if fnmatch.fnmatch(seg, p)), None)
            if hit:
                return Decision("deny", f"policy denies '{hit}' (in: {seg[:80]})")
            if any(seg == ro or seg.startswith(ro + " ") for ro in READ_ONLY):
                for arg in shlex.split(seg, posix=True)[1:] if "'" not in seg and '"' not in seg else []:
                    if any(fnmatch.fnmatch(arg, g) for g in deny_read):
                        return Decision("deny", f"reading {arg} is not allowed")
                continue
            if any(fnmatch.fnmatch(seg, p) for p in allow):
                continue
            return Decision("deny", f"not in the project's command policy: {seg[:120]}")
        return Decision("allow", "matches the command policy")

    if tool in WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if files.get("write_within", "worktree") == "worktree" and not _within(path, worktree):
            return Decision("deny", f"writes are allowed only inside the worktree ({path})")
        return Decision("allow", "write inside the worktree")

    if tool in ("Read", "Glob", "Grep"):
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if path and any(fnmatch.fnmatch(path, g) for g in deny_read):
            return Decision("deny", f"reading {path} is not allowed")
        return Decision("allow", "read")

    if tool in ("WebFetch", "WebSearch"):
        return Decision("allow", "documentation lookup")
    return Decision("deny", f"tool {tool} is not used by AI Flow runs")


def decide_read_only(policy: dict[str, Any], tool: str, tool_input: dict[str, Any], worktree: str,
                     why: str) -> Decision:
    """The policy of a run that only reads: no file tools that write, and only INSPECT_ONLY shell commands.

    The project's command allowlist is ignored (it holds installs and builds that change the worktree); its deny
    rules and deny_read globs still apply through decide().
    """
    if tool in WRITE_TOOLS:
        return Decision("deny", why)
    if tool == "Bash":
        command = str(tool_input.get("command", "")).strip()
        trick = next((t for t in SHELL_TRICKS if t in command), None)
        if trick:
            return Decision("deny", f"{why} (redirects and substitutions are not allowed: '{trick}')")
        for seg in _segments(command):
            ro = next((r for r in INSPECT_ONLY if seg == r or seg.startswith(r + " ")), None)
            if not ro:
                return Decision("deny", f"{why} (not a read-only command: {seg[:120]})")
            bad = WRITING_ARGS.get(ro, ())
            if any(a == b or a.startswith(b + "=") or (b.startswith("-") and len(b) == 2 and a.startswith(b))
                   for a in seg.split()[1:] for b in bad):
                return Decision("deny", f"{why} (this use of {ro} writes or runs a program: {seg[:120]})")
        policy = {**policy, "commands": {**(policy.get("commands") or {}), "allow": []}}
    return decide(policy, tool, tool_input, worktree)
