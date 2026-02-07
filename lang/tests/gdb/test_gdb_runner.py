from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("DRIFT_GDB_TEST") != "1", reason="gdb test requires DRIFT_GDB_TEST=1")
def test_gdb_runner_smoke() -> None:
	repo_root = Path(__file__).resolve().parents[3]
	runner = repo_root / "lang" / "tests" / "gdb" / "runner.py"
	env = os.environ.copy()
	env["PYTHONPATH"] = str(repo_root)
	res = subprocess.run([sys.executable, str(runner), "lambda_captures"], cwd=repo_root, env=env, text=True, capture_output=True)
	assert res.returncode == 0, res.stderr
