"""Project and epic closing reports built from the registry history."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any


class ReportNotFound(LookupError):
    """The requested epic is not in the requested project."""


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()!|<>#~])")


def _md_text(value: object) -> str:
    """Keep untrusted report text inline and inert when rendered as Markdown."""
    text = re.sub(r"[\r\n]+", " ", str(value))
    return _MD_SPECIAL.sub(r"\\\1", text)


def _under_epic(ticket: dict, epic_key: str, by_key: dict[str, dict]) -> bool:
    parent = ticket.get("parent_key")
    seen: set[str] = set()
    while parent and parent not in seen:
        if parent == epic_key:
            return True
        seen.add(parent)
        row = by_key.get(parent)
        parent = row.get("parent_key") if row else None
    return False


def _duration_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"sample_size": 0, "median_seconds": None, "p90_seconds": None}
    ordered = sorted(values)
    return {
        "sample_size": len(ordered),
        "median_seconds": round(float(median(ordered)), 1),
        "p90_seconds": round(ordered[math.ceil(0.9 * len(ordered)) - 1], 1),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def build_report(ctx, project_key: str, epic_key: str | None = None) -> dict[str, Any]:
    """Summarize completed tickets and all recorded run history for a project or epic."""
    all_tickets = ctx.db.all(
        "SELECT key, project, status, type, summary, parent_key, labels, jira_created, jira_updated, done_at"
        " FROM tickets WHERE project=%s ORDER BY key", (project_key,))
    by_key = {t["key"]: t for t in all_tickets}
    if epic_key:
        epic = by_key.get(epic_key)
        if not epic or epic["type"] != "Epic":
            raise ReportNotFound(f"unknown epic {epic_key} in project {project_key}")
    tickets = [t for t in all_tickets if t["type"] != "Epic" and
               (not epic_key or _under_epic(t, epic_key, by_key))]
    ticket_keys = [t["key"] for t in tickets]
    if not ticket_keys:
        return {
            "project": project_key, "epic": epic_key,
            "period": {"start": None, "end": None, "days": None},
            "tickets": {"total": 0, "done": 0, "done_by_type": {}},
            "lead_time": _duration_stats([]), "ai_time": {**_duration_stats([]), "total_seconds": 0},
            "runs_by_outcome": {}, "rework_rounds": {"count": 0, "tickets": []},
            "usage": {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0, "cost_usd": 0.0,
                      "by_account": [], "by_model": []},
            "incidents_opened": {"count": 0, "items": []}, "owner_needed": {"count": 0, "tickets": []},
            "slowest_tickets": [], "lessons": ["No lessons can be inferred from the recorded project history yet."],
        }

    completed = [t for t in tickets if t["status"] == "Done"]
    done_at = {t["key"]: t["done_at"] or t["jira_updated"] for t in completed}
    done_by_type = dict(sorted(Counter(t["type"] for t in completed).items()))
    starts = [t["jira_created"] for t in tickets if t["jira_created"]]
    ends = [done_at[t["key"]] for t in completed if done_at[t["key"]]]
    period_start = min(starts) if starts else None
    period_end = max(ends) if ends else None
    period_days = round((period_end - period_start).total_seconds() / 86400, 2) if period_start and period_end else None

    runs = ctx.db.all(
        "SELECT ticket_key, step, account_id, provider, model, status, outcome, started_at, ended_at,"
        " tokens_in, tokens_out, cost_usd FROM runs WHERE ticket_key = ANY(%s) ORDER BY started_at",
        (ticket_keys,))
    run_outcomes: Counter[str] = Counter()
    account_totals: dict[str, dict] = {}
    model_totals: dict[tuple[str, str], dict] = {}
    total_in = total_out = 0
    total_cost = 0.0
    ai_seconds_by_ticket: dict[str, float] = defaultdict(float)
    for run in runs:
        run_outcomes[run["outcome"] or run["status"] or "unknown"] += 1
        tin, tout, cost = int(run["tokens_in"]), int(run["tokens_out"]), float(run["cost_usd"])
        total_in += tin
        total_out += tout
        total_cost += cost
        for key, label in ((run["account_id"], str(run["account_id"])),
                           ((run["provider"], run["model"]), f"{run['provider']}/{run['model']}")):
            groups = account_totals if isinstance(key, str) else model_totals
            row = groups.setdefault(key, {"account": label} if isinstance(key, str) else
                                     {"provider": key[0], "model": key[1]})
            row["runs"] = row.get("runs", 0) + 1
            row["tokens_in"] = row.get("tokens_in", 0) + tin
            row["tokens_out"] = row.get("tokens_out", 0) + tout
            row["tokens_total"] = row["tokens_in"] + row["tokens_out"]
            row["cost_usd"] = round(row.get("cost_usd", 0.0) + cost, 6)
        if run["ended_at"]:
            seconds = max(0.0, (run["ended_at"] - run["started_at"]).total_seconds())
            ai_seconds_by_ticket[run["ticket_key"]] += seconds

    lead_values = []
    slowest = []
    ai_values = []
    for ticket in completed:
        start, end = ticket["jira_created"], done_at[ticket["key"]]
        if start and end:
            seconds = max(0.0, (end - start).total_seconds())
            lead_values.append(seconds)
            slowest.append({"key": ticket["key"], "summary": ticket["summary"], "type": ticket["type"],
                            "lead_time_seconds": round(seconds, 1), "created_at": _iso(start),
                            "done_at": _iso(end)})
        if ticket["key"] in ai_seconds_by_ticket:
            ai_values.append(ai_seconds_by_ticket[ticket["key"]])

    rework = ctx.db.all(
        "SELECT ticket_key, count(*) AS rounds FROM runs WHERE ticket_key = ANY(%s)"
        " AND step='verify' AND outcome='rework' GROUP BY ticket_key ORDER BY ticket_key", (ticket_keys,))
    rework_tickets = [{"key": r["ticket_key"], "rounds": int(r["rounds"])} for r in rework]
    owner_rows = ctx.db.all(
        "SELECT ticket, count(*) AS requests FROM write_audit WHERE ticket = ANY(%s) AND accepted=true"
        " AND action='transition' AND (result='Needs owner' OR payload->>'to'='Needs owner')"
        " GROUP BY ticket ORDER BY ticket", (ticket_keys,))
    owner_keys = {r["ticket"] for r in owner_rows}
    owner_keys.update(t["key"] for t in tickets if t["status"] == "Needs owner" or "needs-owner" in t["labels"])
    owner_needed = [{"key": key, "summary": by_key[key]["summary"]} for key in sorted(owner_keys)]

    incidents = ctx.db.all(
        "SELECT id, opened_at, severity, kind, subject AS ticket, title, detail FROM incidents"
        " WHERE subject = ANY(%s) ORDER BY opened_at", (ticket_keys,))
    for item in incidents:
        item["opened_at"] = _iso(item["opened_at"])
    ai_total = sum(ai_seconds_by_ticket.values())
    slowest.sort(key=lambda row: (-row["lead_time_seconds"], row["key"]))
    report = {
        "project": project_key, "epic": epic_key,
        "period": {"start": _iso(period_start), "end": _iso(period_end), "days": period_days},
        "tickets": {"total": len(tickets), "done": len(completed), "done_by_type": done_by_type},
        "lead_time": _duration_stats(lead_values),
        "ai_time": {**_duration_stats(ai_values), "total_seconds": round(ai_total, 1)},
        "runs_by_outcome": dict(sorted(run_outcomes.items())),
        "rework_rounds": {"count": sum(r["rounds"] for r in rework_tickets), "tickets": rework_tickets},
        "usage": {"tokens_in": total_in, "tokens_out": total_out, "tokens_total": total_in + total_out,
                  "cost_usd": round(total_cost, 6),
                  "by_account": sorted(account_totals.values(), key=lambda x: x["account"]),
                  "by_model": sorted(model_totals.values(), key=lambda x: (x["provider"], x["model"]))},
        "incidents_opened": {"count": len(incidents), "items": incidents},
        "owner_needed": {"count": len(owner_needed), "tickets": owner_needed},
        "slowest_tickets": slowest[:5],
    }
    lessons = []
    if rework_tickets:
        lessons.append(f"Clarify acceptance criteria for the {len(rework_tickets)} ticket(s) that needed"
                       f" {sum(r['rounds'] for r in rework_tickets)} rework round(s).")
    if owner_needed:
        lessons.append(f"Bring owner decisions into intake earlier for the {len(owner_needed)} ticket(s) that needed"
                       " owner input.")
    if incidents:
        lessons.append(f"Review the {len(incidents)} recorded incident(s) and their causes before repeating this"
                       " project setup.")
    if not lessons:
        lessons.append("The recorded history shows no rework, owner escalation, or incident; keep the successful workflow.")
    report["lessons"] = lessons
    return report


def _duration_value(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


def _duration(stats: dict[str, Any]) -> str:
    if not stats["sample_size"]:
        return "n/a"
    return (f"median {_duration_value(stats['median_seconds'])}, "
            f"p90 {_duration_value(stats['p90_seconds'])} (n={stats['sample_size']})")


def render_markdown(report: dict[str, Any]) -> str:
    """Render a report as portable Markdown for the Delivery drawer and copy action."""
    title = _md_text(report["epic"] or report["project"])
    period = report["period"]
    period_text = f"{period['start'] or 'unknown'} to {period['end'] or 'in progress'}"
    if period["days"] is not None:
        period_text += f" ({period['days']:.2f} days)"
    out = [f"# Project report: {title}", "", f"**Period:** {period_text}", "",
           "## Tickets", "", f"Done: {report['tickets']['done']} of {report['tickets']['total']}", "",
           "| Type | Done |", "| --- | ---: |"]
    out.extend(f"| {_md_text(kind)} | {count} |" for kind, count in report["tickets"]["done_by_type"].items())
    if not report["tickets"]["done_by_type"]:
        out.append("| — | 0 |")
    out += ["", "## Time", "", f"- Lead time: {_duration(report['lead_time'])}",
            f"- AI time per completed ticket: {_duration(report['ai_time'])}",
            f"- Total recorded AI time: {_duration_value(report['ai_time']['total_seconds'])}", "",
            "## Runs by outcome", "", "| Outcome | Runs |", "| --- | ---: |"]
    out.extend(f"| {_md_text(outcome)} | {count} |" for outcome, count in report["runs_by_outcome"].items())
    if not report["runs_by_outcome"]:
        out.append("| — | 0 |")
    out += ["", f"## Rework rounds ({report['rework_rounds']['count']})", ""]
    out.extend(f"- {_md_text(item['key'])}: {item['rounds']}" for item in report["rework_rounds"]["tickets"])
    if not report["rework_rounds"]["tickets"]:
        out.append("- None")
    usage = report["usage"]
    out += ["", "## Tokens and cost", "", f"- Tokens: {usage['tokens_total']} ({usage['tokens_in']} in, {usage['tokens_out']} out)",
            f"- Cost: ${usage['cost_usd']:.6f}", "", "### By account", "",
            "| Account | Runs | Tokens | Cost (USD) |", "| --- | ---: | ---: | ---: |"]
    out.extend(f"| {_md_text(r['account'])} | {r['runs']} | {r['tokens_total']} | ${r['cost_usd']:.6f} |"
                for r in usage["by_account"])
    if not usage["by_account"]:
        out.append("| — | 0 | 0 | $0.000000 |")
    out += ["", "### By model", "", "| Provider | Model | Runs | Tokens | Cost (USD) |",
            "| --- | --- | ---: | ---: | ---: |"]
    out.extend(f"| {_md_text(r['provider'] or '—')} | {_md_text(r['model'] or '—')} | {r['runs']} | {r['tokens_total']} | ${r['cost_usd']:.6f} |"
                for r in usage["by_model"])
    if not usage["by_model"]:
        out.append("| — | — | 0 | 0 | $0.000000 |")
    out += ["", f"## Incidents opened ({report['incidents_opened']['count']})", ""]
    out.extend(f"- {_md_text(r['ticket'])} · {_md_text(r['kind'])} · {_md_text(r['title'])} ({r['opened_at']})"
                for r in report["incidents_opened"]["items"])
    if not report["incidents_opened"]["items"]:
        out.append("- None")
    out += ["", f"## Tickets that needed the owner ({report['owner_needed']['count']})", ""]
    out.extend(f"- {_md_text(r['key'])} · {_md_text(r['summary'])}" for r in report["owner_needed"]["tickets"])
    if not report["owner_needed"]["tickets"]:
        out.append("- None")
    out += ["", "## Five slowest tickets", "", "| Ticket | Type | Lead time | Summary |",
            "| --- | --- | ---: | --- |"]
    out.extend(f"| {_md_text(r['key'])} | {_md_text(r['type'])} | {_duration_value(r['lead_time_seconds'])} | {_md_text(r['summary'])} |"
                for r in report["slowest_tickets"])
    if not report["slowest_tickets"]:
        out.append("| — | — | — | No completed tickets with timestamps |")
    out += ["", "## Lessons", ""]
    out.extend(f"- {_md_text(lesson)}" for lesson in report["lessons"])
    return "\n".join(out) + "\n"
