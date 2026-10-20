"""AI CLIs the supervisor can start, how to start them and how to read what they leave behind."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

NAMES = ("Claude", "Codex", "Grok", "OpenCode")
TOOL_PATHS = (Path.home() / ".local" / "bin", Path.home() / ".opencode" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
DENIED_BASH = (
    "Bash(rm -rf /:*)", "Bash(rm -rf ~:*)", "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git reset --hard:*)",
    "Bash(docker run:*)", "Bash(docker compose run:*)", "Bash(docker system prune:*)", "Bash(docker volume rm:*)",
)
# Usage limits and unusable accounts alike: both mean "this AI cannot work now", so the step moves to another AI.
# Phrases, not bare words: a log also holds file contents and commit names ("roles-quota") that must not count.
QUOTA_PATTERN = re.compile(
    r"usage limit (reached|exceeded|hit)|hit your (usage )?limit|rate.?limit(ed| exceeded| reached| hit)|"
    r"quota (exceeded|exhausted|reached)|exceeded (your )?(current )?quota|insufficient[_ ]quota|quota_exceeded|out of quota|"
    r"too many requests|credit balance|resets? (at|in) \d|"
    r"status 402|http 402|payment required|balance exhausted|usage balance|"
    r"not signed in|api key has expired|api_key_expired|invalid api key|please log ?in|authentication_error", re.I)
SMALL_LOG = 6000  # a run that dies on a usage or sign-in error leaves a short log; a long one is a run that wandered
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TITLE_PATTERN = re.compile(r"^PR_TITLE:\s*(.+?)\s*$", re.M)
VERDICT_PATTERN = re.compile(r"^(RESULT|VERDICT):\s*(DONE|PASS|FAIL|BLOCKED)\b[:\s-]*(.*)$", re.M)


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    extra = [str(p) for p in TOOL_PATHS if p.is_dir()]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def run_env(agent: str, workdir: Path, root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment of an AI process. PWD must be the worktree: some CLIs (OpenCode) take the project directory from PWD, not from
    the real working directory, and then treat the worktree as an external directory and reject every command in it."""
    env = child_env()
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env["PWD"] = str(workdir)
    env.update(extra or {})
    # No AI may write to GitHub: every AI acts as the owner's account, so its comments would be read back as the owner's own
    # feedback. An empty gh configuration and no token make every gh call fail to authenticate; the supervisor keeps its login.
    empty = root / ".tmp" / "aiflow" / "gh-empty"
    try:
        empty.mkdir(parents=True, exist_ok=True)
    except OSError:  # an unwritable root: gh then finds no configuration at that path either
        pass
    env["GH_CONFIG_DIR"] = str(empty)
    env["GH_PROMPT_DISABLED"] = "1"
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(name, None)
    if agent == "OpenCode":  # the files outside the worktree that the prompt sends it to: the Jira adapter, the shared runtime, comment files
        allow = {str(workdir): "allow", f"{workdir}/*": "allow", f"{root}/scripts/*": "allow", f"{root}/.tmp/*": "allow",
                 f"{root}/AGENTS.md": "allow", f"{root}/JOOservices/AGENTS.md": "allow"}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"permission": {"external_directory": allow}})
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
    bash += [f"Bash({root}/JOOservices/projects/ai-flow/bin/aiflow-mem:*)", f"Bash({root}/scripts/jira:*)", f"Bash({root}/scripts/docker/runtime:*)", f"Bash(scripts/docker/runtime:*)"]
    return ",".join(["Read", "Edit", "Write", "Glob", "Grep", *bash])


def parse_spec(spec: str) -> tuple[str, str, str]:
    """`Agent[:model[:effort]]` from the environment -> (agent, model, effort); missing parts are empty."""
    parts = [p.strip() for p in (spec or "").split(":")] + ["", ""]
    return parts[0], parts[1], parts[2]


READ_ONLY_TOOLS = "Read,Glob,Grep"


def build_command(agent: str, workdir: Path, result_file: Path, prompt: str, root: Path,
                  model: str = "", effort: str = "", read_only: bool = False) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text). The AI works only inside `workdir`; scripts/jira lives under `root`.
    `read_only` (the operator): the AI can read files and nothing else; OpenCode cannot be restricted, so it is refused."""
    if read_only and agent == "OpenCode":
        raise ValueError("OpenCode cannot run read-only")
    if agent == "Claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                *(["--model", model] if model else []), *(["--effort", effort] if effort else []),
                "--allowedTools", READ_ONLY_TOOLS if read_only else claude_allowed(root),
                "--disallowedTools", ",".join(DENIED_BASH),
                "--add-dir", str(root / ".tmp")]
        return argv, prompt
    if agent == "Codex":
        argv = ["codex", "exec", "-C", str(workdir), "-s", "read-only" if read_only else "danger-full-access", "--skip-git-repo-check", "-o", str(result_file),
                *(["-m", model] if model else []), *(["-c", f'model_reasoning_effort="{effort}"'] if effort else []), "-"]
        return argv, prompt
    if agent == "Grok":
        prompt_file = result_file.with_suffix(".prompt")
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        # plain output keeps the final message readable; the allowlist is the same as Claude's (Grok accepts --allowedTools rules)
        return ["grok", "--cwd", str(workdir), "--permission-mode", "dontAsk", "--output-format", "plain",
                "--allowedTools", READ_ONLY_TOOLS if read_only else claude_allowed(root), "--disallowedTools", ",".join(DENIED_BASH),
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


def probe_state(code: int | None, text: str, quota: bool) -> str:
    """Outcome of the one-word probe: `ok`, `down: <why>` (cannot work: signed out, balance, limit) or `unknown`."""
    if code == 0 and "ok" in text.lower():
        return "ok"
    if quota:
        found = QUOTA_PATTERN.search(ANSI.sub("", text))
        return "down: " + (found.group(0) if found else "no answer")
    return "unknown"


def probe(agent: str, root: Path, timeout: int = 60) -> str:
    """Ask the AI for one word. Only a clear sign-in, balance or limit error counts as down; a timeout or any other
    failure is `unknown` and leaves the AI in the pool."""
    workdir = root / ".tmp" / "aiflow" / "probe"
    workdir.mkdir(parents=True, exist_ok=True)
    result, log_file = workdir / f"{agent}.out", workdir / f"{agent}.log"
    argv, stdin = build_command(agent, workdir, result, "Reply with the single word OK. Do nothing else.", root)
    try:
        with log_file.open("wb") as handle:
            proc = subprocess.Popen(argv, cwd=workdir, env=run_env(agent, workdir, root), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                proc.communicate(stdin.encode() if stdin is not None else None, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return "unknown"
    except OSError as error:
        return f"down: {error}"
    text = final_text(agent, log_file, result)
    return probe_state(proc.returncode, text, looks_like_quota(log_file, text, proc.returncode))


def tool_versions() -> dict[str, str]:
    """First line of `--version` for the tools the flow depends on."""
    found = {}
    for name in ("claude", "codex", "grok", "opencode", "gh", "node", "jq", "git"):
        try:
            out = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=15, env=child_env()).stdout.strip().splitlines()
            found[name] = out[0][:60] if out else "?"
        except (OSError, subprocess.TimeoutExpired):
            found[name] = "missing"
    return found


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


def looks_like_quota(log_file: Path, text: str, code: int | None = None) -> bool:
    """Did the AI stop because it cannot work now (usage limit, empty balance, not signed in)?

    A short log is searched whole. A long log only counts when the process failed, and then only its last lines."""
    if not log_file.is_file():
        return bool(QUOTA_PATTERN.search(text[-2000:]))
    size = log_file.stat().st_size
    with log_file.open("rb") as handle:
        if size > SMALL_LOG:
            if not code:
                return False
            handle.seek(size - 1500)
        tail = handle.read().decode("utf-8", errors="replace")
    return bool(QUOTA_PATTERN.search(tail)) or (size <= SMALL_LOG and bool(QUOTA_PATTERN.search(text[-2000:])))


def choose_other(dev: str, present: list[str], avoid: tuple[str, ...] = ()) -> str | None:
    """Reviewer and QA must differ from the developer; prefer one that also differs from `avoid`."""
    others = [a for a in present if a != dev]
    fresh = [a for a in others if a not in avoid]
    return (fresh or others or [None])[0]
