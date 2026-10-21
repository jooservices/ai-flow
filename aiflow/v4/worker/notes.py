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
