"""Codex: `codex exec --json` in the workspace-write sandbox (spike S2: needs bubblewrap; .git is read-only,
so the AI cannot commit; network is opened by config for package installs). Account isolation by CODEX_HOME.
"""
from __future__ import annotations

from .base import Adapter, RunResult, RunSpec


class CodexAdapter(Adapter):
    provider = "codex"

    def argv(self, spec: RunSpec) -> list[str]:
        cmd = ["codex", "exec", "--json", "--cd", spec.cwd, "--sandbox", "workspace-write",
               "-c", 'approval_policy="never"', "-c", "sandbox_workspace_write.network_access=true",
               "-c", "features.apps=false"]
        if spec.model:
            cmd += ["-m", spec.model]
        if spec.effort:
            cmd += ["-c", f'model_reasoning_effort="{spec.effort}"']
        return cmd + [spec.prompt]

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {"CODEX_HOME": f"{spec.account_home}/.codex"}

    def parse_event(self, ev: dict, res: RunResult) -> None:
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message"):
                res.final_text = item.get("text") or res.final_text
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            res.tokens_in += int(u.get("input_tokens") or 0)
            res.tokens_out += int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
        elif t in ("error", "turn.failed"):
            msg = str(ev.get("message") or (ev.get("error") or {}).get("message") or "")
            res.final_text = res.final_text or ""
            res.error_message = msg[:400]
            from .base import AUTH_PATTERN, QUOTA_PATTERN
            if QUOTA_PATTERN.search(msg):
                res.error_kind = "quota"
            elif AUTH_PATTERN.search(msg):
                res.error_kind = "auth"
            elif not res.error_kind:
                res.error_kind = "provider"
