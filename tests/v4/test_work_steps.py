import pytest

from aiflow.v4.control.work_steps import roles_for_step


@pytest.mark.parametrize("step,role", [
    ("dev", "dev"),
    ("verify", "verify"),
    ("qa", "verify"),
    ("triage", "master"),
    ("intake", "intake_refiner"),
    ("impact", "intake_refiner"),
    ("post_merge", "release"),
])
def test_claim_step_only_keeps_its_role(step, role):
    assert roles_for_step({role, "unrelated"}, step) == {role}


def test_unknown_step_has_no_eligible_roles():
    assert roles_for_step({"dev", "verify"}, "unknown") == set()


def test_account_without_the_step_role_is_not_eligible():
    assert roles_for_step({"verify"}, "dev") == set()


def test_unfiltered_work_listing_preserves_all_roles():
    assert roles_for_step({"dev", "verify"}, None) == {"dev", "verify"}
