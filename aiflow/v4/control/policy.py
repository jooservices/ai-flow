"""Policy decisions for AI tool calls (the PreToolUse hook of Claude, the run wrapper of other CLIs).

Rules come from the control repo (policy/default.yaml, overridable per project). Deny wins over allow; a
command that matches no rule is denied with a reason, so a gap in the policy shows up as a denial that the
question router can pick up, never as a silent approval.
"""
from __future__ import annotations

import fnmatch
import glob
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
                "jq", "diff", "true", "date", "which", "git status", "git diff", "git log", "git show",
                "git rev-parse")
WRITING_ARGS = {"find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"),
                "sort": ("-o", "--output"), "git diff": ("--output",), "git log": ("--output",),
                "git show": ("--output",), "rg": ("--pre",), "tree": ("-o", "--output")}
UNSAFE_ARGS = {
    "find": ("-files0-from", "-L", "-follow"),
    "rg": ("--files-from", "--follow", "-L"),
    "grep": ("-R",), "ls": ("-L",), "tree": ("-l",),
    "wc": ("--files0-from",), "file": ("-f",),
    "git status": ("--pathspec-from-file",),
    "git diff": ("--pathspec-from-file", "--ext-diff", "--textconv"),
    "git log": ("--pathspec-from-file", "--ext-diff"),
    "git show": ("--textconv",),
}
SHELL_PATH_OPTIONS = {
    "grep": ("-f", "--file", "--exclude-from"),
    "rg": ("-f", "--file", "--ignore-file"),
    "file": ("-f",),
    "find": ("-newer", "-anewer", "-cnewer", "-samefile"),
    "date": ("-f", "--file"),
    "jq": ("-f", "--from-file", "-L", "--library-path", "--argfile", "--rawfile", "--slurpfile"),
}


def _segments(command: str) -> list[str]:
    """Split a shell line on && || ; | so each command can be checked separately."""
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
            parts.append(cur)
            cur = ""
            i += 1
        elif ch in ";|\n":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


@dataclass
class Decision:
    decision: str  # allow | deny
    reason: str

    def hook_output(self) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": self.decision,
                                       "permissionDecisionReason": self.reason}}


def _intake_segments(command: str) -> list[str]:
    """Split safe shell chains; reject syntax that can redirect or execute outside a segment."""
    parts, cur, i, quote, escaped = [], "", 0, "", False
    while i < len(command):
        ch = command[i]
        if escaped:
            cur += ch
            escaped = False
        elif ch == "\\" and quote != "'":
            cur += ch
            escaped = True
        elif quote == "'":
            cur += ch
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch in "$`":
                raise ValueError("shell expansion is not allowed")
            cur += ch
            if ch == '"':
                quote = ""
        elif ch in "'\"":
            quote = ch
            cur += ch
        elif ch in "$\n\r":
            raise ValueError("shell expansion and newlines are not allowed")
        elif ch == "&":
            if not command.startswith("&&", i):
                raise ValueError("background commands are not allowed")
            if not cur.strip():
                raise ValueError("empty shell command")
            parts.append(cur)
            cur = ""
            i += 1
        elif ch in ";|":
            if not cur.strip():
                raise ValueError("empty shell command")
            parts.append(cur)
            cur = ""
            if ch == "|" and command.startswith("||", i):
                i += 1
        elif ch in "<>`()":
            raise ValueError("shell redirects and substitutions are not allowed")
        else:
            cur += ch
        i += 1
    if escaped or quote:
        raise ValueError("incomplete shell quoting")
    if not cur.strip():
        if parts:
            raise ValueError("empty shell command")
        return []
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _inspect_argv(segment: str) -> tuple[str, list[str]]:
    """Return an allowed inspection command and its parsed arguments."""
    argv = shlex.split(segment, posix=True)
    if not argv:
        return "", []
    command = argv[0]
    if command == "git" and len(argv) > 1:
        command = f"git {argv[1]}"
        args = argv[2:]
    else:
        args = argv[1:]
    if command not in INSPECT_ONLY:
        return "", args
    return command, args


def _argument_paths_within(args: list[str], command: str, root: str) -> bool:
    """Constrain command operands and path-taking options to the checkout."""
    paths = list(args)
    names = command.split()
    executable = names[-1]
    path_options = SHELL_PATH_OPTIONS.get(executable, ())
    for i, arg in enumerate(args):
        if arg in path_options and i + 1 < len(args):
            paths.append(args[i + 1])
        for option in path_options:
            if arg.startswith(option + "="):
                paths.append(arg.split("=", 1)[1])
            elif option.startswith("-") and len(option) == 2 and arg.startswith(option) and arg != option:
                paths.append(arg[len(option):])
        if arg.startswith("--") and "=" in arg:
            paths.append(arg.split("=", 1)[1])
    for path in paths:
        if path.startswith("~") or not _within(path, root):
            return False
    return True


def _has_writing_option(args: list[str], command: str) -> bool:
    """Check parsed shell arguments, including quoted and attached option values."""
    names = command.split()
    bad = WRITING_ARGS.get(command, WRITING_ARGS.get(names[-1], ()))
    return any(a == b or a.startswith(b + "=") or
               (b.startswith("-") and len(b) == 2 and a.startswith(b))
               for a in args for b in bad)


def _has_unsafe_option(args: list[str], command: str) -> bool:
    """Reject options that read external path lists or can launch configured diff tools."""
    names = command.split()
    bad = UNSAFE_ARGS.get(command, UNSAFE_ARGS.get(names[-1], ()))
    return any(a == b or a.startswith(b + "=") or
               (b.startswith("-") and not b.startswith("--") and len(b) == 2 and a.startswith(b))
               for a in args for b in bad)


def _confined_read_path(path: str, root: str) -> bool:
    """Check a file-tool path, including glob patterns, stays inside the checkout."""
    if not path or path.startswith("~"):
        return False
    if not _within(path, root):
        return False
    if any(ch in path for ch in "*?["):
        matches = glob.glob(path if os.path.isabs(path) else os.path.join(root, path), recursive=True)
        return all(_within(match, root) for match in matches)
    return True


def _within(path: str, root: str) -> bool:
    if not path:
        return False
    try:
        full = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
        base = os.path.realpath(root)
    except (OSError, ValueError):
        return False
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
                try:
                    args = shlex.split(seg, posix=True)[1:]
                except ValueError:
                    return Decision("deny", f"invalid shell quoting: {seg[:80]}")
                for arg in args:
                    candidates = [arg]
                    if "=" in arg:
                        candidates.append(arg.split("=", 1)[1])
                    if any(fnmatch.fnmatch(value, g) for value in candidates for g in deny_read):
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
    """The intake policy: no writes, reads stay in the checkout, and network tools are denied.

    The project's command allowlist is ignored (it holds installs and builds that change the worktree); its deny
    rules and deny_read globs still apply through decide(). Only INSPECT_ONLY shell commands are allowed.
    """
    if tool in WRITE_TOOLS:
        return Decision("deny", why)
    if tool in ("WebFetch", "WebSearch"):
        return Decision("deny", f"{why} (network access is not allowed during intake)")
    if tool in ("Read", "Glob", "Grep"):
        root = worktree
        if tool == "Read":
            path = str(tool_input.get("file_path") or "")
            paths = [path]
        elif tool == "Glob":
            path = str(tool_input.get("path") or "")
            pattern = str(tool_input.get("pattern") or "")
            paths = [p for p in (path, pattern) if p]
            if not paths:
                paths = ["."]
        else:
            path = str(tool_input.get("path") or "")
            paths = [path or "."]
        if not all(_confined_read_path(path, root) for path in paths):
            return Decision("deny", f"{why} (reads are allowed only inside the worktree)")
    if tool == "Bash":
        command = str(tool_input.get("command", "")).strip()
        try:
            segments = _intake_segments(command)
            if not segments:
                return Decision("deny", f"{why} (empty shell command)")
            for seg in segments:
                ro, args = _inspect_argv(seg)
                if not ro:
                    return Decision("deny", f"{why} (not a read-only command: {seg[:120]})")
                if _has_writing_option(args, ro):
                    return Decision("deny", f"{why} (this use of {ro} writes or runs a program: {seg[:120]})")
                if _has_unsafe_option(args, ro):
                    return Decision("deny", f"{why} (this use of {ro} can read outside the worktree: {seg[:120]})")
                if not _argument_paths_within(args, ro, worktree):
                    return Decision("deny", f"{why} (shell paths are allowed only inside the worktree: {seg[:120]})")
        except ValueError as e:
            return Decision("deny", f"{why} ({e})")
        policy = {**policy, "commands": {**(policy.get("commands") or {}), "allow": []}}
    return decide(policy, tool, tool_input, worktree)
