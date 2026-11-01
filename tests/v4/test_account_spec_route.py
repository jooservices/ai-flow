"""GET /v1/accounts/{account_id}/spec answers only the node the account runs on (the route lives in the fleet feature)."""
from __future__ import annotations

from conftest import join_node


def node_headers(client, name):
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def test_the_account_spec_is_given_to_the_node_that_runs_the_account(client, ctx):
    headers = node_headers(client, "cc")

    response = client.get("/v1/accounts/claude-01/spec", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    account = ctx.repo().account("claude-01")
    model, effort = ctx.repo().model_for(account.provider, account.tier)
    assert body == {"account": "claude-01", "provider": account.provider, "tier": account.tier,
                    "model": model, "effort": effort}


def test_the_account_spec_is_refused_to_another_node_and_to_an_unknown_account(client):
    other = node_headers(client, "node1")

    refused = client.get("/v1/accounts/claude-01/spec", headers=other)
    unknown = client.get("/v1/accounts/no-such-account/spec", headers=other)

    assert refused.status_code == 403 and "does not run on node1" in refused.text
    assert unknown.status_code == 403
