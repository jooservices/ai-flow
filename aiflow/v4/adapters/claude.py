"""Claude Code: `claude -p` with stream-json, permission mode dontAsk and an http PreToolUse hook.

Spike S1: in dontAsk mode the hook's allow lets write commands through and its deny blocks them; a silent
hook leaves only read-only commands. The hook sends the run token so control-api knows which run asks.
Spike S4: `rate_limit_event` carries utilization of the 5-hour and 7-day windows and their reset times.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .base import Adapter, RunResult, RunSpec, ts

HOOK_TOOLS = "Bash|Edit|Write|MultiEdit|NotebookEdit"


class ClaudeAdapter(Adapter):
    provider = "claude"

    def settings_path(self, spec: RunSpec) -> Path:
        return Path(spec.account_home) / ".aiflow" / "claude-run-settings.json"

    def prepare(self, spec: RunSpec) -> None:
        p = self.settings_path(spec)
        p.parent.mkdir(parents=True, exist_ok=True)
        policy_base = ("http://127.0.0.1:8471" if os.environ.get("AIFLOW4_POLICY_RELAY", "1") == "1"
                       else spec.control_url.rstrip("/"))
        settings = {
            "env": {"DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"},
            "hooks": {"PreToolUse": [{"matcher": HOOK_TOOLS, "hooks": [{
                "type": "http", "url": f"{policy_base}/v1/policy/decide", "timeout": 65,
                "onFailure": "block", "headers": {"Authorization": "Bearer $AIFLOW4_RUN_TOKEN"},
                "allowedEnvVars": ["AIFLOW4_RUN_TOKEN"]}]}]},
        }
        p.write_text(json.dumps(settings, indent=2))
        p.chmod(0o600)
        home = Path(spec.account_home)
        if home.exists():  # the worker runs as root; the file must belong to the account's user
            st = home.stat()
            for target in (p.parent, p):
                try:
                    os.chown(target, st.st_uid, st.st_gid)
                except PermissionError:
                    pass

    def argv(self, spec: RunSpec) -> list[str]:
        cmd = ["claude", "-p", spec.prompt, "--output-format", "stream-json", "--verbose",
               "--permission-mode", "dontAsk", "--settings", str(self.settings_path(spec))]
        if spec.model:
            cmd += ["--model", spec.model]
        return cmd

    def env(self, spec: RunSpec) -> dict[str, str]:
        return {"CLAUDE_CONFIG_DIR": f"{spec.account_home}/.claude", "DISABLE_AUTOUPDATER": "1",
                "AIFLOW4_RUN_TOKEN": spec.run_token}

    def parse_event(self, ev: dict, res: RunResult) -> None:
        t = ev.get("type")
        if t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            windows = info.get("unifiedWindows") or {}
            res.quota = {"provider": "claude", "status": info.get("status"), "type": info.get("rateLimitType"),
                         "resets_at": info.get("resetsAt"),
                         "windows": {k: {"utilization": v.get("utilization"), "resets_at": v.get("resetsAt")}
                                     for k, v in windows.items()}}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                res.error_kind = "quota"
                res.reset_at = ts(info.get("resetsAt"))
        elif t == "result":
            res.final_text = ev.get("result") or ""
            res.permission_denials = len(ev.get("permission_denials") or [])
            res.cost_usd = float(ev.get("total_cost_usd") or 0)
            u = ev.get("usage") or {}
            res.tokens_in = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + \
                int(u.get("cache_read_input_tokens") or 0)
            res.tokens_out = int(u.get("output_tokens") or 0)
            if ev.get("is_error") and not res.error_kind:
                res.error_kind = "provider"
