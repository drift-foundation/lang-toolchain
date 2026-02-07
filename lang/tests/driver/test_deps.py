from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get("DRIFT_DEPS_TEST") == "0", reason="deps test disabled via DRIFT_DEPS_TEST=0")
def test_deps_check_smoke() -> None:
	repo_root = Path(__file__).resolve().parents[3]
	script = repo_root / "tools" / "deps_check.py"
	env = os.environ.copy()
	env["PYTHONPATH"] = str(repo_root)
	res = subprocess.run([sys.executable, str(script), "--quiet"], cwd=repo_root, env=env, text=True, capture_output=True)
	assert res.returncode == 0, res.stderr
