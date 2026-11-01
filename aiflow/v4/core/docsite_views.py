"""Mapping rules used by the documentation site's console diff check."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Pattern


@dataclass(frozen=True)
class ViewMappingRules:
    root: Path
    ui_dir: Path
    extension_views: dict[str, set[str]]
    asset_views: dict[str, tuple[str, ...]]
    layout_views: frozenset[str]
    renderer_views: dict[str, tuple[str, ...] | None]
    renderer_line_views: tuple[tuple[Pattern[str], tuple[str, ...]], ...]
    view_ui_patterns: tuple[Pattern[str], ...]
    view_array_loop: Pattern[str]
    local_line_views: tuple[tuple[Pattern[str], str], ...]
    shared_ui_patterns: tuple[Pattern[str], ...]
    view_patterns: tuple[tuple[Pattern[str], tuple[str, ...]], ...]
    line_views: tuple[tuple[Pattern[str], str], ...]
    console_views: Callable[[Path], list[str]]
    function_spans: Callable[[list[str]], dict[str, tuple[int, int]]]
    changed_navigation_views: Callable[[str, str, set[str]], set[str]]
    escape_only_lines: Callable[[str], set[int]]
    console_page_at: Callable[[str], str]
    console_diff: Callable[[str, str], str]
    run: Callable[..., Any]


@dataclass(frozen=True)
class ConsoleContext:
    lines: list[str]
    base_lines: list[str]
    all_views: set[str]
    spans: list[tuple[int, int, str]]
    functions: dict[str, tuple[int, int]]
    hints_start: int | None
    hints_end: int | None
    layout_start: int | None
    layout_end: int | None
    help_start: int | None
    help_end: int
    security_lines: set[int]


def _extension_changed_views(base: str, rules: ViewMappingRules) -> set[str]:
    views: set[str] = set()
    for name, extension_views in rules.extension_views.items():
        path = (rules.ui_dir / name).relative_to(rules.root).as_posix()
        result = rules.run(["git", "diff", "--quiet", f"{base}...HEAD", "--", path],
                           cwd=rules.root, check=False)
        if result.returncode == 1:
            views |= extension_views
        elif result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args)
    return views


def _console_context(source: str, base_source: str, diff: str, all_views: set[str],
                     rules: ViewMappingRules) -> ConsoleContext:
    lines = source.splitlines()
    base_lines = base_source.splitlines()
    hints_start = next((i for i, line in enumerate(lines) if line.startswith("var HINTS = {")), None)
    hints_end = next((i for i in range(hints_start + 1, len(lines)) if lines[i] == "};"), None) \
        if hints_start is not None else None
    spans = []
    for i, line in enumerate(lines):
        if match := re.search(r'<section class="view" id="view-([a-z-]+)"', line):
            end = next((j for j in range(i, len(lines)) if "</section>" in lines[j]), i)
            spans.append((i + 1, end + 1, match.group(1)))
    functions = rules.function_spans(lines)
    layout_start = next((i + 1 for i, line in enumerate(lines)
                         if line.startswith("/* ---------- customizable layout")), None)
    layout_end = next((i + 1 for i, line in enumerate(lines)
                       if line.startswith("/* ---------- interactions")), None)
    help_start = next((i for i, line in enumerate(lines) if line.startswith("/* Help: every screen")), None)
    help_end = functions.get("renderHelp", (0, 0))[1] - 1
    security_lines = rules.escape_only_lines(diff)
    csrf_start = next((i for i, line in enumerate(lines)
                       if line.startswith("/* Cross-site request protection")), None)
    if csrf_start is not None:
        csrf_end = next((j for j in range(csrf_start, len(lines)) if lines[j] == "})();"), csrf_start)
        security_lines |= set(range(csrf_start + 1, csrf_end + 2))
    sec_start = next((i for i, line in enumerate(lines) if line.startswith("/* ---------- security:")), None)
    if sec_start is not None:
        sec_end = next((j for j in range(sec_start + 1, len(lines))
                        if lines[j].startswith("/* ---------- ")), len(lines))
        security_lines |= set(range(sec_start + 1, sec_end + 1))
    return ConsoleContext(lines, base_lines, all_views, spans, functions, hints_start, hints_end,
                          layout_start, layout_end, help_start, help_end, security_lines)


def _skip_removed_hunk(old_first: int, count: int, context: ConsoleContext,
                       rules: ViewMappingRules) -> tuple[bool, set[str]]:
    if count != 0 or not 1 <= old_first <= len(context.base_lines):
        return False, set()
    old_line = context.base_lines[old_first - 1]
    removed_section = re.search(r'<section class="view" id="view-([a-z]+)"', old_line)
    if removed_section:
        view = removed_section.group(1)
        return True, {view} if view not in context.all_views else set()
    if any(pattern.search(old_line) for pattern in rules.shared_ui_patterns):
        return True, set(context.all_views)
    return False, set()


def _layout_markup(line: str) -> bool:
    return (
        'id="layout-live"' in line
        or ('data-block="' in line and 'data-block="first-run-setup"' not in line)
        or "layouts: {" in line
        or "S.layouts." in line
        or "layoutAction(act" in line
        or "applyLayout('delivery'" in line
        or bool(re.search(r"\bloadLayouts\(\)", line))
    )


def _layout_style(line: str) -> bool:
    return bool(re.search(
        r"customizable layout|\.blocks|\.block-cell|\.layout-bar|\.layout-hidden|"
        r"\.block-tools|\.block-sizes|\.block-handle|\.sr-only|dashboard-layout-headings|"
        r"@media \(pointer:coarse\)", line,
    ))


def _is_layout_ui(context: ConsoleContext, line_no: int, line: str) -> bool:
    in_layout_region = (context.layout_start is not None and context.layout_end is not None
                        and context.layout_start <= line_no <= context.layout_end)
    return in_layout_region or _layout_markup(line) or _layout_style(line)


def _specific_line_views(context: ConsoleContext, line_no: int, line: str, old_first: int,
                         old_count: int, first: int, count: int,
                         rules: ViewMappingRules) -> tuple[set[str], set[str], set[str]]:
    line_views = {view for lo, hi, view in context.spans if lo <= line_no <= hi}
    navigation_views: set[str] = set()
    if old_count == count and 1 <= old_first <= len(context.base_lines):
        old_index = old_first - 1 + (line_no - first)
        if old_index < len(context.base_lines):
            navigation_views = rules.changed_navigation_views(
                context.base_lines[old_index], line, context.all_views,
            )
    specific_views = {
        match.group(1)
        for pattern in rules.view_ui_patterns
        if not (pattern is rules.view_array_loop and navigation_views)
        and (match := pattern.search(line))
        and match.group(1) in context.all_views
    }
    specific_views |= navigation_views
    specific_views |= {view for pattern, view in rules.local_line_views if pattern.search(line)}
    return line_views, specific_views, navigation_views


def _renderer_line_views(context: ConsoleContext, line_no: int, line: str,
                         specific_views: set[str], rules: ViewMappingRules) -> tuple[set[str], bool]:
    line_views: set[str] = set()
    unmapped_renderer = False
    for name, (lo, hi) in context.functions.items():
        if lo <= line_no <= hi:
            if name not in rules.renderer_views:
                unmapped_renderer = True
                break
            mapped = rules.renderer_views[name]
            line_views |= ((specific_views or context.all_views) if mapped is None else set(mapped))
    if unmapped_renderer:
        line_views = set()
    for pattern, mapped in rules.renderer_line_views:
        if pattern.search(line):
            line_views = set(mapped)
    if ("editing: '', temporaryPassword" in line
            or ("S.view === 'users'" in line and "loadUsers(" in line)):
        line_views = {"users"}
    return line_views, unmapped_renderer


def _apply_help_hints(context: ConsoleContext, line_no: int, line: str,
                      line_views: set[str], is_layout: bool,
                      layout_views: frozenset[str]) -> tuple[set[str], str | None]:
    hint_view = None
    if is_layout:
        line_views = set(layout_views)
    if context.hints_start is not None and context.hints_end is not None \
            and context.hints_start <= line_no - 1 <= context.hints_end:
        hint = re.match(r"\s*['\"]?([a-z-]+)['\"]?:\s*'", line)
        if hint:
            hint_view = hint.group(1)
            line_views.add(hint_view)
        else:
            line_views |= context.all_views
    if context.help_start is not None and context.help_start <= line_no - 1 <= context.help_end \
            and hint_view is None:
        line_views |= context.all_views
    return line_views, hint_view


def _apply_screen_line_rules(line: str, line_views: set[str], specific_views: set[str],
                             navigation_views: set[str], all_views: set[str],
                             rules: ViewMappingRules) -> set[str]:
    shared_ui_change = any(pattern.search(line) for pattern in rules.shared_ui_patterns)
    if shared_ui_change and not (specific_views or navigation_views):
        line_views |= all_views
    for pattern, mapped in rules.view_patterns:
        if pattern.search(line):
            line_views |= set(mapped)
    if "rh-form" in line:
        line_views.add("machines")
    for pattern, view in rules.line_views:
        if pattern.search(line):
            line_views.add(view)
    if "A signed-in person whose roles grant review.actions" in line:
        line_views.add("review")
    return line_views


def _classify_console_line(context: ConsoleContext, line_no: int, old_first: int,
                           old_count: int, first: int, count: int,
                           rules: ViewMappingRules) -> tuple[set[str], bool]:
    line = context.lines[line_no - 1] if line_no <= len(context.lines) else ""
    line_views, specific_views, navigation_views = _specific_line_views(
        context, line_no, line, old_first, old_count, first, count, rules,
    )
    is_layout = _is_layout_ui(context, line_no, line)
    renderer_views, unmapped_renderer = _renderer_line_views(
        context, line_no, line, specific_views, rules,
    )
    if unmapped_renderer:
        line_views = set()
    line_views |= renderer_views
    line_views, _ = _apply_help_hints(context, line_no, line, line_views, is_layout, rules.layout_views)
    line_views = _apply_screen_line_rules(
        line, line_views, specific_views, navigation_views, context.all_views, rules,
    )
    if not line.strip():
        return set(), unmapped_renderer
    if not unmapped_renderer:
        line_views |= specific_views
    if line_no in context.security_lines or "security" in line.lower():
        line_views = (specific_views - {"security"}) or {"security"}
    return line_views, unmapped_renderer


def _map_hunk(match: re.Match[str], context: ConsoleContext, touched: set[str],
              unmapped: set[int], rules: ViewMappingRules) -> None:
    old_first, old_count = int(match.group(1)), int(match.group(2) or 1)
    first, count = int(match.group(3)), int(match.group(4) or 1)
    skip, removed_views = _skip_removed_hunk(old_first, count, context, rules)
    if skip:
        touched |= removed_views
        return
    hunk_touched: set[str] = set()
    hunk_unmapped: set[int] = set()
    last = first + max(count, 1) - 1
    for line_no in range(max(first, 1), last + 1):
        line_views, _ = _classify_console_line(
            context, line_no, old_first, old_count, first, count, rules,
        )
        line = context.lines[line_no - 1] if line_no <= len(context.lines) else ""
        if line_views:
            touched |= line_views
            hunk_touched |= line_views
        elif line.strip():
            unmapped.add(line_no)
            hunk_unmapped.add(line_no)
    if hunk_touched:
        for line_no in hunk_unmapped:
            if line_no <= len(context.lines) and context.lines[line_no - 1].strip() == "}":
                unmapped.discard(line_no)


def _raise_unmapped(unmapped: set[int]) -> None:
    if unmapped:
        lines_text = ", ".join(str(number) for number in sorted(unmapped))
        raise ValueError(
            "console UI change could not be mapped to a screen at line(s) " + lines_text
            + "; add the renderer to SCREEN_RENDERER_VIEWS or keep screen content inside its view section"
        )


def map_changed_views(base: str, rules: ViewMappingRules) -> set[str]:
    extension_views = _extension_changed_views(base, rules)
    merge_base = rules.run(["git", "merge-base", base, "HEAD"], cwd=rules.root, text=True,
                           capture_output=True, check=True).stdout.strip()
    source = rules.console_page_at("HEAD")
    base_source = rules.console_page_at(merge_base)
    diff = rules.console_diff(base_source, source)
    all_views = set(rules.console_views(rules.ui_dir))
    asset_diff = rules.run(["git", "diff", "--name-only", f"{base}...HEAD", "--", "aiflow/v4/control/ui/"],
                           cwd=rules.root, text=True, capture_output=True, check=True).stdout
    touched = set().union(*(rules.asset_views.get(path.strip(), ()) for path in asset_diff.splitlines()))
    context = _console_context(source, base_source, diff, all_views, rules)
    unmapped: set[int] = set()
    for match in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M):
        _map_hunk(match, context, touched, unmapped, rules)
    _raise_unmapped(unmapped)
    return touched | extension_views
