"""Worker-step names and the account role required to claim each step."""

_ROLE_BY_STEP = {
    "dev": "dev",
    "verify": "verify",
    "qa": "verify",
    "triage": "master",
    "intake": "intake_refiner",
    "impact": "intake_refiner",
    "post_merge": "release",
}


def role_for_step(step: str) -> str | None:
    """Role that may claim this step; unknown steps are never eligible."""
    return _ROLE_BY_STEP.get(step)
