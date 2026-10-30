"""Routes of the scale_proof feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
from types import SimpleNamespace

from .bodies import ScaleProofBody
from fastapi import Depends, HTTPException
from ... import (scale_proof)


def install(app, ctx, deps) -> SimpleNamespace:
    audit = deps.audit
    owner_auth = deps.owner_auth
    # --- scale proof: budgets, runs attached to releases, tickets for findings (docs/v4/scale-proof.md) ---------
    @app.get("/v1/scale-proof")
    def scale_proof_summary() -> dict:
        return scale_proof.summary(ctx.db)

    @app.post("/v1/scale-proof/results")
    def scale_proof_results(body: ScaleProofBody, actor: str = Depends(owner_auth)) -> dict:
        """CI submits a run: it is attached to the release, judged against the budgets here (the submitted verdict
        is ignored) and every finding becomes a ticket."""
        report = body.report
        if not isinstance(report.get("measurements"), dict) or not report["measurements"]:
            raise HTTPException(422, "the report has no measurements")
        run_id = scale_proof.record(ctx, report, body.release, body.source)
        tickets = scale_proof.file_tickets(ctx, run_id)
        run = ctx.db.one("SELECT release, passed, report FROM scale_proof_runs WHERE id=%s", (run_id,))
        audit("aiflow-control", "scale-proof-result", "", "", None, actor, True,
              "passed" if run["passed"] else "failed", {"run_id": run_id, "release": run["release"]})
        return {"run_id": run_id, "release": run["release"], "passed": run["passed"], "blocked": not run["passed"],
                "failures": run["report"]["failures"], "tickets": tickets}


    return SimpleNamespace(scale_proof_summary=scale_proof_summary, scale_proof_results=scale_proof_results)
