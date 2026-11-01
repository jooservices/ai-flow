"""Characterization cases for the console view diff mapper."""
from __future__ import annotations

import re
import subprocess

import pytest

from aiflow.v4.control.ui import console_template
from aiflow.v4.core import docsite
from aiflow.v4.core import docsite_views


# Expected values are literal snapshots from the pre-refactor implementation.
ALL_VIEWS = [
    "backup", "business", "cc", "delivery", "integrations", "knowledge", "lead", "licensing", "machines",
    "notifications", "projects", "review", "sa", "security", "settings", "stakeholder", "support",
    "system-check", "users", "work",
]
LAYOUT_VIEWS = ["cc", "delivery", "knowledge", "machines", "review", "users"]
EXPECTED_EVENTS = [
    "extension", "extension", "extension", "merge-base", "page:HEAD", "page:BASE", "no-index",
    "console-views", "asset-list",
]
CASES = [
    {"name": "unchanged", "hunks": [], "expected": []},
    {"name": "view section", "hunks": [('id="view-projects"', 1)], "expected": ["projects"]},
    {"name": "mapped renderer", "hunks": [("function renderProjects(", 1)], "expected": ["projects"]},
    {"name": "shared help renderer", "hunks": [("function renderHelp(", 1)], "expected": ALL_VIEWS},
    {"name": "users refresh exception", "hunks": [("S.view === 'users'", 1)], "expected": ["users"]},
    {"name": "view-specific tab", "hunks": [('id="tab-support"', 1)], "expected": ["support"]},
    {"name": "global theme style", "hunks": [(":root{", 1)], "expected": ALL_VIEWS},
    {"name": "shared help plumbing", "hunks": [('id="help-link"', 1)], "expected": ALL_VIEWS},
    {"name": "screen hint", "hunks": [("  cc: '", 1)], "expected": ["cc"]},
    {"name": "hint declaration", "hunks": [("var HINTS = {", 1)], "expected": ALL_VIEWS},
    {"name": "layout style", "hunks": [(".dashboard-layout-headings{", 1)], "expected": LAYOUT_VIEWS},
    {"name": "first-run panel", "hunks": [('data-block="first-run-setup"', 1)], "expected": ["cc"]},
    {"name": "security routing line", "hunks": [("S.view === 'security'", 1)], "expected": ["security"]},
    {"name": "shared toast style", "hunks": [(".toasts{", 1)], "expected": ["cc", "projects"]},
    {"name": "escalation mapping", "hunks": [("esc: { data:", 1)], "expected": ["cc", "machines"]},
    {"name": "machine form mapping", "hunks": [("rh-form", 1)], "expected": ["machines"]},
    {"name": "local log navigation", "hunks": [("logs-nav", 1)], "expected": ["cc"]},
    {"name": "extension view", "hunks": [], "extension": "system-check.js",
     "expected": ["projects", "system-check"]},
    {"name": "mapped UI asset", "hunks": [], "assets": "aiflow/v4/control/ui/provider-routing.js\n",
     "expected": ["machines"]},
    {"name": "removed duplicate panel", "delete": 'id="view-machines"', "expected": []},
    {"name": "removed unknown panel", "delete": 'id="view-machines"', "legacy": True,
     "expected": ["legacy"]},
    {"name": "removed shared initializer", "delete": "loadPrefs();", "shared_delete": True,
     "expected": ALL_VIEWS},
    {"name": "hunk beyond end", "hunks": [("<END>", 1)], "expected": []},
    {"name": "blank line", "hunks": [("<BLANK>", 1)], "expected": []},
    {"name": "zero-context insertion", "hunks": [("function renderProjects(", 1)], "insert": True,
     "expected": ["projects"]},
    {"name": "navigation entry edit", "hunks": [("support: 'console.manage'", 1)], "navigation": True,
     "expected": ["support"]},
    {"name": "closing brace in mapped hunk", "hunks": [("<APPENDED_BRACE>", 2)], "append_style": True,
     "expected": ["cc", "projects"]},
    {"name": "unmapped renderer refusal", "hunks": [("function unknownRenderer(", 1)], "append_renderer": True,
     "expected": "ValueError: console UI change could not be mapped to a screen at line(s) 5721; add the renderer to SCREEN_RENDERER_VIEWS or keep screen content inside its view section"},
    {"name": "extension command refusal", "hunks": [], "extension": 2,
     "expected": "CalledProcessError: Command '['git', 'diff', '--quiet', 'origin/develop...HEAD', '--', 'aiflow/v4/control/ui/system-check.js']' returned non-zero exit status 2."},
    {"name": "diff command refusal", "hunks": [], "diff_status": 2,
     "expected": "CalledProcessError: Command 'git diff --no-index' returned non-zero exit status 2."},
    {"name": "merge-base command refusal", "hunks": [], "merge_status": 2,
     "expected": "CalledProcessError: Command '['git', 'merge-base', 'origin/develop', 'HEAD']' returned non-zero exit status 2."},
    {"name": "asset listing command refusal", "hunks": [], "asset_status": 2,
     "expected": "CalledProcessError: Command '['git', 'diff', '--name-only', 'origin/develop...HEAD', '--', 'aiflow/v4/control/ui/']' returned non-zero exit status 2."},
]


def _line(lines: list[str], needle: str) -> int:
    if needle == "<END>":
        return len(lines) + 1
    if needle == "<BLANK>":
        return next(i for i, line in enumerate(lines, 1) if not line.strip())
    if needle == "<APPENDED_BRACE>":
        return len(lines) - 1
    return next(i for i, line in enumerate(lines, 1) if needle in line)


def _rules(run=subprocess.run) -> docsite_views.ViewMappingRules:
    return docsite_views.ViewMappingRules(
        root=docsite.ROOT,
        ui_dir=docsite.UI_DIR,
        extension_views=docsite.UI_EXTENSION_VIEWS,
        asset_views=docsite.SCREEN_ASSET_VIEWS,
        layout_views=docsite.LAYOUT_SCREEN_VIEWS,
        renderer_views=docsite.SCREEN_RENDERER_VIEWS,
        renderer_line_views=docsite.SCREEN_RENDERER_LINE_VIEWS,
        view_ui_patterns=docsite.SCREEN_VIEW_UI_PATTERNS,
        view_array_loop=docsite.SCREEN_VIEW_ARRAY_LOOP,
        local_line_views=docsite.SCREEN_LOCAL_LINE_VIEWS,
        shared_ui_patterns=docsite.SCREEN_SHARED_UI_PATTERNS,
        view_patterns=docsite.SCREEN_VIEW_PATTERNS,
        line_views=docsite.SCREEN_LINE_VIEWS,
        console_views=docsite.console_views,
        function_spans=docsite._function_spans,
        changed_navigation_views=docsite._changed_navigation_views,
        escape_only_lines=docsite._escape_only_lines,
        console_page_at=docsite._console_page_at,
        console_diff=docsite._console_diff,
        run=run,
    )


def _context(source: str | None = None, base_source: str | None = None, diff: str = ""):
    source = console_template() if source is None else source
    base_source = source if base_source is None else base_source
    all_views = set(docsite.console_views(docsite.UI_DIR))
    return docsite_views._console_context(source, base_source, diff, all_views, _rules())


def _case_diff(case: dict, source_lines: list[str], base_lines: list[str]) -> str:
    if "delete" in case:
        line_no = _line(base_lines, case["delete"])
        if case.get("legacy"):
            base_lines[line_no - 1] = '<section class="view" id="view-legacy">'
        elif case.get("shared_delete"):
            base_lines[line_no - 1] = (
                "try { var th = localStorage.getItem('aiflow4-theme'); if (th) "
                "document.documentElement.setAttribute('data-theme', th); } catch (x) { /* storage off */ }"
            )
        return f"@@ -{line_no},1 +{line_no},0 @@\n"
    hunks = []
    for needle, count in case.get("hunks", []):
        line_no = _line(source_lines, needle)
        if case.get("insert"):
            hunks.append(f"@@ -{line_no},0 +{line_no},{count} @@\n")
        else:
            hunks.append(f"@@ -{line_no},{count} +{line_no},{count} @@\n")
    return "".join(hunks)


def _case_inputs(case: dict) -> tuple[list[str], list[str], str]:
    source = console_template()
    if case.get("append_style"):
        source += "}\n.toasts{color:red}\n"
    if case.get("append_renderer"):
        source += "function unknownRenderer() {\n  return 1;\n}\n"
    source_lines = source.splitlines()
    base_lines = source_lines.copy()
    if case.get("navigation"):
        line_no = _line(source_lines, "support: 'console.manage'")
        base_lines[line_no - 1] = base_lines[line_no - 1].replace(", support: 'console.manage'", "")
    return source_lines, base_lines, _case_diff(case, source_lines, base_lines)


def _fake_git_run(command, case: dict, diff: str, events: list[str], check: bool = False):
    if command[1] == "merge-base":
        events.append("merge-base")
        status = case.get("merge_status", 0)
        if check and status:
            raise subprocess.CalledProcessError(status, command)
        return subprocess.CompletedProcess(command, status, stdout="BASE\n", stderr="")
    if command[1] == "diff" and "--quiet" in command:
        events.append("extension")
        extension = case.get("extension", "")
        status = 2 if extension == 2 else 1 if extension and command[-1].endswith(extension) else 0
        return subprocess.CompletedProcess(command, status, stdout="", stderr="")
    if command[1] == "diff" and "--no-index" in command:
        events.append("no-index")
        status = case.get("diff_status", 1 if diff else 0)
        return subprocess.CompletedProcess(command, status, stdout=diff, stderr="")
    if command[1] == "diff" and "--name-only" in command:
        events.append("asset-list")
        status = case.get("asset_status", 0)
        if check and status:
            raise subprocess.CalledProcessError(status, command)
        return subprocess.CompletedProcess(command, status, stdout=case.get("assets", ""), stderr="")
    raise AssertionError(f"unexpected command: {command}")


def _expected_events(case: dict) -> list[str]:
    if case.get("extension") == 2:
        return ["extension"]
    if case.get("merge_status"):
        return EXPECTED_EVENTS[:4]
    if case.get("diff_status") == 2:
        return EXPECTED_EVENTS[:-2]
    return EXPECTED_EVENTS


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_changed_views_matches_pre_refactor_characterization(monkeypatch, case):
    source_lines, base_lines, diff = _case_inputs(case)
    events = []

    def fake_run(command, **kwargs):
        return _fake_git_run(command, case, diff, events, check=kwargs.get("check", False))

    monkeypatch.setattr(docsite.subprocess, "run", fake_run)

    original_console_views = docsite.console_views

    def tracked_console_views(ui_dir=docsite.UI_DIR):
        events.append("console-views")
        return original_console_views(ui_dir)

    def page_at(ref):
        events.append(f"page:{ref}")
        return "\n".join(base_lines if ref == "BASE" else source_lines)

    monkeypatch.setattr(docsite, "_console_page_at", page_at)
    monkeypatch.setattr(docsite, "console_views", tracked_console_views)

    try:
        result = docsite.changed_views("origin/develop")
    except Exception as exc:  # snapshots include the existing refusal and subprocess error surfaces
        actual = f"{type(exc).__name__}: {exc}"
    else:
        actual = sorted(result)
    assert actual == case["expected"]
    assert events == _expected_events(case)


def test_extension_view_helper_uses_only_changed_extensions():
    def fake_run(command, **kwargs):
        status = 1 if command[-1].endswith("system-check.js") else 0
        return subprocess.CompletedProcess(command, status)

    assert docsite_views._extension_changed_views("base", _rules(fake_run)) == {"system-check", "projects"}


def test_console_context_helper_collects_view_and_security_spans():
    context = _context(diff="@@ -1 +1 @@\n-escape\n+escape\n")
    assert "projects" in context.all_views
    assert context.hints_start is not None and context.hints_end is not None
    assert context.functions and context.spans
    assert isinstance(context.security_lines, set)


def test_removed_hunk_helper_preserves_duplicate_and_shared_line_rules():
    rules = _rules()
    source = console_template()
    lines = source.splitlines()
    machine_line = _line(lines, 'id="view-machines"')
    all_views = set(docsite.console_views(docsite.UI_DIR))
    duplicate = docsite_views._console_context(source, source, "", all_views, rules)
    assert docsite_views._skip_removed_hunk(machine_line, 0, duplicate, rules) == (True, set())

    base_lines = lines.copy()
    base_lines[machine_line - 1] = '<section class="view" id="view-legacy">'
    legacy = docsite_views._console_context(source, "\n".join(base_lines), "", all_views, rules)
    assert docsite_views._skip_removed_hunk(machine_line, 0, legacy, rules) == (True, {"legacy"})


def test_layout_markup_helper_recognizes_layout_hooks():
    assert docsite_views._layout_markup('id="layout-live"')
    assert docsite_views._layout_markup('<div data-block="tickets">')
    assert not docsite_views._layout_markup('<div data-block="first-run-setup">')
    assert not docsite_views._layout_markup("plain screen text")


def test_layout_style_helper_recognizes_layout_css():
    assert docsite_views._layout_style(".block-cell{display:grid}")
    assert docsite_views._layout_style("@media (pointer:coarse)")
    assert not docsite_views._layout_style(".toasts{display:flex}")


def test_layout_region_helper_includes_regions_and_matching_markup():
    context = _context()
    lines = context.lines
    region_line = next(i for i, line in enumerate(lines, 1) if line.startswith("/* ---------- customizable layout"))
    assert docsite_views._is_layout_ui(context, region_line, lines[region_line - 1])
    first_run = _line(lines, 'data-block="first-run-setup"')
    assert not docsite_views._is_layout_ui(context, first_run, lines[first_run - 1])


def test_specific_line_helper_maps_section_and_navigation_views():
    context = _context()
    line_no = _line(context.lines, 'id="view-projects"')
    line, specific, navigation = docsite_views._specific_line_views(
        context, line_no, context.lines[line_no - 1], line_no, 1, line_no, 1, _rules(),
    )
    assert "projects" in line and not specific and not navigation


def test_renderer_helper_maps_named_and_refuses_unregistered_functions():
    context = _context()
    line_no = _line(context.lines, "function renderProjects(")
    assert docsite_views._renderer_line_views(context, line_no, context.lines[line_no - 1], set(), _rules()) == (
        {"projects"}, False,
    )
    source = console_template() + "function unknownRenderer() {\n  return 1;\n}\n"
    context = _context(source)
    line_no = _line(context.lines, "function unknownRenderer(")
    assert docsite_views._renderer_line_views(context, line_no, context.lines[line_no - 1], set(), _rules()) == (
        set(), True,
    )


def test_help_hint_helper_keeps_hint_edits_local_and_plumbing_shared():
    context = _context()
    hint_line = _line(context.lines, "  cc: '")
    views, hint = docsite_views._apply_help_hints(context, hint_line, context.lines[hint_line - 1], set(), False,
                                                   docsite.LAYOUT_SCREEN_VIEWS)
    assert views == {"cc"} and hint == "cc"
    declaration = _line(context.lines, "var HINTS = {")
    views, hint = docsite_views._apply_help_hints(context, declaration, context.lines[declaration - 1], set(), False,
                                                   docsite.LAYOUT_SCREEN_VIEWS)
    assert views == set(context.all_views) and hint is None


def test_screen_line_rules_add_explicit_machine_and_escalation_views():
    rules = _rules()
    assert docsite_views._apply_screen_line_rules("rh-form", set(), set(), set(), set(), rules) == {"machines"}
    assert docsite_views._apply_screen_line_rules("esc: { data:", set(), set(), set(), set(), rules) == {
        "cc", "machines",
    }


def test_console_line_classifier_maps_a_section_line():
    context = _context()
    line_no = _line(context.lines, 'id="view-projects"')
    assert docsite_views._classify_console_line(context, line_no, line_no, 1, line_no, 1, _rules()) == (
        {"projects"}, False,
    )


def test_hunk_mapper_updates_touched_views_without_refusal():
    context = _context()
    line_no = _line(context.lines, 'id="view-projects"')
    touched: set[str] = set()
    unmapped: set[int] = set()
    header = f"@@ -{line_no},1 +{line_no},1 @@"
    match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
    docsite_views._map_hunk(match, context, touched, unmapped, _rules())
    assert touched == {"projects"} and not unmapped


def test_unmapped_error_helper_preserves_sorted_message():
    docsite_views._raise_unmapped(set())
    with pytest.raises(ValueError, match=r"line\(s\) 2, 5"):
        docsite_views._raise_unmapped({5, 2})
