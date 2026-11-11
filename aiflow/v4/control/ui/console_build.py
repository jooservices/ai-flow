"""Build the v4 console document from its manifest and trusted UI extensions."""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
CONSOLE_SOURCES = HERE / "console"


def console_source_names() -> list[str]:
    """The console source files in page order, one file name per line."""
    return (CONSOLE_SOURCES / "manifest.txt").read_text(encoding="utf-8").split()


def console_template() -> str:
    """The console sources joined in manifest order, before view extensions are injected."""
    return "".join((CONSOLE_SOURCES / name).read_text(encoding="utf-8") for name in console_source_names())


def build_console_page() -> str:
    """Compose the console with its independently maintained Security and System check views."""
    html = console_template()
    tab_marker = '      <button class="tab" role="tab" id="tab-licensing"'
    view_marker = '  <section class="view" id="view-licensing"'
    security_tab_marker = '      <button class="tab" role="tab" id="tab-notifications"'
    security_script_marker = "/* ---------- boot ---------- */"
    script_marker = "})();\n</script>"
    boot_marker = "/* ---------- boot ---------- */"
    tab = ('      <button class="tab" role="tab" id="tab-system-check" aria-controls="view-system-check" '
           'aria-selected="false" data-act="view" data-menu-view data-view="system-check">System check</button>')
    panel = ('  <section class="view" id="view-system-check" role="tabpanel" '
             'aria-labelledby="tab-system-check" hidden></section>')
    security_tab = ('      <button class="tab" role="tab" id="tab-security" aria-controls="view-security" '
                    'aria-selected="false" data-act="view" data-menu-view data-view="security">Security</button>')
    for marker in (tab_marker, view_marker, security_tab_marker, security_script_marker, script_marker):
        if marker not in html:
            raise RuntimeError("console template is missing a view insertion point")
    html = html.replace(tab_marker, tab + "\n" + tab_marker, 1)
    html = html.replace(view_marker, panel + "\n" + view_marker, 1)
    html = html.replace(security_tab_marker, security_tab + "\n" + security_tab_marker, 1)
    security_extension = "\n".join((
        (HERE / "csrf.js").read_text(encoding="utf-8"),
        (HERE / "layout.js").read_text(encoding="utf-8"),
        (HERE / "security.js").read_text(encoding="utf-8"),
    ))
    html = html.replace(security_script_marker, security_extension + "\n" + security_script_marker, 1)
    policy = (HERE / "load-policy.js").read_text(encoding="utf-8")
    timeouts = (HERE / "dashboard-timeouts.js").read_text(encoding="utf-8")
    silent_runs = (HERE / "dashboard-silent-runs.js").read_text(encoding="utf-8")
    attention = (HERE / "dashboard_attention.js").read_text(encoding="utf-8")
    yield_metrics = (HERE / "dashboard-yield.js").read_text(encoding="utf-8")
    cpu_use = (HERE / "dashboard-cpu.js").read_text(encoding="utf-8")
    extension = (HERE / "system-check.js").read_text(encoding="utf-8")
    html = html.replace(boot_marker, policy + "\n" + timeouts + "\n" + silent_runs + "\n" + attention
                        + "\n" + yield_metrics + "\n" + cpu_use + "\n" + boot_marker, 1)
    return html.replace(script_marker, extension + "\n" + script_marker, 1)


def build_served_console_page() -> str:
    """Build the console response, redirecting its first licensing tag to the combined asset."""
    return build_console_page().replace(
        '<script src="/console-assets/licensing-notice.js"></script>',
        '<script src="/console-assets/console-features.js"></script>', 1)
