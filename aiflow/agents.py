"""AI CLIs the supervisor can start, how to start them and how to read what they leave behind."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

NAMES = ("Claude", "Codex", "Grok", "OpenCode")
TOOL_PATHS = (Path.home() / ".local" / "bin", Path.home() / ".opencode" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
DENIED_BASH = (
    "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git reset --hard:*)",
    "Bash(docker run:*)", "Bash(docker compose run:*)", "Bash(docker system prune:*)", "Bash(docker volume rm:*)",
)
# Usage limits and unusable accounts alike: both mean "this AI cannot work now", so the ticket moves to another AI.
QUOTA_PATTERN = re.compile(
    r"usage limit|rate.?limit|quota|\b429\b|credit balance|too many requests|overloaded|resets? (at|in)|"
    r"status 402|http 402|payment required|balance exhausted|usage balance|"
    r"not signed in|api key has expired|api_key_expired|invalid api key|please log ?in|authentication_error", re.I)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TITLE_PATTERN = re.compile(r"^PR_TITLE:\s*(.+?)\s*$", re.M)
VERDICT_PATTERN = re.compile(r"^(RESULT|VERDICT):\s*(DONE|PASS|FAIL|BLOCKED)\b[:\s-]*(.*)$", re.M)


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [str(p) for p in TOOL_PATHS if p.is_dir()]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def available(order: tuple[str, ...]) -> list[str]:
    path = child_env()["PATH"]
    binaries = {"Claude": "claude", "Codex": "codex", "Grok": "grok", "OpenCode": "opencode"}
    return [name for name in order if name in binaries and shutil.which(binaries[name], path=path)]


SAFE_BASH = ("git", "python3", "ls", "cat", "grep", "find", "sed", "head", "tail", "wc", "od", "diff", "mkdir", "touch", "cp", "mv", "rm",
             "sleep", "echo", "printf", "test", "cd", "pwd", "date", "sort", "uniq", "tr", "cut", "basename", "dirname", "xargs", "tee")


def claude_allowed(root: Path) -> str:
    """Claude may edit files, search, and run a fixed set of commands plus the Jira adapter and the shared runtimes."""
    bash = [f"Bash({name}:*)" for name in SAFE_BASH]
    bash += [f"Bash({root}/scripts/jira:*)", f"Bash({root}/scripts/docker/runtime:*)", f"Bash(scripts/docker/runtime:*)"]
    return ",".join(["Read", "Edit", "Write", "Glob", "Grep", *bash])


def parse_spec(spec: str) -> tuple[str, str, str]:
    """`Agent[:model[:effort]]` from the environment -> (agent, model, effort); missing parts are empty."""
    parts = [p.strip() for p in (spec or "").split(":")] + ["", ""]
    return parts[0], parts[1], parts[2]


def build_command(agent: str, workdir: Path, result_file: Path, prompt: str, root: Path,
                  model: str = "", effort: str = "") -> tuple[list[str], str | None]:
    """Return (argv, stdin_text). The AI works only inside `workdir`; scripts/jira lives under `root`."""
    if agent == "Claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                *(["--model", model] if model else []), *(["--effort", effort] if effort else []),
                "--allowedTools", claude_allowed(root),
                "--disallowedTools", ",".join(DENIED_BASH),
                "--add-dir", str(root / ".tmp")]
        return argv, prompt
    if agent == "Codex":
        argv = ["codex", "exec", "-C", str(workdir), "-s", "danger-full-access", "--skip-git-repo-check", "-o", str(result_file),
                *(["-m", model] if model else []), *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []), "-"]
        return argv, prompt
    if agent == "Grok":
        prompt_file = result_file.with_suffix(".prompt")
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        # plain output keeps the final message readable; the allowlist is the same as Claude's (Grok accepts --allowedTools rules)
        return ["grok", "--cwd", str(workdir), "--permission-mode", "dontAsk", "--output-format", "plain",
                "--allowedTools", claude_allowed(root), "--disallowedTools", ",".join(DENIED_BASH),
                *(["-m", model] if model else []), *(["--reasoning-effort", effort] if effort else []),
                "--prompt-file", str(prompt_file)], None
    if agent == "OpenCode":
        return ["opencode", "run", *(["-m", model] if model else []), prompt], None
    raise ValueError(f"unknown agent {agent!r}")


def final_text(agent: str, log_file: Path, result_file: Path) -> str:
    """The last message of the AI."""
    if agent == "Codex" and result_file.is_file():
        return result_file.read_text(encoding="utf-8", errors="replace")
    text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
    if agent == "Claude":
        for line in reversed(text.splitlines()):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                return str(event.get("result", ""))
    return ANSI.sub("", text)[-8000:]


def verdict(text: str) -> tuple[str, str] | None:
    """Last `RESULT:`/`VERDICT:` line, as (DONE|PASS|FAIL|BLOCKED, reason)."""
    found = VERDICT_PATTERN.findall(text)
    if not found:
        return None
    _, word, reason = found[-1]
    return word.upper(), reason.strip()


def pr_title(text: str) -> str | None:
    """Last `PR_TITLE:` line of the developer's final message."""
    found = TITLE_PATTERN.findall(text)
    return found[-1].strip("` ") if found else None


def looks_like_quota(log_file: Path, text: str) -> bool:
    tail = ""
    if log_file.is_file():
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 20000))
            tail = handle.read().decode("utf-8", errors="replace")
    return bool(QUOTA_PATTERN.search(tail) or QUOTA_PATTERN.search(text[-2000:]))


def choose_other(dev: str, present: list[str], avoid: tuple[str, ...] = ()) -> str | None:
    """Reviewer and QA must differ from the developer; prefer one that also differs from `avoid`."""
    others = [a for a in present if a != dev]
    fresh = [a for a in others if a not in avoid]
    return (fresh or others or [None])[0]
