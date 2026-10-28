"""Helpers for executing the console with the static assets the browser loads beside it."""
import re
from pathlib import Path

BOOT = "/* ---------- boot ---------- */"


def console_script(page: Path) -> str:
    source = page.read_text(encoding="utf-8")
    script = re.search(r"<script>\n(.*?)</script>", source, re.S).group(1)
    script = script[:script.index(BOOT)]
    assets = page.parent
    before_console = "".join((assets / name).read_text(encoding="utf-8") for name in
                             ("release-readiness.js", "scale-proof.js"))
    extensions = "\n".join((assets / name).read_text(encoding="utf-8") for name in
                            ("csrf.js", "layout.js", "security.js", "load-policy.js"))
    return before_console + script + extensions
