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
        if t == "text" and part.get("text"):
            res.final_text = part["text"]
        elif t == "error":
            msg = str(ev.get("error") or ev.get("message") or "")
            res.error_message = msg[:400]
            res.error_kind = "quota" if QUOTA_PATTERN.search(msg) else ("auth" if AUTH_PATTERN.search(msg) else "provider")
