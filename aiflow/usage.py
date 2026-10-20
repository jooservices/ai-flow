"""Token usage of one AI run, read from the log the run already wrote (no extra call, no extra token).

    Claude    stream-json: the last `result` event (input, output, cache read/write, thinking, cost, turns, per model)
    Codex     `codex exec --json`: the `turn.completed` events (input, cached, output, reasoning); plain logs: the `tokens used N` line
    OpenCode  `opencode run --format json`: the `step-finish` events (tokens and cost)
    Grok      its JSON output was not verified (the account has no balance): usage_known=false
Numbers are reported as the AI reports them; `cost_usd_list` is the cost at list price, not money paid (a subscription is not billed per token)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import events

FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")


def _events(text: str):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _row(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {name: int(kw.get(name) or 0) for name in FIELDS}
    row.update(cost_usd_list=round(float(kw.get("cost") or 0), 6), model=str(kw.get("model") or ""), turns=int(kw.get("turns") or 0), usage_known=True)
    return row


def claude(text: str) -> dict[str, Any] | None:
    result = None
    for event in _events(text):
        if event.get("type") == "result" and "usage" in event:
            result = event
    if result is None:
        return None
    use = result["usage"]
    models = list((result.get("modelUsage") or {}))
    return _row(input=use.get("input_tokens"), output=use.get("output_tokens"), cache_read=use.get("cache_read_input_tokens"),
                cache_write=use.get("cache_creation_input_tokens"), reasoning=(use.get("output_tokens_details") or {}).get("thinking_tokens"),
                total=sum(int(use.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
                cost=result.get("total_cost_usd"), model=",".join(models), turns=result.get("num_turns"))


def codex(text: str) -> dict[str, Any] | None:
    total = dict.fromkeys(("input", "cached", "cache_write", "output", "reasoning"), 0)
    seen = False
    for event in _events(text):
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            seen = True
            use = event["usage"]
            for name, key in (("input", "input_tokens"), ("cached", "cached_input_tokens"), ("cache_write", "cache_write_input_tokens"),
                              ("output", "output_tokens"), ("reasoning", "reasoning_output_tokens")):
                total[name] += int(use.get(key) or 0)
    if seen:
        return _row(input=total["input"] - total["cached"], output=total["output"], cache_read=total["cached"], cache_write=total["cache_write"],
                    reasoning=total["reasoning"], total=total["input"] + total["output"])
    match = re.search(r"^tokens used\s*\n\s*([\d,]+)\s*$", text, re.M)  # a plain log: one number, no split
    return _row(total=int(match.group(1).replace(",", ""))) if match else None


def opencode(text: str) -> dict[str, Any] | None:
    total = dict.fromkeys(("total", "input", "output", "reasoning", "read", "write", "cost", "steps"), 0.0)
    for event in _events(text):
        part = event.get("part") or {}
        if event.get("type") in ("step_finish", "step-finish") and isinstance(part.get("tokens"), dict):
            tokens = part["tokens"]
            cache = tokens.get("cache") or {}
            total["steps"] += 1
            total["total"] += tokens.get("total") or 0
            total["input"] += tokens.get("input") or 0
            total["output"] += tokens.get("output") or 0
            total["reasoning"] += tokens.get("reasoning") or 0
            total["read"] += cache.get("read") or 0
            total["write"] += cache.get("write") or 0
            total["cost"] += part.get("cost") or 0
    if not total["steps"]:
        return None
    return _row(input=total["input"], output=total["output"], cache_read=total["read"], cache_write=total["write"], reasoning=total["reasoning"],
                total=total["total"], cost=total["cost"], turns=total["steps"])


PARSERS = {"Claude": claude, "Codex": codex, "OpenCode": opencode}


def parse(agent: str, text: str) -> dict[str, Any]:
    """The usage of a run, or `{"usage_known": False}` when this AI does not report it or the log has no report (e.g. it was stopped)."""
    parser = PARSERS.get(agent)
    try:
        found = parser(text) if parser else None
    except (ValueError, TypeError, KeyError):
        found = None
    return found or {"usage_known": False}


def report(agent: str, log_file: Path, **fields: Any) -> dict[str, Any]:
    """Read the log, emit `ai.usage` and return the numbers (for the daily summary)."""
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    found = parse(agent, text)
    asked = fields.pop("model", None)  # the model the supervisor asked for; `model` in the report is the one the log says answered
    row = {**fields, **found}  # one dict: a key present in both can never be passed twice to emit()
    if asked:
        row["model_requested"] = asked
    events.emit("ai.usage", agent=agent, **row)
    return found
