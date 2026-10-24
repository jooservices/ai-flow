"""Scale proof: performance budgets, their enforcement, the release attachment and the tickets for findings.

The scenario itself (100 projects, 50 machines, 200 simulated AI sessions, realistic ticket traffic) lives in
`scale_harness`. This module holds what is shared by the test, CI and the console:

  BUDGETS    the one place where the budgets are written down: limit, unit and the statistic they apply to.
  evaluate   compares measured values with the budgets; a budget over its limit is a failure that names the
             budget and the measured value.
  finalize   recomputes every verdict from the measurements, so a report can never claim more than it measured.
  record     stores a run and attaches it to a release (`scale_proof_runs`).
  blocker    the reason a release is blocked: its latest attached run has a failed budget or check.
  file_tickets  one Jira Bug per finding signature (a budget over its limit, or incorrect behaviour at this size),
             no manual step; a repeated finding only counts up, a finding whose ticket is Done gets a new ticket.
  summary    the read model of /v1/state and /v1/scale-proof for the Command Center panel.

Docs: docs/v4/scale-proof.md.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .db import J

log = logging.getLogger("aiflow4.scale_proof")

SCHEMA = 1
LABEL = "scale-proof"
RUNS_SHOWN = 10


@dataclass(frozen=True)
class Budget:
    key: str
    title: str
    limit: float
    unit: str
    statistic: str   # what is compared with the limit; stated next to the budget in docs/v4/scale-proof.md
    measures: str


BUDGETS: tuple[Budget, ...] = (
    Budget("console_page_load", "Console pages load", 2.0, "s", "p95 per page, worst page",
           "Each console page (HTML plus the data call it renders from) is loaded 5 times at the busiest moment "
           "(200 sessions working); the 95th percentile of every page is compared, the slowest page counts."),
    Budget("project_picker", "Project picker answers", 200.0, "ms", "p95",
           "GET /v1/projects (the list behind the project picker) is called 20 times at the busiest moment; "
           "the 95th percentile of those calls is compared."),
    Budget("ticket_pickup", "Ticket is picked up", 30.0, "s", "p95",
           "Time from a ticket being ready (eligible for work) to a session's claim being granted, for every "
           "ticket of the traffic; the 95th percentile is compared."),
)
BUDGET_BY_KEY = {b.key: b for b in BUDGETS}


def percentile(values: Iterable[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100); 0.0 for no samples."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def describe(budgets: Iterable[Budget] = BUDGETS) -> list[dict[str, Any]]:
    return [{"key": b.key, "title": b.title, "limit": b.limit, "unit": b.unit, "statistic": b.statistic,
             "measures": b.measures} for b in budgets]


def evaluate(measured: dict[str, float], budgets: Iterable[Budget] = BUDGETS) -> list[dict[str, Any]]:
    """One verdict per budget. A budget with no measurement fails: unmeasured is not within budget."""
    out = []
    for b in budgets:
        value = measured.get(b.key)
        if value is None:
            passed, message = False, f"{b.title}: {b.key} was not measured"
        else:
            passed = value <= b.limit
            message = (f"{b.title}: {b.key} {b.statistic} {value:.3f} {b.unit} "
                       + (f"is within the budget of {b.limit:g} {b.unit}" if passed
                          else f"exceeds the budget of {b.limit:g} {b.unit}"))
        out.append({"key": b.key, "title": b.title, "limit": b.limit, "unit": b.unit, "statistic": b.statistic,
                    "measured": None if value is None else round(float(value), 3), "passed": passed,
                    "message": message})
    return out


def finalize(report: dict[str, Any], budgets: Iterable[Budget] = BUDGETS) -> dict[str, Any]:
    """Recompute budgets, HTTP and correctness checks, findings and the verdict from the report."""
    measured = {key: (m or {}).get("value") for key, m in (report.get("measurements") or {}).items()}
    verdicts = evaluate({k: v for k, v in measured.items() if isinstance(v, (int, float))}, budgets)
    checks = [c for c in report.get("checks") or [] if isinstance(c, dict)]
    seen_errors: set[tuple[str, str]] = set()
    for error in report.get("errors") or []:
        detail = str(error).strip()
        if not detail:
            continue
        if " answered HTTP " in detail:
            endpoint = detail.partition(" answered HTTP ")[0]
            name = f"http_response:{endpoint}"
        elif "could not join: HTTP " in detail:
            machine = detail.partition(" could not join: HTTP ")[0]
            name = f"http_response:{machine}"
        else:
            name = "harness_error"
        identity = (name, detail)
        if identity in seen_errors:
            continue
        seen_errors.add(identity)
        checks.append({"name": name, "ok": False, "detail": detail})
    failures = [v["message"] for v in verdicts if not v["passed"]]
    failures += [f"correctness: {c.get('name')}: {c.get('detail', '')}".rstrip(": ") for c in checks if not c.get("ok")]
    findings = [{"signature": f"budget:{v['key']}", "kind": "budget", "title": f"Scale proof: {v['message']}",
                 "detail": v["message"]} for v in verdicts if not v["passed"]]
    findings += [{"signature": f"correctness:{c.get('name')}", "kind": "correctness",
                  "title": f"Scale proof: incorrect behaviour at this size: {c.get('name')}",
                  "detail": str(c.get("detail", ""))} for c in checks if not c.get("ok")]
    return {**report, "schema": SCHEMA, "checks": checks, "budgets": verdicts, "failures": failures,
            "findings": findings, "passed": not failures}


# --- storage, release attachment and gate ---------------------------------------------------------------------
def record(ctx, report: dict[str, Any], release: str = "", source: str = "") -> int:
    """Store a finalized report and attach it to `release`; returns the run id."""
    report = finalize(report)
    release = (release or report.get("release") or "").strip()[:200]
    row = ctx.db.one("INSERT INTO scale_proof_runs (release, passed, plan_digest, source, report)"
                     " VALUES (%s,%s,%s,%s,%s) RETURNING id",
                     (release, report["passed"], str(report.get("plan_digest") or ""), source[:60],
                      J({**report, "release": release})))
    run_id = int(row["id"])
    ctx.events.emit("scale_proof.recorded", outcome="passed" if report["passed"] else "failed",
                    reason=f"{release or 'no release'}: " + ("within every budget" if report["passed"]
                                                             else "; ".join(report["failures"]))[:500],
                    data={"run_id": run_id, "release": release})
    return run_id


def latest_for_release(db, release: str) -> dict | None:
    return db.one("SELECT id, created_at, release, passed, report FROM scale_proof_runs WHERE release=%s"
                  " ORDER BY id DESC LIMIT 1", (release,)) if release else None


def _blocker_text(run: dict) -> str:
    return f"scale proof run #{run['id']} failed: " + "; ".join((run["report"] or {}).get("failures") or ["failed"])


def blocker(db, release: str) -> str:
    """Why `release` must not ship: its latest attached scale-proof run failed. Empty when fine or not attached."""
    run = latest_for_release(db, release)
    return "" if not run or run["passed"] else _blocker_text(run)


def attached(db, releases: list[str]) -> dict[str, dict[str, Any]]:
    """The latest run attached to each of `releases` (for the release screens): id, passed, when, blocker text."""
    rows = db.all("SELECT DISTINCT ON (release) id, created_at, release, passed, report FROM scale_proof_runs"
                  " WHERE release = ANY(%s) ORDER BY release, id DESC", (releases,)) if releases else []
    return {r["release"]: {"run_id": r["id"], "passed": r["passed"],
                           "created_at": r["created_at"].isoformat(timespec="seconds"),
                           "blocker": "" if r["passed"] else _blocker_text(r)} for r in rows}


# --- findings become tickets ----------------------------------------------------------------------------------
def _description(finding: dict, run_id: int, report: dict) -> str:
    sc = report.get("scenario") or {}
    return (f"{finding['detail']}\n\n"
            f"Found by scale proof run #{run_id} of release {report.get('release') or '(none)'}: "
            f"{sc.get('projects')} projects, {sc.get('machines')} machines, {sc.get('sessions')} simulated AI sessions, "
            f"seed {sc.get('seed')}, plan {str(report.get('plan_digest') or '')[:12]}.\n"
            "The run is listed in Command Center under Scale proof (docs/v4/scale-proof.md).")


def file_tickets(ctx, run_id: int) -> list[dict[str, Any]]:
    """File current findings and retry every persisted finding that still has no Jira ticket."""
    run = ctx.db.one("SELECT report FROM scale_proof_runs WHERE id=%s", (run_id,))
    if not run:
        return []
    report = run["report"]
    project = ctx.settings.canary_bug_project
    out = []
    for finding in report.get("findings") or []:
        ctx.db.run(
            "INSERT INTO scale_proof_findings (signature, kind, title, detail, first_run_id, last_run_id)"
            " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (signature) DO UPDATE SET last_run_id=EXCLUDED.last_run_id,"
            " occurrences=scale_proof_findings.occurrences+1, detail=EXCLUDED.detail, title=EXCLUDED.title,"
            " updated_at=now()", (finding["signature"], finding["kind"], finding["title"][:240],
                                  finding["detail"], run_id, run_id))
        row = ctx.db.one("SELECT ticket FROM scale_proof_findings WHERE signature=%s", (finding["signature"],))
        ticket = str((row or {}).get("ticket") or "")
        if ticket:
            status = (ctx.db.one("SELECT status FROM tickets WHERE key=%s", (ticket,)) or {}).get("status")
            if status != "Done":
                if ctx.jira:
                    try:
                        ctx.jira.comment(ticket, f"Seen again in scale proof run #{run_id} "
                                                 f"(release {report.get('release') or 'none'}): {finding['detail']}")
                    except Exception as exc:  # noqa: BLE001 - the count is already kept
                        log.warning("comment on %s failed: %s", ticket, type(exc).__name__)
                out.append({"signature": finding["signature"], "ticket": ticket, "created": False})
                continue
            ticket = ""  # the earlier ticket is Done and the finding is back: a new ticket
            ctx.db.run("UPDATE scale_proof_findings SET ticket='' WHERE signature=%s", (finding["signature"],))

    # A successful new run can still be the first opportunity to report a finding from an earlier run.
    pending = ctx.db.all(
        "SELECT f.signature, f.kind, f.title, f.detail, f.last_run_id, r.report"
        " FROM scale_proof_findings f JOIN scale_proof_runs r ON r.id=f.last_run_id"
        " WHERE f.ticket='' ORDER BY f.updated_at, f.signature")
    for item in pending:
        finding = {"signature": item["signature"], "kind": item["kind"], "title": item["title"],
                   "detail": item["detail"]}
        finding_run_id = int(item["last_run_id"])
        finding_report = item["report"] or report
        if not ctx.jira:
            ctx.db.run("UPDATE scale_proof_findings SET last_error='Jira is not configured' WHERE signature=%s",
                       (finding["signature"],))
            out.append({"signature": finding["signature"], "ticket": "", "created": False})
            continue
        labels = ["flow-bug", LABEL, finding["signature"].replace(":", "-")]
        try:
            description = _description(finding, finding_run_id, finding_report)
            key = ctx.jira.create_issue(project, finding["title"][:240], description, issuetype="Bug", labels=labels)
        except Exception as exc:  # noqa: BLE001 - retried by the next release submission
            ctx.db.run("UPDATE scale_proof_findings SET last_error=%s WHERE signature=%s",
                       (str(exc)[:300] or type(exc).__name__, finding["signature"]))
            out.append({"signature": finding["signature"], "ticket": "", "created": False})
            continue
        ctx.db.run("UPDATE scale_proof_findings SET ticket=%s, last_error='' WHERE signature=%s",
                   (key, finding["signature"]))
        try:
            status = ctx.jira.status(key)
        except Exception:  # noqa: BLE001
            status = "Created"
        ctx.db.run("INSERT INTO tickets (key, project, status, type, labels, summary, description, jira_created,"
                   " jira_updated) VALUES (%s,%s,%s,'Bug',%s,%s,%s,now(),now()) ON CONFLICT (key) DO NOTHING",
                   (key, project, status, labels, finding["title"][:240], description))
        ctx.db.run("INSERT INTO write_audit (target, action, ticket, actor, accepted, result, payload)"
                   " VALUES ('jira','scale_proof_create',%s,'scale-proof',true,%s,%s)",
                   (key, status, J({"project": project, "labels": labels, "signature": finding["signature"],
                                    "run_id": finding_run_id})))
        ctx.events.emit("scale_proof.ticket", ticket=key, project=project, outcome="created",
                        reason=finding["title"][:300], data={"signature": finding["signature"],
                                                              "run_id": finding_run_id})
        out.append({"signature": finding["signature"], "ticket": key, "created": True})
    return out


# --- read model for the console -------------------------------------------------------------------------------
def _run_view(row: dict, tickets: dict[str, str]) -> dict[str, Any]:
    report = row["report"] or {}
    return {"id": row["id"], "created_at": row["created_at"].isoformat(timespec="seconds"),
            "release": row["release"], "passed": row["passed"], "source": row["source"],
            "scenario": report.get("scenario") or {}, "counts": report.get("counts") or {},
            "duration_s": report.get("duration_s"), "plan_digest": report.get("plan_digest", ""),
            "budgets": report.get("budgets") or [], "failures": report.get("failures") or [],
            "checks": report.get("checks") or [],
            "tickets": [{"signature": f["signature"], "ticket": tickets.get(f["signature"], "")}
                        for f in report.get("findings") or []]}


def summary(db) -> dict[str, Any]:
    runs = db.all("SELECT id, created_at, release, passed, source, report FROM scale_proof_runs"
                  " ORDER BY id DESC LIMIT %s", (RUNS_SHOWN,))
    findings = db.all("SELECT signature, kind, title, ticket, occurrences, last_run_id, last_error"
                      " FROM scale_proof_findings ORDER BY updated_at DESC LIMIT 50")
    tickets = {f["signature"]: f["ticket"] for f in findings}
    return {"budgets": describe(), "runs": [_run_view(r, tickets) for r in runs], "findings": findings}
