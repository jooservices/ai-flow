"""An epic is never offered as work, whatever its labels (JAIF-439)."""
from __future__ import annotations

from conftest import add_ticket, join_node

from aiflow.v4.control import incidents


def join(client, name):
    return {"Authorization": "Bearer " + join_node(client, name).json()["node_token"]}


def keys_offered(client, headers, account="claude-01"):
    return {t["key"] for t in client.get("/v1/work/candidates", params={"account": account}, headers=headers).json()["tickets"]}


def test_a_ready_labelled_epic_is_not_offered_but_an_ordinary_ticket_is(client, ctx):
    headers = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-910")
    add_ticket(ctx.db, "TESTWP-911")
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-911'")
    offered = keys_offered(client, headers)
    assert "TESTWP-910" in offered and "TESTWP-911" not in offered


def test_an_epic_in_any_status_is_not_offered(client, ctx):
    headers = join(client, "cc")
    for index, status in enumerate(("To Do", "In Verify", "Intake", "Backlog", "Needs owner")):
        key = f"TESTWP-92{index}"
        add_ticket(ctx.db, key, status=status)
        ctx.db.run("UPDATE tickets SET type='Epic' WHERE key=%s", (key,))
    assert not {key for key in keys_offered(client, headers) if key.startswith("TESTWP-92")}


def test_the_idle_flow_check_ignores_epics(client, ctx):
    add_ticket(ctx.db, "TESTWP-930")
    ctx.db.run("UPDATE tickets SET type='Epic', synced_at=now()-interval '2 hours', jira_updated=now()-interval '2 hours'"
               " WHERE key='TESTWP-930'")
    assert incidents.idle_tickets(ctx, 30) == []
