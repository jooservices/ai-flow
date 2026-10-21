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


def decide_read_only(policy: dict[str, Any], tool: str, tool_input: dict[str, Any], worktree: str) -> Decision:
    """Triage may inspect source, but cannot run commands that write to the checkout or elsewhere."""
    if tool in WRITE_TOOLS:
        return Decision("deny", "a triage run reads the base branch and cannot edit files")
    if tool == "Bash":
        command = str(tool_input.get("command", "")).strip()
        if any(ch in command for ch in (">", "<", "`", "$")):
            return Decision("deny", "shell redirection and command substitution are not allowed during triage")
        allowed = {command for command in READ_ONLY if command.split()[0] != "env" and command.split()[0] != "date"}
        for segment in _segments(command):
            try:
                args = shlex.split(segment)
            except ValueError:
                return Decision("deny", "invalid shell quoting during triage")
            if not args:
                continue
            prefix = " ".join(args[:2]) if args[0] == "git" and len(args) > 1 else args[0]
            if prefix not in allowed:
                return Decision("deny", f"command is not read-only during triage: {segment[:120]}")
            if args[0] == "find" and any(a in ("-exec", "-execdir", "-delete", "-ok", "-okdir")
                                           or a.startswith(("-fprint", "-fprintf"))
                                           for a in args):
                return Decision("deny", "find commands that execute or write are not allowed during triage")
            if args[0] in ("sort", "tree") and any(a == "-o" or a.startswith("-o") or a == "--output"
                                                       or a.startswith("--output=") for a in args):
                return Decision("deny", f"{args[0]} output files are not allowed during triage")
            if args[:2] == ["git", "branch"] and any(
                    not a.startswith("-") or a in ("-d", "-D", "-m", "-M", "-c", "-C", "-f",
                                                    "--delete", "--move", "--copy", "--force",
                                                    "--edit-description") for a in args[2:]):
                return Decision("deny", "git branch commands that change refs are not allowed during triage")
            if args[0] == "git" and args[1:2] in (["diff"], ["show"], ["log"]) and any(
                    a in ("--ext-diff", "--output", "-o") or a.startswith("--output=") for a in args[2:]):
                return Decision("deny", "Git commands that write files or run external diff tools are not allowed")
            for arg in args[1:]:
                if any(fnmatch.fnmatch(arg, pattern) for pattern in (policy.get("files") or {}).get("deny_read", [])):
                    return Decision("deny", f"reading {arg} is not allowed")
        return Decision("allow", "read-only command during triage")
    return decide(policy, tool, tool_input, worktree)
