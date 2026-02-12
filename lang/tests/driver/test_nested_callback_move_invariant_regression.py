# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_nested_callback_move_case_runs_after_fix() -> None:
	cmd = [
		str(Path(sys.executable)),
		"lang/tests/codegen/e2e/runner.py",
		"callback_move_capture_nested_callback",
		"--jobs",
		"1",
	]
	res = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[3])
	assert res.returncode == 0
	assert "callback_move_capture_nested_callback: ok" in res.stdout
