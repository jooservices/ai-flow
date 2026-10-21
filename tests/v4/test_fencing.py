import time

from aiflow.v4.core.fencing import Leases


def test_claim_is_exclusive_and_fences_grow(leases: Leases):
    a = leases.claim("TESTWP-1", "run-a", 5_000)
    assert a.ok and a.fence == 1
    b = leases.claim("TESTWP-1", "run-b", 5_000)
    assert not b.ok and b.holder == "run-a:1"
    assert leases.valid("TESTWP-1", "run-a", 1)
    assert not leases.valid("TESTWP-1", "run-b", 1)
    assert leases.release("TESTWP-1", "run-a", 1)
    c = leases.claim("TESTWP-1", "run-c", 5_000)
    assert c.ok and c.fence == 2


def test_expired_lease_lets_the_next_owner_in_and_stale_token_is_refused(leases: Leases):
    a = leases.claim("TESTWP-2", "run-a", 200)
    time.sleep(0.35)
    assert not leases.valid("TESTWP-2", "run-a", a.fence)
    b = leases.claim("TESTWP-2", "run-b", 5_000)
    assert b.ok and b.fence == a.fence + 1
    # the old owner wakes up: renew, check and release all fail
    assert not leases.renew("TESTWP-2", "run-a", a.fence, 5_000)
    assert not leases.valid("TESTWP-2", "run-a", a.fence)
    assert not leases.release("TESTWP-2", "run-a", a.fence)
    assert leases.holder("TESTWP-2") == f"run-b:{b.fence}"


def test_renew_extends_only_for_the_owner(leases: Leases):
    a = leases.claim("TESTWP-3", "run-a", 300)
    assert leases.renew("TESTWP-3", "run-a", a.fence, 5_000)
    time.sleep(0.4)
    assert leases.valid("TESTWP-3", "run-a", a.fence)
    assert leases.ttl_ms("TESTWP-3") > 3_000
    assert "TESTWP-3" in leases.held_keys()


def test_cooldown_and_leader(leases: Leases):
    leases.cooldown("codex-01", 30, "quota")
    reason, ttl = leases.cooling("codex-01")
    assert reason == "quota" and 0 < ttl <= 30
    assert leases.cooling("claude-01") is None
    assert leases.leader("jira_sync", "a", 5_000)
    assert leases.leader("jira_sync", "a", 5_000)
    assert not leases.leader("jira_sync", "b", 5_000)
