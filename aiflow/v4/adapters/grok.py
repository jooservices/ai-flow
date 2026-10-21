"""Grok Build: `grok -p` with streaming-json. Spike S4: an empty balance is an error event with HTTP 402."""
from __future__ import annotations

from .base import AUTH_PATTERN, QUOTA_PATTERN, Adapter, RunResult, RunSpec


class GrokAdapter(Adapter):
    provider = "grok"

    def argv(self, spec: RunSpec) -> list[str]:
        cmd = ["grok", "-p", spec.prompt, "--output-format", "streaming-json"]
        if spec.model:
            cmd += ["-m", spec.model]
        return cmd

    def parse_event(self, ev: dict, res: RunResult) -> None:
        t = ev.get("type")
        if t == "error":
            msg = str(ev.get("message") or "")
            res.error_kind = "quota" if QUOTA_PATTERN.search(msg) else ("auth" if AUTH_PATTERN.search(msg) else "provider")
        elif t in ("result", "final", "message") and ev.get("text"):
            res.final_text = ev.get("text") or res.final_text
        elif t == "result" and ev.get("result"):
            res.final_text = ev.get("result")
