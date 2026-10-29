"""JAIF-364: split parents are never developed; children follow blocker order."""
from aiflow.v4.control.worker_candidates import DEV_TICKET_SQL, SPLIT_PARENT, may_offer_dev


def _ticket(key, labels, blocked_by=()):
    return {"key": key, "labels": list(labels), "blocked_by": list(blocked_by), "status": "To Do"}


def test_dev_ticket_sql_escapes_like_wildcard_for_psycopg():
    assert "LIKE 'aiflow-split-%%'" in DEV_TICKET_SQL


def test_split_parent_is_never_a_developer_candidate():
    parent = _ticket("TESTWP-40", ["aiflow-ready", SPLIT_PARENT])
    assert may_offer_dev(parent, done=set()) is False
    assert may_offer_dev(parent, done={"TESTWP-1"}) is False


def test_child_with_unfinished_blocker_is_not_offered():
    child = _ticket("TESTWP-42", ["aiflow-split-testwp-40-2"], blocked_by=["TESTWP-41"])
    assert may_offer_dev(child, done=set()) is False
    assert may_offer_dev(child, done={"TESTWP-99"}) is False


def test_unblocked_split_child_is_offered_without_aiflow_ready():
    first = _ticket("TESTWP-41", ["aiflow-split-testwp-40-1"])
    second = _ticket("TESTWP-42", ["aiflow-split-testwp-40-2"], blocked_by=["TESTWP-41"])
    assert may_offer_dev(first, done=set()) is True
    assert may_offer_dev(second, done={"TESTWP-41"}) is True


def test_hold_lease_and_aiflow_hold_still_apply():
    ready = _ticket("TESTWP-10", ["aiflow-ready"])
    held = _ticket("TESTWP-11", ["aiflow-ready"])
    cooling = _ticket("TESTWP-12", ["aiflow-ready"])
    parked = _ticket("TESTWP-13", ["aiflow-ready"])
    labelled_hold = _ticket("TESTWP-14", ["aiflow-ready", "aiflow-hold"])
    split_hold = _ticket("TESTWP-15", ["aiflow-split-testwp-40-1", "aiflow-hold"])

    assert may_offer_dev(ready, done=set()) is True
    assert may_offer_dev(held, done=set(), held={"TESTWP-11"}) is False
    assert may_offer_dev(cooling, done=set(), cooling={"TESTWP-12"}) is False
    assert may_offer_dev(parked, done=set(), parked={"TESTWP-13"}) is False
    assert may_offer_dev(labelled_hold, done=set()) is False
    assert may_offer_dev(split_hold, done=set()) is False
