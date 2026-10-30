"""The run timeline rows are written by listeners of RunStarted and RunFinished (JAIF-483)."""
from aiflow.v4.control.features.work.events import RunStarted
from aiflow.v4.control.features.work.listeners import run_finished
from aiflow.v4.control.platform.events.wiring import publish_for


def _row(ctx, type_):
    return ctx.db.one("SELECT * FROM events WHERE type=%s AND run_id='R-1'", (type_,))


def test_run_started_writes_the_claimed_row_in_its_transaction(ctx):
    with ctx.db.transaction():
        publish_for(ctx, RunStarted(run_id="R-1", ticket_key="TST-1", project="TST", step="dev", fence=3,
                                    account="a1", provider="claude", model="m", node="n1"))
    row = _row(ctx, "run.claimed")
    assert row["ticket"] == "TST-1" and row["fence"] == 3 and row["node"] == "n1"
    assert ctx.db.one("SELECT type FROM domain_events WHERE aggregate_id='R-1'")["type"] == "run.started"


def test_run_finished_keeps_usage_in_the_timeline_row(ctx):
    run_finished(ctx, run_id="R-1", ticket_key="TST-1", project="TST", step="dev", fence=3, account="a1",
                 provider="claude", model="m", node="n1", outcome="done", reason="ok", duration_ms=1200,
                 usage_in=10, usage_out=20, cost_usd=0.5, still_owner=True, pr_url="https://x/pull/1")
    row = _row(ctx, "run.finished")
    assert row["outcome"] == "done" and row["duration_ms"] == 1200
    assert row["data"]["tokens_in"] == 10 and row["data"]["tokens_out"] == 20 and row["data"]["still_owner"] is True
