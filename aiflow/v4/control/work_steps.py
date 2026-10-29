"""Worker-step names and the account roles required to claim each step."""
from collections.abc import Iterable

_ROLE_BY_STEP = {
    "dev": "dev",
    "verify": "verify",
    "qa": "verify",
    "triage": "master",
    "intake": "intake_refiner",
    "impact": "intake_refiner",
    "post_merge": "release",
}


def roles_for_step(roles: Iterable[str], step: str | None) -> set[str]:
    """Return account roles relevant to a claim; unknown steps are never eligible."""
    eligible = set(roles)
    if step is None:
        return eligible
    role = _ROLE_BY_STEP.get(step)
    return eligible & {role} if role else set()
