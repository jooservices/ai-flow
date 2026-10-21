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


def build_report(ctx, project_key: str, epic_key: str | None = None) -> dict[str, Any]:
    """Summarize completed tickets and all recorded run history for a project or epic."""
    all_tickets = ctx.db.all(
        "SELECT key, project, status, type, summary, description, parent_key, labels, jira_created, jira_updated, done_at"
        " FROM tickets WHERE project=%s ORDER BY key", (project_key,))
    by_key = {t["key"]: t for t in all_tickets}
    if epic_key:
        epic = by_key.get(epic_key)
        if not epic or epic["type"] != "Epic":
            raise ReportNotFound(f"unknown epic {epic_key} in project {project_key}")
    tickets = [t for t in all_tickets if t["type"] != "Epic" and
               (not epic_key or _under_epic(t, epic_key, by_key))]
    ticket_keys = [t["key"] for t in tickets]
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
    rework_by_ticket = {r["key"]: r["rounds"] for r in rework_tickets}
    latest_verify = {r["ticket_key"]: r for r in ctx.db.all(
        "SELECT DISTINCT ON (ticket_key) ticket_key, status, outcome, details, ended_at FROM runs"
        " WHERE ticket_key = ANY(%s) AND step='verify' AND status='finished'"
        " ORDER BY ticket_key, started_at DESC, ended_at DESC", (ticket_keys,))}
    lead_by_ticket = {}
    for ticket in completed:
        start, end = ticket["jira_created"], done_at[ticket["key"]]
        if start and end:
            lead_by_ticket[ticket["key"]] = round(max(0.0, (end - start).total_seconds()), 1)
    coverage = _requirement_coverage(tickets, latest_verify, rework_by_ticket, usage_by_ticket,
                                     lead_by_ticket, epic_key)
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
    return {
        "project": project_key, "epic": epic_key,
        "period": {"start": _iso(period_start), "end": _iso(period_end), "days": period_days},
        "tickets": {"total": len(tickets), "done": len(completed), "in_progress": sum(
            t["status"] in {"In Progress", "In Verify", "Human review"} for t in tickets),
            "needs_owner": sum(t["status"] == "Needs owner" for t in tickets), "done_by_type": done_by_type},
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
        "requirement_coverage": coverage,
    }


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
           f"- Tokens: {report['usage']['tokens_total']}", f"- Estimated cost: ${report['usage']['cost_usd']:.6f} USD", "",
           "## Tickets", "", f"Done: {tickets['done']} of {tickets['total']}", "",
           "| Type | Done |", "| --- | ---: |"]
    out.extend(f"| {_md_text(kind)} | {count} |" for kind, count in tickets["done_by_type"].items())
    if not tickets["done_by_type"]:
        out.append("| — | 0 |")
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
<h2>Requirement coverage · <span class=""" + ("gap" if coverage["insufficient_tasks"] else "") + """>""" + state + """</span></h2>
<table><thead><tr><th>Ticket</th><th>Parent</th><th>Status</th><th>Acceptance criteria · verify result</th><th>Rework</th><th>Tokens</th><th>Cost (USD)</th><th>Gap</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table>
</body></html>"""
