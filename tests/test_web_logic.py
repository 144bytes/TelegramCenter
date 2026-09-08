"""The interface's own logic tests, run with the rest of the suite.

web/tests holds the parts of the interface that decide something without
React - a window's draft over a live record, when a check may start.
`npm test` compiles them with the TypeScript the build already uses and runs
them on Node's own test runner: no extra packages. Skipped where the web
tools are not installed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def test_the_interface_logic_tests_pass():
    npm = shutil.which("npm")
    if npm is None or not (WEB / "node_modules").is_dir():
        pytest.skip("the web tools are not installed (npm ci in web/)")

    done = subprocess.run([npm, "test", "--silent"], cwd=WEB, capture_output=True,
                          encoding="utf-8", errors="replace", timeout=300)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "# fail 0" in done.stdout
