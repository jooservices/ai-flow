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


def project_env(project_key: str) -> dict[str, str]:
    """Node-local environment for a project's AI runs (test service URLs and the like), from
    /etc/aiflow/projects/<KEY>.env (root-only on the node, never in the control repo)."""
    from ..core.settings import read_env_file
    from ..core.toolchains import PLAYWRIGHT_BROWSERS_DIR
    # the shared browsers folder is part of every run's environment; a project file may override it
    return {"PLAYWRIGHT_BROWSERS_PATH": PLAYWRIGHT_BROWSERS_DIR,
            **read_env_file(f"/etc/aiflow/projects/{project_key}.env")}
