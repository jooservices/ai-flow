from __future__ import annotations

from aiflow.v4.control.db import J
from aiflow.v4.control.repositories.accounts import AccountRepository, FakeAccountRepository


def test_fake_account_repository_matches_database_reads_for_large_account_set(db):
    database_repository = AccountRepository(db)
    fake_repository = FakeAccountRepository()
    assert fake_repository.quota_routing_rows([]) == database_repository.quota_routing_rows([]) == []
    assert fake_repository.ready_ids() == database_repository.ready_ids() == set()

    providers = ("claude", "codex", "grok")
    statuses = ("idle", "working", "probing", "paused", "signed_out", "error", "unknown")
    rows = [
        {"id": f"jaif301-{index:04d}", "provider": providers[index % len(providers)],
         "status": statuses[index % len(statuses)],
         "quota": None if index % 17 == 0 else {"windows": {"five_hour": {"used": index}}},
         "quota_at_risk": index % 2 == 0}
        for index in range(180)
    ]
    with db.transaction():
        for row in rows:
            db.run("INSERT INTO accounts (id,provider,status,quota,quota_at_risk) VALUES (%s,%s,%s,%s,%s)",
                   (row["id"], row["provider"], row["status"], J(row["quota"]), row["quota_at_risk"]))
    fake_repository = FakeAccountRepository(rows)

    for selected_providers in ([], ["unknown"], ["claude"], ["grok", "codex"], ["grok", "codex", "grok"]):
        fake_rows = fake_repository.quota_routing_rows(selected_providers)
        database_rows = database_repository.quota_routing_rows(selected_providers)
        assert fake_rows == database_rows
        assert all(set(row) == {"id", "provider", "quota", "quota_at_risk"} for row in fake_rows)
        assert [row["id"] for row in fake_rows] == sorted(row["id"] for row in fake_rows)
    assert fake_repository.ready_ids() == database_repository.ready_ids()
    assert fake_repository.ready_ids() == {
        row["id"] for row in rows if row["status"] in {"idle", "working", "probing"}
    }


def test_fake_account_repository_copies_input_and_returned_quota_values():
    source = [{"id": "account-a", "provider": "codex", "status": "idle",
               "quota": {"windows": ["original"]}, "quota_at_risk": None}]
    repository = FakeAccountRepository(source)
    source[0]["quota"]["windows"].append("input mutation")
    source[0]["status"] = "signed_out"

    first_read = repository.quota_routing_rows(["codex"])
    first_read[0]["quota"]["windows"].append("output mutation")
    assert repository.quota_routing_rows(["codex"]) == [
        {"id": "account-a", "provider": "codex", "quota": {"windows": ["original"]}, "quota_at_risk": None}
    ]
    assert repository.ready_ids() == {"account-a"}


def test_fake_account_repository_matches_database_read_with_nullable_risk(db):
    rows = [
        {"id": "account-null-risk", "provider": "codex", "status": "idle", "quota": None,
         "quota_at_risk": None},
        {"id": "account-safe", "provider": "codex", "status": "paused",
         "quota": {"windows": {"five_hour": {"utilization": 0.4}}}, "quota_at_risk": False},
    ]
    database_repository = AccountRepository(db)
    fake_repository = FakeAccountRepository(rows)

    # The production column is NOT NULL; shadow it only inside this transaction to cover a nullable read result.
    with db.transaction():
        db.run("CREATE TEMP TABLE accounts (id text, provider text, status text, quota jsonb, quota_at_risk boolean)")
        for row in rows:
            db.run("INSERT INTO accounts (id,provider,status,quota,quota_at_risk) VALUES (%s,%s,%s,%s,%s)",
                   (row["id"], row["provider"], row["status"], J(row["quota"]), row["quota_at_risk"]))

        assert fake_repository.quota_routing_rows(["codex"]) == database_repository.quota_routing_rows(["codex"])
        assert fake_repository.ready_ids() == database_repository.ready_ids()
