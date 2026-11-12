"""Characterize incident preview helpers before and after their source split."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from aiflow.v4.control.ui import (
    build_console_page,
    build_served_console_page,
    console_source_names,
    console_template,
)


def _helper(name: str) -> str:
    source = console_template()
    start = re.search(rf"^function {name}\(", source, re.M)
    assert start is not None, f"{name} is missing from the assembled console"
    next_function = re.search(r"^function [A-Za-z_$][\w$]*\(", source[start.end():], re.M)
    end = start.end() + next_function.start() if next_function else len(source)
    return source[start.start():end].rstrip()


def test_console_template_and_pages_keep_the_captured_base_bytes():
    expected = {
        "template": "8acc7464036e6d33813656e97e998dc57fb19a879e06f2945c5ee2536edccec3",
        "built": "0a76c31d4c7d979294056d2045a5726aea0a8409831c43f893ec85bb0facd925",
        "served": "f30ed4f50e9aae9ad1a0f71d0902e88a7280b1a397a3fa81eb69ac8ca816d77b",
    }
    outputs = {
        "template": console_template(),
        "built": build_console_page(),
        "served": build_served_console_page(),
    }
    assert {name: sha256(value.encode("utf-8")).hexdigest() for name, value in outputs.items()} == expected


def test_shared_incident_helpers_are_loaded_once_at_the_original_position():
    root = Path(__file__).parents[2] / "aiflow" / "v4" / "control" / "ui" / "console"
    names = console_source_names()
    command_center_index = names.index("075-command-center.js")
    assert names[command_center_index - 1] == "074-incident-preview.js"

    fragment = (root / "074-incident-preview.js").read_bytes()
    command_center = (root / "075-command-center.js").read_bytes()
    assert fragment.startswith(b"/* ---------- Command Center ---------- */\n")
    assert fragment.endswith(b"}\n")
    assert command_center.startswith(b"function slowVerificationsPanel(items) {")
    assert b"function slowVerificationsPanel" not in fragment

    source = console_template()
    assert source.count("function incidentHandoff(") == 1
    assert source.count("function incidentPreview(") == 1
    assert source.count("esc(incidentPreview(i))") == 2
    assert source.count("var handoff = incidentHandoff(i), b") == 1
    assert source.count(fragment.decode("utf-8")) == 1
    assert source.index(fragment.decode("utf-8")) < source.index("function slowVerificationsPanel(items)")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_incident_helpers_keep_fallbacks_and_null_exception():
    long_detail = "ordinary detail " * 20000
    script = "\n".join((
        "const assert = require('assert');",
        "function tok(value) { return 'tokens(' + value + ')'; }",
        "function mmss(value) { return 'elapsed(' + value + ')'; }",
        _helper("incidentHandoff"),
        _helper("incidentPreview"),
        "assert.strictEqual(incidentHandoff(null), null);",
        "assert.throws(() => incidentPreview(null), TypeError);",
        "assert.strictEqual(incidentHandoff({}), null);",
        "assert.strictEqual(incidentPreview({}), '');",
        "assert.strictEqual(incidentHandoff({detail: 123}), null);",
        "assert.strictEqual(incidentPreview({detail: 123}), 123);",
        "assert.strictEqual(incidentPreview({detail: 'ordinary detail'}), 'ordinary detail');",
        "assert.strictEqual(incidentPreview({detail: 'handoff-v1:{broken'}), 'handoff-v1:{broken');",
        "assert.strictEqual(incidentPreview({detail: 'handoff-v1:null'}), 'handoff-v1:null');",
        "assert.strictEqual(incidentPreview({detail: 'handoff-v1:' + JSON.stringify({schema: 'other'})}),",
        "  'handoff-v1:' + JSON.stringify({schema: 'other'}));",
        "assert.strictEqual(incidentPreview({detail: null}), '');",
        f"const longDetail = {json.dumps(long_detail)};",
        "assert.strictEqual(incidentPreview({detail: longDetail}), longDetail);",
    ))
    subprocess.run([shutil.which("node"), "-"], input=script, text=True, check=True,
                   capture_output=True, timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_incident_preview_keeps_run_selection_formatting_and_caller_escaping():
    payload = {
        "schema": "aiflow.ticket-handoff/v1",
        "streak_count": 3,
        "elapsed_ms": 65000,
        "runs": [
            {"node": "older", "provider": "old-provider", "model": "old-model", "tokens": 8,
             "cost_usd": 1.0},
            {"node": "latest", "provider": "claude", "model": "opus", "tokens": 12345,
             "cost_usd": 0.00012},
        ],
        "notification": {"state": "delivered", "configuration_gap": '<img src=x onerror="bad">'},
    }
    empty_payload = {"schema": "aiflow.ticket-handoff/v1", "streak_count": 1, "elapsed_ms": 0,
                     "runs": [], "notification": {}}
    missing_runs_payload = {"schema": "aiflow.ticket-handoff/v1", "streak_count": 4, "elapsed_ms": 2000,
                            "notification": {"state": "pending"}}
    zero_payload = {"schema": "aiflow.ticket-handoff/v1", "streak_count": 2, "elapsed_ms": 1000,
                    "runs": [{"tokens": 0, "cost_usd": 0}]}
    large_payload = {"schema": "aiflow.ticket-handoff/v1", "streak_count": 2, "elapsed_ms": 1000,
                     "runs": [{"node": "previous"} for _ in range(5000)] + [{"node": "latest"}]}
    script = "\n".join((
        "const assert = require('assert');",
        "function tok(value) { return 'tokens(' + value + ')'; }",
        "function mmss(value) { return 'elapsed(' + value + ')'; }",
        _helper("incidentHandoff"),
        _helper("incidentPreview"),
        _helper("esc"),
        f"const latest = {{detail: 'handoff-v1:' + {json.dumps(json.dumps(payload))}}};",
        "const note = 'Blocked · 3 consecutive failures · elapsed(65) · latest · claude/opus · '",
        "  + 'tokens(12345) tokens · $0.0001 · notification delivered · <img src=x onerror=\"bad\">';",
        "assert.strictEqual(incidentPreview(latest), note);",
        "assert.strictEqual(esc(incidentPreview(latest)),",
        "  'Blocked · 3 consecutive failures · elapsed(65) · latest · claude/opus · '",
        "  + 'tokens(12345) tokens · $0.0001 · notification delivered · &lt;img src=x onerror=&quot;bad&quot;&gt;');",
        f"const empty = {{detail: 'handoff-v1:' + {json.dumps(json.dumps(empty_payload))}}};",
        "assert.strictEqual(incidentPreview(empty),",
        "  'Blocked · 1 consecutive failures · elapsed(0) · unavailable · unavailable/unavailable · '",
        "  + 'unavailable tokens · unavailable · notification unavailable');",
        f"const missingRuns = {{detail: 'handoff-v1:' + {json.dumps(json.dumps(missing_runs_payload))}}};",
        "assert.strictEqual(incidentPreview(missingRuns),",
        "  'Blocked · 4 consecutive failures · elapsed(2) · unavailable · unavailable/unavailable · '",
        "  + 'unavailable tokens · unavailable · notification pending');",
        f"const zero = {{detail: 'handoff-v1:' + {json.dumps(json.dumps(zero_payload))}}};",
        "assert.strictEqual(incidentPreview(zero),",
        "  'Blocked · 2 consecutive failures · elapsed(1) · unavailable · unavailable/unavailable · '",
        "  + 'tokens(0) tokens · $0.0000 · notification unavailable');",
        f"const large = {{detail: 'handoff-v1:' + {json.dumps(json.dumps(large_payload))}}};",
        "assert.strictEqual(incidentHandoff(large).runs.length, 5001);",
        "assert.strictEqual(incidentPreview(large),",
        "  'Blocked · 2 consecutive failures · elapsed(1) · latest · unavailable/unavailable · '",
        "  + 'unavailable tokens · unavailable · notification unavailable');",
    ))
    subprocess.run([shutil.which("node"), "-"], input=script, text=True, check=True,
                   capture_output=True, timeout=30)


def test_repeated_and_concurrent_console_builds_keep_the_same_result():
    template = console_template()
    with ThreadPoolExecutor(max_workers=4) as pool:
        templates = list(pool.map(lambda _index: console_template(), range(4)))
        pages = list(pool.map(lambda _index: build_console_page(), range(4)))
    assert templates == [template] * 4
    assert pages == [build_console_page()] * 4
    assert "incidentPreview(i)" in template
