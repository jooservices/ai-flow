"""Helpers for executing the console with the static assets the browser loads beside it."""
import re

from aiflow.v4.control.ui import HERE, console_template

BOOT = "/* ---------- boot ---------- */"


def console_script() -> str:
    script = re.search(r"<script>\n(.*?)</script>", console_template(), re.S).group(1)
    script = script[:script.index(BOOT)]
    assets = HERE
    before_console = "".join((assets / name).read_text(encoding="utf-8") for name in
                             ("release-readiness.js", "scale-proof.js", "flow-doctor.js", "owner_escalation.js"))
    extensions = "\n".join((assets / name).read_text(encoding="utf-8") for name in
                            ("csrf.js", "layout.js", "security.js", "load-policy.js"))
    return before_console + script + extensions
