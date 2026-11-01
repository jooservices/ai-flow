"""Project and epic progress reports built from the registry history."""
from __future__ import annotations

import csv
import html
import io
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median
from typing import Any


class ReportNotFound(LookupError):
    """The requested epic is not in the requested project."""


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()!|<>#~])")
_AC_LINE = re.compile(r"^\s*(?:[-*]\s*)?(AC\s*\d+)\s*[:.)-]\s*(.*?)\s*$", re.IGNORECASE)
_SECTION_HEADING = re.compile(r"^\s*(?:#{1,6}\s+.+|(?:Business requirement|Technical notes|Definition of done|Test cases)\s*:).*$",
                              re.IGNORECASE)


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


def _acceptance_criteria(description: str) -> list[dict[str, str]]:
    """Read AC lines only from the ticket's Acceptance criteria section."""
    lines = str(description or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        heading = re.sub(r"^\s*#{1,6}\s*", "", line).strip().rstrip(":").strip()
        if heading.casefold() == "acceptance criteria":
            start = i + 1
            break
    if start is None:
        return []
    criteria = []
    for line in lines[start:]:
        if _SECTION_HEADING.match(line):
            break
        match = _AC_LINE.match(line)
        if match and match.group(2):
            criteria.append({"id": re.sub(r"\s+", "", match.group(1)).upper(), "text": match.group(2).strip()})
    return criteria


def _verify_verdict(run: dict | None) -> str | None:
    if not run:
        return None
    outcome = str(run.get("outcome") or "").lower()
    if outcome in {"merged", "human"}:
        return "PASS"
    if outcome == "rework":
        return "FAIL"
    verdict = str((run.get("details") or {}).get("verdict") or "").upper()
    if verdict in {"PASS", "FAIL", "BLOCKED"}:
        if verdict == "PASS" and outcome in {"failed", "blocked", "timeout", "lost", "quota", "released"}:
            return None
        return verdict
    return None


def _requirement_coverage(tickets: list[dict], latest_verify: dict[str, dict], rework_by_ticket: dict[str, int],
                          usage_by_ticket: dict[str, dict], lead_by_ticket: dict[str, float],
                          epic_key: str | None) -> dict[str, Any]:
    gaps: list[dict[str, str]] = []
    rows = []
    if epic_key and not tickets:
        gaps.append({"ticket": epic_key, "reason": "epic_has_no_child_tickets"})
    for ticket in tickets:
        criteria = _acceptance_criteria(ticket.get("description", ""))
        verify = latest_verify.get(ticket["key"])
        verdict = _verify_verdict(verify)
        ac_status = "verified" if verdict == "PASS" else ("not_verified" if not verify else "not_confirmed")
        for ac in criteria:
            ac["status"] = ac_status
        if not criteria:
            gaps.append({"ticket": ticket["key"], "reason": "ticket_has_no_acceptance_criteria"})
        elif ticket["status"] == "Done" and ac_status != "verified":
            gaps.append({"ticket": ticket["key"], "reason": "done_without_verified_acceptance_criteria"})
        usage = usage_by_ticket.get(ticket["key"], {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0, "cost_usd": 0.0})
        rows.append({
            "key": ticket["key"], "type": ticket["type"], "summary": ticket["summary"],
            "parent_key": ticket["parent_key"], "status": ticket["status"],
            "acceptance_criteria": criteria, "verified_ac": sum(ac["status"] == "verified" for ac in criteria),
            "ac_total": len(criteria), "verify": {"verdict": verdict, "outcome": verify.get("outcome") if verify else None,
                                                    "ended_at": _iso(verify.get("ended_at")) if verify else None},
            "rework_rounds": rework_by_ticket.get(ticket["key"], 0),
            "lead_time_seconds": lead_by_ticket.get(ticket["key"]), "usage": usage,
        })
    return {"insufficient_tasks": bool(gaps), "gaps": gaps, "tickets": rows}


def load_project_tickets(ctx, project_key: str) -> list[dict]:
    """Read every ticket in the project, ordered by key."""
    return ctx.db.all(
        "SELECT key, project, status, type, summary, description, parent_key, labels, jira_created, jira_updated, done_at"
        " FROM tickets WHERE project=%s ORDER BY key", (project_key,))


def build_project_close_suggestion(all_tickets: list[dict]) -> dict[str, Any]:
    """Suggest closure only when the project has epics and every epic is Done."""
    project_epics = [ticket for ticket in all_tickets if ticket["type"] == "Epic"]
    done_epics = sum(ticket["status"] == "Done" for ticket in project_epics)
    return {
        "suggested": bool(project_epics) and done_epics == len(project_epics),
        "epics_done": done_epics,
        "epics_total": len(project_epics),
    }


def require_epic(by_key: dict[str, dict], project_key: str, epic_key: str) -> None:
    """Reject a key that is missing or is not an Epic in this project."""
    epic = by_key.get(epic_key)
    if not epic or epic["type"] != "Epic":
        raise ReportNotFound(f"unknown epic {epic_key} in project {project_key}")


def tickets_in_scope(all_tickets: list[dict], epic_key: str | None, by_key: dict[str, dict]) -> list[dict]:
    """Non-epic tickets, limited to descendants of the epic when one was requested."""
    return [ticket for ticket in all_tickets if ticket["type"] != "Epic" and
            (not epic_key or _under_epic(ticket, epic_key, by_key))]


def completion_times(completed: list[dict]) -> dict[str, Any]:
    """Completion instant for each Done ticket: done_at, otherwise jira_updated."""
    return {ticket["key"]: ticket["done_at"] or ticket["jira_updated"] for ticket in completed}


def load_runs(ctx, ticket_keys: list[str]) -> list[dict]:
    """Recorded runs for the scoped tickets, oldest start first."""
    return ctx.db.all(
        "SELECT ticket_key, step, account_id, provider, model, status, outcome, started_at, ended_at,"
        " tokens_in, tokens_out, cost_usd FROM runs WHERE ticket_key = ANY(%s) ORDER BY started_at",
        (ticket_keys,))


def summarize_runs(runs: list[dict]) -> dict[str, Any]:
    """Sum outcomes, token usage and recorded AI seconds for each run."""
    run_outcomes: Counter[str] = Counter()
    account_totals: dict[str, dict] = {}
    model_totals: dict[tuple[str, str], dict] = {}
    total_in = total_out = 0
    total_cost = 0.0
    ai_seconds_by_ticket: dict[str, float] = defaultdict(float)
    usage_by_ticket: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "tokens_in": 0, "tokens_out": 0, "tokens_total": 0, "cost_usd": 0.0,
    })
    for run in runs:
        run_outcomes[run["outcome"] or run["status"] or "unknown"] += 1
        tin, tout, cost = int(run["tokens_in"]), int(run["tokens_out"]), float(run["cost_usd"])
        total_in += tin
        total_out += tout
        total_cost += cost
        ticket_usage = usage_by_ticket[run["ticket_key"]]
        ticket_usage["tokens_in"] += tin
        ticket_usage["tokens_out"] += tout
        ticket_usage["tokens_total"] += tin + tout
        ticket_usage["cost_usd"] = round(ticket_usage["cost_usd"] + cost, 6)
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
    return {
        "run_outcomes": run_outcomes,
        "account_totals": account_totals,
        "model_totals": model_totals,
        "total_in": total_in,
        "total_out": total_out,
        "total_cost": total_cost,
        "ai_seconds_by_ticket": ai_seconds_by_ticket,
        "usage_by_ticket": usage_by_ticket,
    }


def completion_samples(completed: list[dict], done_at: dict[str, Any],
                       ai_seconds_by_ticket: dict[str, float]) -> tuple[list[float], list[dict], list[float]]:
    """Lead-time samples, slowest rows and AI-time samples for completed tickets."""
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
    return lead_values, slowest, ai_values


def lead_time_by_ticket(completed: list[dict], done_at: dict[str, Any]) -> dict[str, float]:
    """Rounded lead time for each completed ticket that has both timestamps."""
    lead_by_ticket = {}
    for ticket in completed:
        start, end = ticket["jira_created"], done_at[ticket["key"]]
        if start and end:
            lead_by_ticket[ticket["key"]] = round(max(0.0, (end - start).total_seconds()), 1)
    return lead_by_ticket


def load_rework(ctx, ticket_keys: list[str]) -> tuple[list[dict], dict[str, int]]:
    """Verify runs whose outcome is rework, as rows and as a count per ticket."""
    rework = ctx.db.all(
        "SELECT ticket_key, count(*) AS rounds FROM runs WHERE ticket_key = ANY(%s)"
        " AND step='verify' AND outcome='rework' GROUP BY ticket_key ORDER BY ticket_key", (ticket_keys,))
    rework_tickets = [{"key": row["ticket_key"], "rounds": int(row["rounds"])} for row in rework]
    return rework_tickets, {row["key"]: row["rounds"] for row in rework_tickets}


def load_latest_verify(ctx, ticket_keys: list[str]) -> dict[str, dict]:
    """Latest finished verify run for each scoped ticket."""
    return {row["ticket_key"]: row for row in ctx.db.all(
        "SELECT DISTINCT ON (ticket_key) ticket_key, status, outcome, details, ended_at FROM runs"
        " WHERE ticket_key = ANY(%s) AND step='verify' AND status='finished'"
        " ORDER BY ticket_key, started_at DESC, ended_at DESC", (ticket_keys,))}


def load_owner_needed(ctx, ticket_keys: list[str], tickets: list[dict], by_key: dict[str, dict]) -> list[dict]:
    """Tickets moved to Needs owner, still in that status, or carrying the needs-owner label."""
    owner_rows = ctx.db.all(
        "SELECT ticket, count(*) AS requests FROM write_audit WHERE ticket = ANY(%s) AND accepted=true"
        " AND action='transition' AND (result='Needs owner' OR payload->>'to'='Needs owner')"
        " GROUP BY ticket ORDER BY ticket", (ticket_keys,))
    owner_keys = {row["ticket"] for row in owner_rows}
    owner_keys.update(ticket["key"] for ticket in tickets
                      if ticket["status"] == "Needs owner" or "needs-owner" in ticket["labels"])
    return [{"key": key, "summary": by_key[key]["summary"]} for key in sorted(owner_keys)]


def load_incidents(ctx, ticket_keys: list[str]) -> list[dict]:
    """Incidents for the scoped tickets. opened_at is rendered on the row the query returned."""
    incidents = ctx.db.all(
        "SELECT id, opened_at, severity, kind, subject AS ticket, title, detail FROM incidents"
        " WHERE subject = ANY(%s) ORDER BY opened_at", (ticket_keys,))
    for item in incidents:
        item["opened_at"] = _iso(item["opened_at"])
    return incidents


def build_period(tickets: list[dict], completed: list[dict], done_at: dict[str, Any]) -> dict[str, Any]:
    """Earliest creation through latest completion, in days."""
    starts = [ticket["jira_created"] for ticket in tickets if ticket["jira_created"]]
    ends = [done_at[ticket["key"]] for ticket in completed if done_at[ticket["key"]]]
    period_start = min(starts) if starts else None
    period_end = max(ends) if ends else None
    period_days = round((period_end - period_start).total_seconds() / 86400, 2) if period_start and period_end else None
    return {"start": _iso(period_start), "end": _iso(period_end), "days": period_days}


def build_ticket_summary(tickets: list[dict], completed: list[dict]) -> dict[str, Any]:
    """Ticket counts for the scope, including done totals by type."""
    return {
        "total": len(tickets), "done": len(completed),
        "in_progress": sum(ticket["status"] in {"In Progress", "In Verify", "Human review"} for ticket in tickets),
        "needs_owner": sum(ticket["status"] == "Needs owner" for ticket in tickets),
        "done_by_type": dict(sorted(Counter(ticket["type"] for ticket in completed).items())),
    }


def build_lead_time(lead_values: list[float]) -> dict[str, float | int | None]:
    """Median and p90 lead time for completed tickets that have both timestamps."""
    return _duration_stats(lead_values)


def build_ai_time(ai_values: list[float], ai_seconds_by_ticket: dict[str, float]) -> dict[str, Any]:
    """Median and p90 AI time on completed tickets, plus every recorded AI second."""
    return {**_duration_stats(ai_values), "total_seconds": round(sum(ai_seconds_by_ticket.values()), 1)}


def build_runs_by_outcome(run_outcomes: Counter[str]) -> dict[str, int]:
    """Run counts keyed by outcome, otherwise status, otherwise unknown."""
    return dict(sorted(run_outcomes.items()))


def build_rework_rounds(rework_tickets: list[dict]) -> dict[str, Any]:
    """Total verify-rework rounds and the per-ticket counts."""
    return {"count": sum(row["rounds"] for row in rework_tickets), "tickets": rework_tickets}


def build_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """Token and cost totals, with rows sorted by account and by provider and model."""
    total_in, total_out = usage["total_in"], usage["total_out"]
    return {
        "tokens_in": total_in, "tokens_out": total_out, "tokens_total": total_in + total_out,
        "cost_usd": round(usage["total_cost"], 6),
        "by_account": sorted(usage["account_totals"].values(), key=lambda row: row["account"]),
        "by_model": sorted(usage["model_totals"].values(), key=lambda row: (row["provider"], row["model"])),
    }


def build_incidents_opened(incidents: list[dict]) -> dict[str, Any]:
    """Opened incidents for the scoped tickets."""
    return {"count": len(incidents), "items": incidents}


def build_owner_needed(owner_needed: list[dict]) -> dict[str, Any]:
    """Tickets that asked for the owner or still carry that status or label."""
    return {"count": len(owner_needed), "tickets": owner_needed}


def build_slowest_tickets(slowest: list[dict]) -> list[dict]:
    """The five completed tickets with the longest lead time."""
    slowest.sort(key=lambda row: (-row["lead_time_seconds"], row["key"]))
    return slowest[:5]


def build_lessons(rework_tickets: list[dict], owner_needed: list[dict], incidents: list[dict]) -> list[str]:
    """Lessons from rework, owner input and incidents on a scope that has tickets."""
    lessons = []
    if rework_tickets:
        lessons.append(f"Clarify acceptance criteria for the {len(rework_tickets)} ticket(s) that needed"
                       f" {sum(row['rounds'] for row in rework_tickets)} rework round(s).")
    if owner_needed:
        lessons.append(f"Bring owner decisions into intake earlier for the {len(owner_needed)} ticket(s) that needed"
                       " owner input.")
    if incidents:
        lessons.append(f"Review the {len(incidents)} recorded incident(s) and their causes before repeating this"
                       " project setup.")
    if not lessons:
        lessons.append("The recorded history shows no rework, owner escalation, or incident; "
                       "keep the successful workflow.")
    return lessons


def empty_report(project_key: str, epic_key: str | None, suggestion: dict[str, Any],
                 tickets: list[dict]) -> dict[str, Any]:
    """Report returned when the scope has no non-epic tickets. Key order differs from the filled report."""
    return {
        "project": project_key, "epic": epic_key,
        "period": build_period(tickets, [], {}),
        "tickets": build_ticket_summary(tickets, []),
        "lead_time": build_lead_time([]),
        "ai_time": build_ai_time([], {}),
        "runs_by_outcome": build_runs_by_outcome(Counter()),
        "rework_rounds": build_rework_rounds([]),
        "usage": build_usage({
            "total_in": 0, "total_out": 0, "total_cost": 0.0, "account_totals": {}, "model_totals": {},
        }),
        "incidents_opened": build_incidents_opened([]),
        "owner_needed": build_owner_needed([]),
        "slowest_tickets": build_slowest_tickets([]),
        "requirement_coverage": _requirement_coverage(tickets, {}, {}, {}, {}, epic_key),
        "project_close_suggestion": suggestion,
        # This sentence belongs to the empty scope. A filled scope with no findings uses build_lessons.
        "lessons": ["No lessons can be inferred from the recorded project history yet."],
    }


def filled_report(ctx, project_key: str, epic_key: str | None, by_key: dict[str, dict],
                  suggestion: dict[str, Any], tickets: list[dict]) -> dict[str, Any]:
    """Assemble the report when the scope contains at least one non-epic ticket."""
    ticket_keys = [ticket["key"] for ticket in tickets]
    completed = [ticket for ticket in tickets if ticket["status"] == "Done"]
    done_at = completion_times(completed)
    runs = load_runs(ctx, ticket_keys)
    usage = summarize_runs(runs)
    lead_values, slowest, ai_values = completion_samples(completed, done_at, usage["ai_seconds_by_ticket"])
    rework_tickets, rework_by_ticket = load_rework(ctx, ticket_keys)
    latest_verify = load_latest_verify(ctx, ticket_keys)
    lead_by_ticket = lead_time_by_ticket(completed, done_at)
    coverage = _requirement_coverage(
        tickets, latest_verify, rework_by_ticket, usage["usage_by_ticket"], lead_by_ticket, epic_key)
    owner_needed = load_owner_needed(ctx, ticket_keys, tickets, by_key)
    incidents = load_incidents(ctx, ticket_keys)
    report = {
        "project": project_key, "epic": epic_key,
        "project_close_suggestion": suggestion,
        "period": build_period(tickets, completed, done_at),
        "tickets": build_ticket_summary(tickets, completed),
        "lead_time": build_lead_time(lead_values),
        "ai_time": build_ai_time(ai_values, usage["ai_seconds_by_ticket"]),
        "runs_by_outcome": build_runs_by_outcome(usage["run_outcomes"]),
        "rework_rounds": build_rework_rounds(rework_tickets),
        "usage": build_usage(usage),
        "incidents_opened": build_incidents_opened(incidents),
        "owner_needed": build_owner_needed(owner_needed),
        "slowest_tickets": build_slowest_tickets(slowest),
        "requirement_coverage": coverage,
    }
    report["lessons"] = build_lessons(rework_tickets, owner_needed, incidents)
    return report


def build_report(ctx, project_key: str, epic_key: str | None = None) -> dict[str, Any]:
    """Summarize completed tickets and all recorded run history for a project or epic."""
    all_tickets = load_project_tickets(ctx, project_key)
    by_key = {ticket["key"]: ticket for ticket in all_tickets}
    suggestion = build_project_close_suggestion(all_tickets)
    if epic_key:
        require_epic(by_key, project_key, epic_key)
    tickets = tickets_in_scope(all_tickets, epic_key, by_key)
    if not tickets:
        return empty_report(project_key, epic_key, suggestion, tickets)
    return filled_report(ctx, project_key, epic_key, by_key, suggestion, tickets)


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


def _coverage_markdown(report: dict[str, Any]) -> list[str]:
    """The requirement-coverage section of the Markdown report."""
    out: list[str] = []
    out += ["", "## Requirement coverage", "",
            ("**Insufficient tasks found.**" if report["requirement_coverage"]["insufficient_tasks"]
             else "No task coverage gaps found."), ""]
    out += ["| Ticket | Parent | Status | Acceptance criteria |", "| --- | --- | --- | --- |"]
    for ticket in report["requirement_coverage"]["tickets"]:
        ac_text = "<br>".join(f"{_md_text(ac['id'])} [{ac['status']}]: {_md_text(ac['text'])}"
                              for ac in ticket["acceptance_criteria"]) or "No AC lines"
        out.append(f"| {_md_text(ticket['key'])} · {_md_text(ticket['summary'])} | "
                   f"{_md_text(ticket['parent_key'] or '—')} | {_md_text(ticket['status'])} | {ac_text} |")
    if not report["requirement_coverage"]["tickets"]:
        out.append("| — | — | — | No child tickets |")
    if report["requirement_coverage"]["gaps"]:
        out += ["", "Gaps:"]
        out.extend(f"- {_md_text(gap['ticket'])}: {_md_text(gap['reason'])}"
                   for gap in report["requirement_coverage"]["gaps"])
    return out


def render_markdown(report: dict[str, Any]) -> str:
    """Render a portable Markdown progress and requirement-coverage report."""
    title = _md_text(report["epic"] or report["project"])
    period = report["period"]
    period_text = f"{period['start'] or 'unknown'} to {period['end'] or 'in progress'}"
    if period["days"] is not None:
        period_text += f" ({period['days']:.2f} days)"
    tickets = report["tickets"]
    out = [f"# Progress report: {title}", "", f"**Period:** {period_text}", "",
           "## Progress", "", f"- Done: {tickets['done']} of {tickets['total']}",
           f"- In progress (In Progress, In Verify, Human review): {tickets['in_progress']}",
           f"- Needs owner: {tickets['needs_owner']}", f"- Rework rounds: {report['rework_rounds']['count']}",
           f"- Lead time: {_duration(report['lead_time'])}",
           f"- Tokens: {report['usage']['tokens_total']}", f"- Estimated cost: ${report['usage']['cost_usd']:.6f} USD", ""]
    suggestion = report["project_close_suggestion"]
    if suggestion["suggested"]:
        out += ["", "## Project closure", "",
                f"All {suggestion['epics_done']} epics are Done. AI Flow suggests considering project closure.", ""]
    else:
        out += ["", "## Project closure", "",
                f"Project closure is not suggested: {suggestion['epics_done']} of {suggestion['epics_total']} epics are Done.", ""]
    out += ["## Tickets", "", f"Done: {tickets['done']} of {tickets['total']}", "",
            "| Type | Done |", "| --- | ---: |"]
    out.extend(f"| {_md_text(kind)} | {count} |" for kind, count in tickets["done_by_type"].items())
    if not tickets["done_by_type"]:
        out.append("| — | 0 |")
    out += _coverage_markdown(report)
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


def render_csv(report: dict[str, Any]) -> str:
    """Render one row per ticket; AC text and its verify status stay together in one quoted cell."""
    def safe_cell(value: object) -> str:
        text = str(value if value is not None else "")
        if text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(("epic", "ticket", "parent", "summary", "status", "done", "total", "in_progress",
                     "needs_owner", "rework_rounds", "lead_time_median_seconds", "lead_time_p90_seconds",
                     "tokens", "estimated_cost_usd", "acceptance_criteria", "coverage_gap"))
    coverage = report["requirement_coverage"]
    gap_by_ticket: dict[str, list[str]] = defaultdict(list)
    for gap in coverage["gaps"]:
        gap_by_ticket[gap["ticket"]].append(gap["reason"])
    tickets = report["tickets"]
    for ticket in coverage["tickets"]:
        ac_text = safe_cell("\n".join(f"{ac['id']} [{ac['status']}]: {ac['text']}"
                                      for ac in ticket["acceptance_criteria"]))
        writer.writerow((safe_cell(report["epic"] or ""), safe_cell(ticket["key"]),
                         safe_cell(ticket["parent_key"] or ""), safe_cell(ticket["summary"]),
                         safe_cell(ticket["status"]), tickets["done"], tickets["total"], tickets["in_progress"],
                         tickets["needs_owner"], report["rework_rounds"]["count"],
                         report["lead_time"]["median_seconds"] or "", report["lead_time"]["p90_seconds"] or "",
                         ticket["usage"]["tokens_total"], f"{ticket['usage']['cost_usd']:.6f}", ac_text,
                         safe_cell("; ".join(gap_by_ticket[ticket["key"]]))))
    if not coverage["tickets"] and coverage["gaps"]:
        writer.writerow((report["epic"] or "", report["epic"] or "", "", "", "", tickets["done"], tickets["total"],
                         tickets["in_progress"], tickets["needs_owner"], report["rework_rounds"]["count"],
                         report["lead_time"]["median_seconds"] or "", report["lead_time"]["p90_seconds"] or "",
                         report["usage"]["tokens_total"], f"{report['usage']['cost_usd']:.6f}", "",
                         "; ".join(gap["reason"] for gap in coverage["gaps"])))
    return stream.getvalue()


def render_html(report: dict[str, Any]) -> str:
    """Render a standalone report with print styles and escaped ticket content."""
    title = html.escape(report["epic"] or report["project"])
    tickets = report["tickets"]
    coverage = report["requirement_coverage"]
    gap_by_ticket: dict[str, list[str]] = defaultdict(list)
    for gap in coverage["gaps"]:
        gap_by_ticket[gap["ticket"]].append(gap["reason"])
    rows = []
    for ticket in coverage["tickets"]:
        ac_rows = "".join(
            f"<li><code>{html.escape(ac['id'])}</code> <span class=\"ac-{ac['status']}\">"
            f"{html.escape(ac['status'].replace('_', ' ').title())}</span> — {html.escape(ac['text'])}</li>"
            for ac in ticket["acceptance_criteria"]
        ) or "<li>No acceptance criteria lines</li>"
        gaps = ", ".join(gap_by_ticket[ticket["key"]]) or "—"
        rows.append(f"<tr><td><strong>{html.escape(ticket['key'])}</strong><br>{html.escape(ticket['summary'])}</td>"
                    f"<td>{html.escape(ticket['parent_key'] or '—')}</td><td>{html.escape(ticket['status'])}</td>"
                    f"<td><ul>{ac_rows}</ul></td><td>{ticket['rework_rounds']}</td>"
                    f"<td>{ticket['usage']['tokens_total']}</td><td>${ticket['usage']['cost_usd']:.6f}</td>"
                    f"<td>{html.escape(gaps)}</td></tr>")
    if not rows:
        rows.append('<tr><td colspan="8">No child tickets.</td></tr>')
    state = "Insufficient tasks found" if coverage["insufficient_tasks"] else "No task coverage gaps found"
    suggestion = report["project_close_suggestion"]
    project_closure = (f"All {suggestion['epics_done']} epics are Done. AI Flow suggests considering project closure."
                       if suggestion["suggested"] else
                       f"Project closure is not suggested: {suggestion['epics_done']} of "
                       f"{suggestion['epics_total']} epics are Done.")
    lead = _duration(report["lead_time"])
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Progress report: """ + title + """</title>
<style>
body{font:14px/1.5 system-ui,sans-serif;color:#172033;margin:28px}h1,h2{margin:.2em 0 .6em}header,.summary{margin-bottom:20px}.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.summary div{border:1px solid #ccd3df;border-radius:6px;padding:10px}.summary strong{display:block;font-size:20px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{border:1px solid #ccd3df;padding:8px;text-align:left;vertical-align:top}th{background:#f0f3f8}ul{margin:0;padding-left:18px}.ac-verified{color:#08643c}.ac-not-confirmed,.ac-not-verified{color:#9a4010}code{font-family:ui-monospace,monospace}.gap{color:#a21d1d;font-weight:600}
@media print{body{margin:0;color:#000;font-size:10pt}.summary div,th,td{border-color:#777}th{background:#eee!important;print-color-adjust:exact}.summary{grid-template-columns:repeat(4,1fr)}tr{break-inside:avoid}h1,h2{break-after:avoid}}
</style></head><body>
<header><h1>Progress report: """ + title + """</h1><div>Project: """ + html.escape(report["project"]) + """ · Period: """ + html.escape(
        (report["period"]["start"] or "unknown") + " to " + (report["period"]["end"] or "in progress")) + """</div></header>
<div class="summary">
<div><strong>""" + str(tickets["done"]) + " of " + str(tickets["total"]) + """</strong>Done / total</div>
<div><strong>""" + str(tickets["in_progress"]) + """</strong>In progress</div>
<div><strong>""" + str(tickets["needs_owner"]) + """</strong>Needs owner</div>
<div><strong>""" + str(report["rework_rounds"]["count"]) + """</strong>Rework rounds</div>
<div><strong>""" + html.escape(lead) + """</strong>Lead time</div>
<div><strong>""" + str(report["usage"]["tokens_total"]) + """</strong>Tokens</div>
<div><strong>$""" + f"{report['usage']['cost_usd']:.6f}" + """</strong>Estimated cost (USD)</div>
</div>
<h2>Project closure</h2><p>""" + html.escape(project_closure) + """</p>
<h2>Requirement coverage · <span class=""" + ("gap" if coverage["insufficient_tasks"] else "") + """>""" + state + """</span></h2>
<table><thead><tr><th>Ticket</th><th>Parent</th><th>Status</th><th>Acceptance criteria · verify result</th><th>Rework</th><th>Tokens</th><th>Cost (USD)</th><th>Gap</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table>
</body></html>"""
