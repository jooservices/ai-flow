"""Codex: `codex exec --json` in the workspace-write sandbox.

The AI edits the worktree, then the worker commits and pushes after the process exits. In particular, do not
give Codex writable access to a worktree's shared Git metadata: it belongs to the repository mirror and is
shared by linked worktrees. Network is opened by config for package installs. Account isolation by CODEX_HOME.
"""
from __future__ import annotations

from .base import Adapter, RunResult, RunSpec, quota_from_event, scratch_root


class CodexAdapter(Adapter):
    provider = "codex"

    def argv(self, spec: RunSpec) -> list[str]:
        # The worker owns Git mutations. Keep Codex contained even if a caller accidentally requests the
        # broader mode; workspace-write still permits edits in cwd and the explicit shared scratch root.
        sandbox = "workspace-write" if spec.sandbox == "danger-full-access" else spec.sandbox
        cmd = ["codex", "exec", "--json", "--cd", spec.cwd, "--sandbox", sandbox,
               "-c", 'approval_policy="never"', "-c", "features.apps=false"]
        if sandbox == "workspace-write":
            cmd += ["-c", "sandbox_workspace_write.network_access=true",
                    "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                    # the shared caches and temp dir on disk (see scratch_env) are writable inside the sandbox
                    "-c", f'sandbox_workspace_write.writable_roots=["{scratch_root(spec)}"]']
        if spec.model:
            cmd += ["-m", spec.model]
        if spec.effort:
            cmd += ["-c", f'model_reasoning_effort="{spec.effort}"']
        return cmd + [spec.prompt]

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {"CODEX_HOME": f"{spec.account_home}/.codex"}

    def parse_event(self, ev: dict, res: RunResult) -> None:
        quota = quota_from_event(ev, self.provider)
        if quota:
            res.quota = quota
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message"):
                res.final_text = item.get("text") or res.final_text
            elif item.get("type") == "command_execution":
                exit_code = item.get("exit_code")
                if exit_code not in (None, 0):
                    self.record_command_result(res, item.get("command") or "",
                                               item.get("aggregated_output") or item.get("output") or "", exit_code)
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
