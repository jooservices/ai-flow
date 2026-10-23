"""One template for every comment AI Flow writes on a ticket, so people read the same shape every time:
what happened, why, the result, what comes next, and which run did it."""
from __future__ import annotations

import time


def note(c, headline: str, what: str = "", why: str = "", result: str = "", next_: str = "", t0: float | None = None,
         node: str = "") -> str:
    lines = [f"AI Flow · {c.step} · {headline}"]
    for label, text in (("What", what), ("Why", why), ("Result", result), ("Next", next_)):
        if text:
            lines.append(f"{label}: {text.strip()}")
    took = f" · {int((time.time() - t0) // 60)} min {int((time.time() - t0) % 60)} s" if t0 else ""
    model = f"/{c.model}" if c.model else ""
    lines.append(f"Run: {c.account} ({c.provider}{model}){' on ' + node if node else ''}{took} · run {c.run_id[:8]} · fence {c.fence}")
    return "\n".join(lines)


def project_env(project_key: str, items: list[dict] | None = None, step: str = "dev") -> dict[str, str]:
    """Environment of a project's AI runs. The environment section of the project configuration (`items`, as the
    claim carries it) gives the values of its test services, URLs, databases and browsers to the runs allowed to use
    them, and the node-local /etc/aiflow/projects/<KEY>.env (root-only, never in the control repo) holds the secrets
    those items name; the file also keeps working for variables the section does not list."""
    from ..core.environment import items_of, run_environment
    from ..core.settings import read_env_file
    from ..core.toolchains import PLAYWRIGHT_BROWSERS_DIR
    node_env = read_env_file(f"/etc/aiflow/projects/{project_key}.env")
    # the shared browsers folder is part of every run's environment; a project file may override it
    return {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_DIR,
            **node_env, **run_environment(items_of(items or []), step, node_env)}


def environment_notes(items: list[dict] | None, step: str, env: dict[str, str]) -> str:
    """The names (never the values of secrets) of the environment items of this run, for the run notes."""
    from ..core.environment import items_of, notes_block
    return notes_block(items_of(items or []), step, env)
