"""Static guards for the Command Center console: server text must be escaped before it reaches HTML."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
SRC = CONSOLE.read_text(encoding="utf-8")


SAFE_HREF = ("esc(safeHref(", "esc(safeUrl(", "esc(jiraUrl(", "esc(jira)", "esc(leadReportHref(")


def _lines():
    for n, line in enumerate(SRC.splitlines(), 1):
        yield n, line


def test_no_dangerous_sinks():
    assert "document.write" not in SRC
    assert "insertAdjacentHTML" not in SRC
    assert "javascript:" not in SRC.lower()
    # only static, data-free inline handlers are allowed
    handlers = re.findall(r"\son[a-z]+=[\"']([^\"']*)", SRC)
    assert set(handlers) <= {"this.select()", "return false"}, handlers


def test_href_and_src_values_are_escaped_and_validated():
    bad = []
    for n, line in _lines():
        for m in re.finditer(r"""\b(href|src)="'\s*\+\s*(.+?)\s*\+\s*'""", line):
            expr = m.group(2)
            if not expr.startswith(SAFE_HREF):
                bad.append((n, expr[:80]))
    assert not bad, f"href/src built from unvalidated values: {bad}"


def test_attribute_values_from_data_are_escaped():
    risky = re.compile(r"""data-[a-z-]+="'\s*\+\s*((?:[a-z]\w*\.)+(?:id|key|name|version|proposal_id|ticket|status|title)\b)\s*\+\s*'""")
    bad = [(n, m.group(1)) for n, line in _lines() for m in risky.finditer(line)]
    assert not bad, f"unescaped data-* attribute values: {bad}"


def test_free_text_not_concatenated_raw_into_kpi_or_markup():
    # node / service names are joined into KPI sub text, which is inserted as HTML
    for n, line in _lines():
        if "kpi(" in line and ".name; }).join(" in line:
            assert "esc(" in line.split(".name; })")[0].rsplit("kpi(", 1)[-1], f"line {n}"
    assert not re.search(r"""<title>'\s*\+\s*(day|d)\s*\+""", SRC)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_esc_and_safe_href_behaviour():
    esc = re.search(r"^function esc\(s\).*$", SRC, re.M).group(0)
    safe = re.search(r"^function safeHref\(u\).*$", SRC, re.M).group(0)
    script = (
        esc
        + safe
        + """
const assert = require('assert');
assert.strictEqual(esc('<img src=x onerror="a()">&\\''), '&lt;img src=x onerror=&quot;a()&quot;&gt;&amp;&#39;');
assert.strictEqual(safeHref('javascript:alert(1)'), '');
assert.strictEqual(safeHref(' javascript:alert(1)'), '');
assert.strictEqual(safeHref('data:text/html,x'), '');
assert.strictEqual(safeHref('//evil.example'), '');
assert.strictEqual(safeHref('https://github.com/a/b/pull/1'), 'https://github.com/a/b/pull/1');
assert.strictEqual(safeHref('/o2/'), '/o2/');
"""
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, timeout=30)
