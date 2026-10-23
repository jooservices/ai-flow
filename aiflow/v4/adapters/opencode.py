"""OpenCode: `opencode run --format json`; explicit-only (used when a ticket names it)."""
from __future__ import annotations

from .base import AUTH_PATTERN, QUOTA_PATTERN, Adapter, RunResult, RunSpec


class OpenCodeAdapter(Adapter):
    provider = "opencode"

    def argv(self, spec: RunSpec) -> list[str]:
        cmd = ["opencode", "run", "--format", "json"]
        if spec.model:
            cmd += ["-m", spec.model]
        return cmd + [spec.prompt]

    def parse_event(self, ev: dict, res: RunResult) -> None:
        t = ev.get("type")
        part = ev.get("part") or {}
        if t == "tool":
            tool = str(part.get("tool") or ev.get("tool") or "").lower()
            state = part.get("state") or ev.get("state") or {}
            state_input = state.get("input") or part.get("input") or {}
            command = state_input.get("command") if isinstance(state_input, dict) else ""
            status = str(state.get("status") or "").lower()
            exit_code = state.get("exit")
            if tool in {"bash", "shell"} and isinstance(command, str) and (
                    status == "error" or (exit_code is not None and exit_code != 0)):
                self.record_command_result(res, command, state.get("output") or part.get("output") or "",
                                           exit_code if exit_code is not None else 1)
        elif t == "text" and part.get("text"):
            res.final_text = part["text"]
        elif t == "error":
            msg = str(ev.get("error") or ev.get("message") or "")
            res.error_message = msg[:400]
            res.error_kind = "quota" if QUOTA_PATTERN.search(msg) else ("auth" if AUTH_PATTERN.search(msg) else "provider")
