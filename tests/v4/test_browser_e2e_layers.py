"""Service-free tests for the Node browser scenario layers."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TEST_SCRIPT = Path(__file__).with_name("browser_e2e_layers.test.cjs")


def test_browser_e2e_layer_functions():
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node.js to test the browser scenario layers")
    result = subprocess.run([node, "--test", str(TEST_SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
