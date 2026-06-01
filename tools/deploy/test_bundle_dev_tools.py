# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: `bundle_dev_tools` ships the CI scripts into dist/lib/tools.

The 0.33.14 bundle shipped doc/test-run.md but NOT the tool it documents
(drift_test_run.py / pytest_jobs.py were source-only, absent from the bundle),
so teams couldn't consume the runner from a staged toolchain.  This pins that
both scripts land under lib/tools/ with their drift_ names, executable, and that
the runner can locate its bin/ siblings + budget helper from that layout.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from tools.deploy.steps.bundle import DEV_TOOLS, bundle_dev_tools


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bundle_dev_tools_ships_runner_and_budget_into_lib_tools() -> None:
	with tempfile.TemporaryDirectory() as td:
		dist = Path(td) / "dist"
		dist.mkdir()
		bundle_dev_tools(_REPO_ROOT, dist)

		tools_dir = dist / "lib" / "tools"
		assert tools_dir.is_dir(), "lib/tools/ not created"

		runner = tools_dir / "drift_test_run.py"
		budget = tools_dir / "drift_pytest_jobs.py"
		assert runner.exists(), "drift_test_run.py missing from lib/tools/"
		assert budget.exists(), "drift_pytest_jobs.py missing from lib/tools/"

		# Executable bit set (chmod 0o755).
		for f in (runner, budget):
			assert os.stat(f).st_mode & stat.S_IXUSR, f"{f.name} not executable"

		# Every declared DEV_TOOLS dest is present (guards future additions).
		for _src, dest_name in DEV_TOOLS:
			assert (tools_dir / dest_name).exists(), f"{dest_name} not bundled"


def test_bundle_dev_tools_NOT_in_bin() -> None:
	"""CI tools are not PATH artifacts — they must not leak into bin/."""
	with tempfile.TemporaryDirectory() as td:
		dist = Path(td) / "dist"
		dist.mkdir()
		bundle_dev_tools(_REPO_ROOT, dist)
		# bin/ should not exist at all from this step (it only writes lib/tools).
		assert not (dist / "bin" / "drift_test_run.py").exists()
		assert not (dist / "bin" / "drift_test_run").exists()


def test_runner_resolves_bin_siblings_from_lib_tools_layout() -> None:
	"""From lib/tools/, the runner walks up to the dist root to find bin/."""
	import sys
	sys.path.insert(0, str(_REPO_ROOT / "tools"))
	import drift_test_run as dtr

	with tempfile.TemporaryDirectory() as td:
		dist = Path(td) / "dist"
		(dist / "bin").mkdir(parents=True)
		(dist / "lib" / "tools").mkdir(parents=True)
		(dist / "bin" / "flocker").write_text("#!/bin/sh\n", encoding="utf-8")
		script_dir = dist / "lib" / "tools"
		assert dtr._find_toolchain_root(script_dir) == dist
