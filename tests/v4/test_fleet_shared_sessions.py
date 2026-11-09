"""The fleet shared-session service keeps account rules outside HTTP and SQL layers."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control.features.fleet import shared_sessions_service as service


ROOT = Path(__file__).parents[2]


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, active):
        self.active = active
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return self

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "count(*) AS n FROM runs" in sql:
            return _Result({"n": self.active})
        return _Result(None)


class _DB:
    def __init__(self, active):
        self.connection = _Connection(active)
        self.pool = SimpleNamespace(connection=lambda: self.connection)


@pytest.mark.unit
def test_session_limit_refusal_keeps_the_active_count_and_does_not_write():
    db = _DB(active=3)
    account = SimpleNamespace(id="shared-01", provider="claude", tier="middle", roles=["dev"],
                              explicit_only=False, sessions=5)
    ctx = SimpleNamespace(db=db, repo=lambda: SimpleNamespace(account=lambda _account_id: account))
    body = SimpleNamespace(session_limit=2)
    user = SimpleNamespace(subject="owner@example.test")

    with pytest.raises(service.SharedAccountRefused) as refused:
        service.set_session_limit_record(ctx, "shared-01", body, user)

    assert refused.value.status == 409
    assert refused.value.detail == {
        "error": "limit_below_active_sessions",
        "message": "shared-01 has 3 running sessions; wait for 1 to finish before lowering the limit to 2.",
    }
    assert [sql.split()[0] for sql, _params in db.connection.calls] == ["SELECT", "SELECT"]


@pytest.mark.unit
def test_account_view_keeps_the_existing_response_shape():
    account = SimpleNamespace(id="shared-01", provider="claude", tier="middle", roles=["dev"], sessions=4)
    db = SimpleNamespace(all=lambda _sql, _params=(): [{"name": "node-a"}, {"name": "node-b"}])

    result = service.account_view(
        db, account, include_signin=True,
        online_nodes_fn=lambda _db: {"node-a"},
        running_by_node_fn=lambda _db, _account: {"node-a": 2},
        machines_view_fn=lambda _db, _account, status, *, include_signin:
        [{"node": name, "status": status[name], "include_signin": include_signin} for name in status],
    )

    assert result == {
        "id": "shared-01", "provider": "claude", "tier": "middle", "roles": ["dev"],
        "session_limit": 4, "sessions_active": 2,
        "machines": [
            {"node": "node-a", "status": "online", "include_signin": True},
            {"node": "node-b", "status": "offline", "include_signin": True},
        ],
    }


@pytest.mark.unit
def test_shared_session_sql_is_confined_to_its_repository():
    feature = ROOT / "aiflow/v4/control/features/fleet"
    sql_starts = {"SELECT", "INSERT", "UPDATE", "DELETE"}

    def sql_statements(module: str) -> list[str]:
        tree = ast.parse((feature / module).read_text(encoding="utf-8"))
        return [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and bool(node.value.strip())
                and node.value.lstrip().split(maxsplit=1)[0].upper() in sql_starts]

    assert any("shared_accounts WHERE managed" in sql for sql in sql_statements("repository.py"))
    assert not sql_statements("shared_sessions_service.py")
    assert not sql_statements("routes_shared_sessions.py")
