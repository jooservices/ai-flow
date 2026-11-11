"""GET /v1/accounts/{account_id}/spec answers only nodes the account runs on."""
from __future__ import annotations

import pytest

from conftest import join_node
from aiflow.v4.control.api import create_app
from aiflow.v4.control.features.fleet import service
from aiflow.v4.control.route_registry import concrete_routes


def node_headers(client, name):
    joined = join_node(client, name)
    assert joined.status_code == 200, joined.text
    return {"Authorization": f"Bearer {joined.json()['node_token']}"}


def test_account_specification_service_checks_assignment_before_model_selection():
    class Account:
        id = "claude-01"
        provider = "anthropic"
        tier = "pro"

        def runs_on(self, node):
            return node == "cc"

    class Repo:
        def __init__(self, account):
            self.account_row = account
            self.model_calls = []

        def account(self, account_id):
            assert account_id == "claude-01"
            return self.account_row

        def model_for(self, provider, tier):
            self.model_calls.append((provider, tier))
            return "claude-opus", "high"

    repo = Repo(Account())
    assert service.account_specification(repo, "claude-01", "cc") == {
        "account": "claude-01", "provider": "anthropic", "tier": "pro",
        "model": "claude-opus", "effort": "high",
    }
    assert repo.model_calls == [("anthropic", "pro")]

    for account, node in ((None, "cc"), (Account(), "node1")):
        repo = Repo(account)
        with pytest.raises(service.AccountSpecRefused, match="account claude-01 does not run on"):
            service.account_specification(repo, "claude-01", node)
        assert repo.model_calls == []


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

    assert refused.status_code == 403
    assert refused.json() == {"detail": "account claude-01 does not run on node1"}
    assert unknown.status_code == 403
    assert unknown.json() == {"detail": "account no-such-account does not run on node1"}


def test_the_account_spec_requires_a_valid_node_token_without_disclosing_the_spec(client):
    for headers in (None, {"Authorization": ""}, {"Authorization": "Bearer invalid"}):
        response = client.get("/v1/accounts/claude-01/spec", headers=headers)

        assert response.status_code == 401
        assert response.json() == {"detail": "unknown node token"}


def test_an_account_enabled_on_multiple_nodes_allows_each_assigned_node(client, ctx):
    ctx.repo().account("claude-01").extra_machines.append("node1")

    for node in ("cc", "node1"):
        response = client.get("/v1/accounts/claude-01/spec", headers=node_headers(client, node))

        assert response.status_code == 200
        assert response.json()["account"] == "claude-01"


def test_the_assembled_app_discovers_the_account_spec_route_once(ctx):
    matches = [route for route in concrete_routes(create_app(ctx).routes)
               if route.path == "/v1/accounts/{account_id}/spec" and "GET" in route.methods]

    assert len(matches) == 1
    assert matches[0].endpoint.__module__ == "aiflow.v4.control.routes.account_spec"
    assert matches[0].endpoint.__name__ == "account_spec"
