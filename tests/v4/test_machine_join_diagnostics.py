"""Service-free tests for the sanitized machine join diagnostics."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TEST_SCRIPT = Path(__file__).with_name("browser_e2e") / "machine_join_diagnostics.test.cjs"


def test_machine_join_diagnostics():
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js to test the machine join diagnostics")
    result = subprocess.run([node, "--test", str(TEST_SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
