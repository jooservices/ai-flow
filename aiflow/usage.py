"""Token usage of one AI run, read from the log the run already wrote (no extra call, no extra token).

    Claude    stream-json: the last `result` event (input, output, cache read/write, thinking, cost, turns, per model)
    Codex     `codex exec --json`: `turn.completed` token events plus list-price estimates for configured models; plain logs: `tokens used N`
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
# Standard per-million-token rates (input, cached input, cache write, output) for configured Codex models.
# Each pair is short-context (<272K) and long-context pricing. Unknown models do not get a guessed cost.
CODEX_LIST_PRICES = {
    "gpt-6-luna": ((0.10, 0.01, 0.125, 0.50), (0.20, 0.02, 0.25, 0.75)),
    "gpt-5.6-terra": ((2.00, 0.20, 2.50, 12.00), (4.00, 0.40, 5.00, 18.00)),
    "gpt-5.6-luna": ((0.20, 0.02, 0.25, 1.20), (0.40, 0.04, 0.50, 1.80)),
}


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
    cost = kw.get("cost")
    row.update(cost_usd_list=round(float(cost), 6) if cost is not None else None, cost_known=cost is not None,
               cost_model=str(kw.get("cost_model") or ""), model=str(kw.get("model") or ""),
               turns=int(kw.get("turns") or 0), usage_known=True)
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


def _codex_list_cost(model: str, input_tokens: int, cache_read: int, cache_write: int, output: int,
                     long_context: bool) -> tuple[float | None, str]:
    normalized = model.lower().removeprefix("openai/")
    name = next((known for known in CODEX_LIST_PRICES if normalized == known or normalized.startswith(known + "-")), "")
    if not name:
        return None, ""
    input_rate, cache_rate, write_rate, output_rate = CODEX_LIST_PRICES[name][int(long_context)]
    uncached = max(0, input_tokens - cache_read - cache_write)
    cost = (uncached * input_rate + cache_read * cache_rate + cache_write * write_rate + output * output_rate) / 1_000_000
    return cost, name


def codex(text: str, requested_model: str = "") -> dict[str, Any] | None:
    total = dict.fromkeys(("input", "cached", "cache_write", "output", "reasoning"), 0)
    seen = False
    model = ""
    cost_total = 0.0
    has_cost = False
    priced_models: set[str] = set()
    for event in _events(text):
        model = str((event.get("usage") or {}).get("model") or event.get("model") or model)
        if event.get("type") != "turn.completed" or not isinstance(event.get("usage"), dict):
            continue
        seen = True
        use = event["usage"]
        values = {name: int(use.get(key) or 0) for name, key in (
            ("input", "input_tokens"), ("cached", "cached_input_tokens"), ("cache_write", "cache_write_input_tokens"),
            ("output", "output_tokens"), ("reasoning", "reasoning_output_tokens"))}
        for name, value in values.items():
            total[name] += value
        cost = use.get("cost_usd_list", use.get("total_cost_usd", event.get("cost_usd_list", event.get("total_cost_usd"))))
        if cost is not None:
            cost_total += float(cost)
            has_cost = True
        else:
            estimate, priced_model = _codex_list_cost(model or requested_model, values["input"], values["cached"],
                                                       values["cache_write"], values["output"], values["input"] > 272_000)
            if estimate is not None:
                cost_total += estimate
                has_cost = True
                priced_models.add(priced_model)
    if seen:
        cost_model = model or ",".join(sorted(priced_models))
        return _row(input=total["input"] - total["cached"] - total["cache_write"], output=total["output"], cache_read=total["cached"],
                    cache_write=total["cache_write"], reasoning=total["reasoning"], total=total["input"] + total["output"],
                    cost=cost_total if has_cost else None,
                    cost_model=cost_model, model=model)
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


def parse(agent: str, text: str, model: str = "") -> dict[str, Any]:
    """The usage of a run, or `{"usage_known": False}` when this AI does not report it or the log has no report (e.g. it was stopped)."""
    parser = PARSERS.get(agent)
    try:
        if agent == "Codex":
            found = codex(text, model)
        else:
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
    asked = fields.pop("model", None)  # the model the supervisor asked for; `model` in the report is the one the log says answered
    found = parse(agent, text, model=asked or "")
    row = {"run_id": fields.pop("run_id", log_file.stem), **fields, **found}  # one dict: a key present in both can never be passed twice to emit()
    if asked:
        row["model_requested"] = asked
    events.emit("ai.usage", agent=agent, **row)
    return found
