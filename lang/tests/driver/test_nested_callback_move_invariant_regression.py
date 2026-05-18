# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_nested_callback_move_case_runs_after_fix() -> None:
	repo_root = Path(__file__).resolve().parents[3]
	cmd = [
		str(Path(sys.executable)),
		"lang/tests/codegen/e2e/runner.py",
		"callback_move_capture_nested_callback",
		"--jobs",
		"1",
	]
	# Inject PYTHONPATH=repo_root so the spawned `python runner.py`
	# can import `lang.*`.  Without this, sys.path[0] in the subprocess
	# is `lang/tests/codegen/e2e/` (the script's dir), `lang/` is not
	# there, and runner.py's `from lang.driftc.parser import ...` fails
	# with `ModuleNotFoundError: No module named 'lang'`.
	env = {**os.environ, "PYTHONPATH": str(repo_root)}
	res = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root, env=env)
	assert res.returncode == 0
	assert "callback_move_capture_nested_callback: ok" in res.stdout
