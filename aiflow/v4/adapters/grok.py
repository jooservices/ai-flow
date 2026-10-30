"""Grok Build: `grok -p` with streaming-json. Spike S4: an empty balance is an error event with HTTP 402."""
from __future__ import annotations

from .base import AUTH_PATTERN, QUOTA_PATTERN, Adapter, RunResult, RunSpec, quota_from_event, record_quota_observation


def _token_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _has_token_count(value: object) -> bool:
    try:
        return value is not None and int(value) >= 0
    except (TypeError, ValueError):
        return False


def _usage_counts(usage: dict) -> tuple[int, int]:
    # Grok reports uncached prompt tokens separately from cache reads/writes. The cache buckets are
    # part of the prompt total; reasoning_tokens is already included in output_tokens/total_tokens.
    tokens_in = sum(_token_count(usage.get(key)) for key in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return tokens_in, _token_count(usage.get("output_tokens"))


def _record_cost(ev: dict, res: RunResult) -> None:
    if ev.get("total_cost_usd") is not None:
        try:
            res.cost_usd = float(ev["total_cost_usd"])
        except (TypeError, ValueError):
            pass


def _record_usage(event: dict, res: RunResult, *, replace: bool = False) -> None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return
    tokens_in, tokens_out = _usage_counts(usage)
    input_keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    has_input = any(_has_token_count(usage.get(key)) for key in input_keys)
    complete_usage = has_input and _has_token_count(usage.get("output_tokens"))
    if complete_usage:
        res.token_usage_reported = True
    elif replace:
        return
    if replace:
        # The terminal end event contains the aggregate for the complete run; it supersedes the
        # per-response usage events above it.
        res.tokens_in, res.tokens_out = tokens_in, tokens_out
    else:
        res.tokens_in += tokens_in
        res.tokens_out += tokens_out


class GrokAdapter(Adapter):
    provider = "grok"

    def argv(self, spec: RunSpec) -> list[str]:
        cmd = ["grok", "-p", spec.prompt, "--output-format", "streaming-json"]
        if spec.model:
            cmd += ["-m", spec.model]
        return cmd

    def parse_event(self, ev: dict, res: RunResult) -> None:
        quota = quota_from_event(ev, self.provider)
        if quota:
            record_quota_observation(res, quota)
        t = ev.get("type")
        if t == "text":
            # streaming-json text.data is a delta, so keep appending chunks in arrival order.
            data = ev.get("data")
            if isinstance(data, str):
                res.final_text += data
        elif t == "usage":
            _record_usage(ev, res)
        elif t == "end":
            _record_usage(ev, res, replace=True)
            _record_cost(ev, res)
        elif t == "error":
            msg = str(ev.get("message") or "")
            res.error_message = msg[:400]
            res.error_kind = "quota" if QUOTA_PATTERN.search(msg) else ("auth" if AUTH_PATTERN.search(msg) else "provider")
            _record_usage(ev, res, replace=True)
            _record_cost(ev, res)
        elif t in ("result", "final", "message") and ev.get("text"):
            res.final_text = ev.get("text") or res.final_text
        elif t == "result" and ev.get("result"):
            res.final_text = ev.get("result")
