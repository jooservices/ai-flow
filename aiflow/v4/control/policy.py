"""Policy decisions for AI tool calls (the PreToolUse hook of Claude, the run wrapper of other CLIs).

Rules come from the control repo (policy/default.yaml, overridable per project). Deny wins over allow; a
command that matches no rule is denied with a reason, so a gap in the policy shows up as a denial that the
question router can pick up, never as a silent approval.
"""
from __future__ import annotations

import fnmatch
import glob
import os
import re
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


MASTER_ACTIONS = {"jira_comment", "jira_update", "dev_reply", "requeue", "incident_open", "incident_close",
                  "notify_owner", "escalate_to_owner"}
# Matched only against what the master requests (the action name, its intent and an explicit category), never
# against observations such as "node1 is offline" or "the run passed the hard limit, restart it": monitoring
# talks about nodes, servers and restarts all the time, and those follow-ups are allowed master work.
ESCALATE_TO_OWNER_CATEGORIES = ("infrastructure", "secret", "production_release")
ESCALATE_TO_OWNER_DEFAULTS = {
    "infrastructure": [
        r"\b(?:change|modify|upgrade|downgrade|reconfigure|configure|provision|decommission|migrate|resize|"
        r"thay đổi|nâng cấp|hạ cấp|cấu hình|cấp phát)\b.{0,60}\b"
        r"(?:infrastructure|infra|hạ tầng|cluster|firewall|dns|load balancer|vpc|terraform|ansible)\b",
        r"\b(?:infrastructure|infra|hạ tầng|cluster|firewall|dns|load balancer|vpc|terraform|ansible)\b"
        r".{0,60}\b(?:change|modify|upgrade|downgrade|reconfigure|configure|provision|decommission|migrate|resize|"
        r"thay đổi|nâng cấp|hạ cấp|cấu hình|cấp phát)\b",
        r"\b(?:provision|decommission)\b.{0,60}\b(?:server|máy chủ|vm|node)\b",
        r"\binfra(?:structure)?[ _]change\b|\bchange[ _]infra(?:structure)?\b",
    ],
    "secret": [
        r"\b(?:rotate|change|create|delete|revoke|expose|reveal|replace|reset|xoay vòng|thu hồi|tiết lộ|thay thế|"
        r"thay đổi)\b.{0,60}\b"
        r"(?:secrets?|bí mật|credentials?|thông tin xác thực|password|mật khẩu|private key|api key|access token)\b",
        r"\b(?:secrets?|bí mật|credentials?|thông tin xác thực|password|mật khẩu|private key|api key|access token)\b"
        r".{0,60}\b(?:rotate|change|create|delete|revoke|expose|reveal|replace|reset|xoay vòng|thu hồi|tiết lộ|"
        r"thay thế|thay đổi)\b",
    ],
    "production_release": [
        r"\b(?:release|deploy|publish|roll out|ship|phát hành|triển khai|đưa lên)\b.{0,100}\b"
        r"(?:production|prod|sản xuất)\b",
        r"\b(?:production|prod|sản xuất)\b.{0,100}\b"
        r"(?:release|deploy|publish|roll out|ship|phát hành|triển khai|đưa lên)\b",
    ],
}


def decide_master_action(policy: dict, action: str, intent: str, category: str = "") -> Decision:
    """Allow only the master control API actions; always escalate protected operational work.

    `intent` is the request itself (what the master wants done), not the observation that led to it."""
    if action == "escalate_to_owner":
        return Decision("escalate", "escalate_to_owner was explicitly requested")
    if category in ESCALATE_TO_OWNER_CATEGORIES:
        return Decision("escalate", f"escalate_to_owner: {category}")
    rules = {key: list(patterns) for key, patterns in ESCALATE_TO_OWNER_DEFAULTS.items()}
    for category, patterns in ((policy or {}).get("escalate_to_owner") or {}).items():
        if isinstance(patterns, str):
            patterns = [patterns]
        rules.setdefault(category, []).extend(patterns)
    text = re.sub(r"[_-]+", " ", f"{action}\n{intent or ''}")[:5000]
    for category, patterns in rules.items():
        for pattern in patterns:
            try:
                if re.search(str(pattern), text, flags=re.IGNORECASE):
                    return Decision("escalate", f"escalate_to_owner: {category}")
            except re.error:
                # Invalid control-repo policy must not weaken mandatory protected-action rules.
                continue
    if action not in MASTER_ACTIONS:
        return Decision("deny", f"master action '{action}' is not allowed")
    return Decision("allow", "allowed master control API action")


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


def _same_worktree(path: str, worktree: str) -> bool:
    """Accept only a bind source that resolves to the current checkout root."""
    if not path:
        return False
    candidate = path if os.path.isabs(path) else os.path.join(worktree, path)
    try:
        return os.path.realpath(candidate) == os.path.realpath(worktree)
    except (OSError, ValueError):
        return False


def _container_command_allowed(segment: str, worktree: str) -> bool:
    """Small shell-command allowlist for the brokered Docker-compatible interface."""
    if any(char in segment for char in "$`<>\r\n"):
        return False
    try:
        argv = shlex.split(segment, posix=True)
    except ValueError:
        return False
    if not argv or argv[0] != "docker":
        return False
    args = argv[1:]
    if not args:
        return False
    if args[0] == "build":
        i, contexts = 1, []
        value_options = {"-f", "--file", "-t", "--tag", "--target", "--platform", "--build-arg",
                         "--progress", "--network"}
        flags = {"--pull", "--no-cache", "--quiet"}
        while i < len(args):
            arg = args[i]
            if arg in flags:
                i += 1
            elif arg in value_options:
                if i + 1 >= len(args):
                    return False
                value = args[i + 1]
                if arg in {"-f", "--file"} and not _within(value, worktree):
                    return False
                if arg == "--network" and value not in {"default", "none"}:
                    return False
                i += 2
            elif arg.startswith("--network="):
                if arg.split("=", 1)[1] not in {"default", "none"}:
                    return False
                i += 1
            elif arg.startswith("-"):
                return False
            else:
                contexts.append(arg)
                i += 1
        return len(contexts) == 1 and _within(contexts[0], worktree)
    if args[0] == "run":
        i, mounts = 1, []
        value_options = {"--name", "--workdir", "-w", "--env", "-e", "--publish", "-p", "--expose",
                         "--entrypoint", "--user", "-u", "--label", "--platform", "--network",
                         "--hostname", "--dns", "--pull", "--memory", "--cpus"}
        flags = {"--rm", "-i", "--interactive", "-t", "--tty", "--init"}
        image_seen = False
        while i < len(args):
            arg = args[i]
            if arg == "--":
                return image_seen and len(mounts) == 1
            if not image_seen:
                if arg in flags:
                    i += 1
                elif arg in {"-v", "--volume"}:
                    if i + 1 >= len(args):
                        return False
                    mounts.append(args[i + 1])
                    i += 2
                elif arg.startswith("--mount="):
                    mounts.append("mount=" + arg.split("=", 1)[1])
                    i += 1
                elif arg == "--mount":
                    if i + 1 >= len(args):
                        return False
                    mounts.append("mount=" + args[i + 1])
                    i += 2
                elif arg in value_options:
                    if i + 1 >= len(args):
                        return False
                    if arg == "--network" and args[i + 1] not in {"default", "bridge", "none"}:
                        return False
                    i += 2
                elif arg.startswith("--network="):
                    if arg.split("=", 1)[1] not in {"default", "bridge", "none"}:
                        return False
                    i += 1
                elif arg.startswith("-"):
                    return False
                else:
                    image_seen = True
                    i += 1
            else:
                i += 1
        if not image_seen or len(mounts) != 1:
            return False
        mount = mounts[0]
        if mount.startswith("mount="):
            fields = dict(part.split("=", 1) for part in mount[6:].split(",") if "=" in part)
            source = fields.get("src", fields.get("source", ""))
            return (fields.get("type") == "bind" and _same_worktree(source, worktree)
                    and not fields.get("bind-propagation"))
        parts = mount.split(":")
        return (len(parts) in (2, 3) and _same_worktree(parts[0], worktree)
                and (len(parts) == 2 or set(parts[2].split(",")) <= {"ro", "rw"}))
    if args[0] == "compose":
        i, files = 1, []
        while i < len(args):
            arg = args[i]
            if arg in {"-p", "--project-name"}:
                return False
            if arg in {"-f", "--file", "--env-file", "--project-directory", "--profile"}:
                if i + 1 >= len(args):
                    return False
                if arg in {"-f", "--file", "--env-file", "--project-directory"} and not _within(args[i + 1], worktree):
                    return False
                if arg in {"-f", "--file"}:
                    files.append(args[i + 1])
                i += 2
                continue
            if arg.startswith("--file=") or arg.startswith("--env-file=") or arg.startswith("--project-directory="):
                path = arg.split("=", 1)[1]
                if not _within(path, worktree):
                    return False
                if arg.startswith("--file="):
                    files.append(path)
                i += 1
                continue
            break
        if i >= len(args) or args[i] not in {"up", "run"}:
            return False
        subcommand, tail = args[i], args[i + 1:]
        if not tail and args[i] == "run":
            return False
        if subcommand == "up":
            safe_flags = {"-d", "--detach", "--build", "--no-build", "--pull", "--remove-orphans",
                          "--force-recreate", "--no-recreate", "--no-deps", "--wait", "--quiet-pull",
                          "--renew-anon-volumes", "--timestamps"}
            value_flags = {"--timeout", "-t"}
            for arg in tail:
                if arg in safe_flags or arg in value_flags:
                    continue
                if arg.startswith("--") and any(arg.startswith(flag + "=") for flag in value_flags):
                    continue
                if arg.startswith("-"):
                    return False
            return True
        safe_flags = {"--rm", "--build", "--no-deps", "--quiet-pull", "-T", "--no-TTY",
                      "--service-ports", "--use-aliases"}
        value_flags = {"--entrypoint", "--env", "-e", "--user", "-u", "--workdir", "-w"}
        i = 0
        while i < len(tail) and tail[i].startswith("-"):
            if tail[i] in safe_flags:
                i += 1
            elif tail[i] in value_flags:
                i += 2
                if i > len(tail):
                    return False
            elif any(tail[i].startswith(flag + "=") for flag in value_flags):
                i += 1
            else:
                return False
        return i < len(tail)  # the remaining arguments are service name and its command
    return False


def decide(policy: dict[str, Any], tool: str, tool_input: dict[str, Any], worktree: str,
           *, containers: bool = False) -> Decision:
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
            if re.search(r"\bdocker\b", seg):
                if not containers:
                    return Decision("deny", "this project has not enabled the safe container runtime")
                if not seg.startswith("docker ") or not _container_command_allowed(seg, worktree):
                    return Decision("deny", "container command is outside the safe build, compose, and mounted-run policy")
                continue
            if re.search(r"\bpodman\b", seg):
                return Decision("deny", "call the brokered Docker-compatible command; direct Podman access is disabled")
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
    """Read-only AI work cannot write, read outside the checkout, or access the network.

    The project's command allowlist is ignored (it holds installs and builds that change the worktree); its deny
    rules and deny_read globs still apply through decide(). Only INSPECT_ONLY shell commands are allowed.
    """
    if tool in WRITE_TOOLS:
        return Decision("deny", why)
    if tool in ("WebFetch", "WebSearch"):
        return Decision("deny", f"{why} (network access is not allowed during a read-only run)")
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
