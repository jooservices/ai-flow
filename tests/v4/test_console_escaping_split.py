"""Characterize the shared console HTML and URL helpers before their source is split."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import aiflow.v4.control.ui.console_build as console_build
from aiflow.v4.control.ui import build_console_page, console_source_names, console_template


SOURCE = console_template()


def _helper(name: str) -> str:
    match = re.search(rf"^function {name}\([^\n]*$", SOURCE, re.M)
    assert match is not None, f"{name} is missing from the assembled console"
    return match.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_escaping_helpers_keep_current_string_conversion_and_html_output():
    esc = _helper("esc")
    unicode_text = "Zażółć gęślą jaźń 雪 😀"
    sensitive = "&<>\"'"
    long_text = ("雪🙂<&>\"'" * 4000)
    expected_long = "雪🙂&lt;&amp;&gt;&quot;&#39;" * 4000
    script = "\n".join((
        esc,
        "const assert = require('assert');",
        "assert.strictEqual(esc(null), '');",
        "assert.strictEqual(esc(undefined), '');",
        "assert.strictEqual(esc(''), '');",
        "assert.strictEqual(esc(0), '0');",
        "assert.strictEqual(esc(false), 'false');",
        f"assert.strictEqual(esc({json.dumps(unicode_text)}), {json.dumps(unicode_text)});",
        f"assert.strictEqual(esc({json.dumps(sensitive)}), '&amp;&lt;&gt;&quot;&#39;');",
        f"assert.strictEqual(esc({json.dumps(long_text)}), {json.dumps(expected_long)});",
    ))
    subprocess.run([shutil.which("node"), "-"], input=script, text=True, check=True,
                   capture_output=True, timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_safe_url_and_safe_href_keep_their_distinct_current_policies():
    safe_url = _helper("safeUrl")
    safe_href = _helper("safeHref")
    script = "\n".join((
        safe_url,
        safe_href,
        "const assert = require('assert');",
        "assert.strictEqual(safeUrl('http://example.test/a'), 'http://example.test/a');",
        "assert.strictEqual(safeUrl('https://example.test/a'), 'https://example.test/a');",
        "assert.strictEqual(safeUrl('HTTPS://example.test/a'), '');",
        "assert.strictEqual(safeUrl('HtTp://example.test/a'), '');",
        "assert.strictEqual(safeUrl('/relative'), '');",
        "assert.strictEqual(safeUrl('//example.test'), '');",
        "assert.strictEqual(safeUrl('/\\\\example.test'), '');",
        "assert.strictEqual(safeUrl(' https://example.test'), '');",
        "assert.strictEqual(safeUrl('javascript:alert(1)'), '');",
        "assert.strictEqual(safeUrl('data:text/html,x'), '');",
        "assert.strictEqual(safeUrl(undefined), '');",
        "assert.strictEqual(safeHref('http://example.test/a'), 'http://example.test/a');",
        "assert.strictEqual(safeHref('HtTpS://example.test/a'), 'HtTpS://example.test/a');",
        "assert.strictEqual(safeHref('/relative'), '/relative');",
        "assert.strictEqual(safeHref('/'), '/');",
        "assert.strictEqual(safeHref('//example.test'), '');",
        "assert.strictEqual(safeHref('/\\\\example.test'), '');",
        "assert.strictEqual(safeHref('\\\\example.test'), '');",
        "assert.strictEqual(safeHref(' https://example.test'), '');",
        "assert.strictEqual(safeHref('javascript:alert(1)'), '');",
        "assert.strictEqual(safeHref('data:text/html,x'), '');",
        "assert.strictEqual(safeHref(null), '');",
    ))
    subprocess.run([shutil.which("node"), "-"], input=script, text=True, check=True,
                   capture_output=True, timeout=30)


def test_escaping_fragment_is_loaded_once_in_manifest_order_inside_the_console_script():
    names = console_source_names()
    assert names[names.index("015-script-open.html") + 1] == "018-escaping.js"
    assert names[names.index("018-escaping.js") + 1] == "020-state.js"

    for source in (console_template(), build_console_page()):
        positions = [source.index(f"function {name}(") for name in ("esc", "safeUrl", "safeHref")]
        assert positions == sorted(positions)
        assert all(source.count(f"function {name}(") == 1 for name in ("esc", "safeUrl", "safeHref"))
        assert source.index("<script>") < positions[0] < source.index("var S =", positions[0])


def test_console_template_still_raises_for_a_manifest_entry_with_no_source(tmp_path: Path, monkeypatch):
    (tmp_path / "manifest.txt").write_text("missing.js\n", encoding="utf-8")
    monkeypatch.setattr(console_build, "CONSOLE_SOURCES", tmp_path)
    with pytest.raises(FileNotFoundError):
        console_template()


def test_build_console_page_still_raises_when_an_insertion_marker_is_missing(monkeypatch):
    monkeypatch.setattr(console_build, "console_template", lambda: "<html></html>")
    with pytest.raises(RuntimeError, match="console template is missing a view insertion point"):
        build_console_page()
