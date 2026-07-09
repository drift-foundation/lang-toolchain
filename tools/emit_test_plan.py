#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Emit a drift_test_run plan for the toolchain's uniform pytest lanes.

This is the toolchain's own reference plan-emitter (the pattern drift-web's
report flagged as the real adoption cost: see doc/test-run.md).  It replaces the
~12 hand-written, byte-identical `lang-<suite>-test` recipes — each carrying a
duplicated pytest/xdist preamble — with one data-driven emitter + a single
`drift_test_run` invocation.

Scope (pilot): the UNIFORM pytest lanes only — those that differ solely by test
directory and run as `pytest -n <budget> --dist=worksteal -v <dir>`.  The two
non-uniform shard-1 lanes stay as recipes for now:
  - `driver` — has its own DRIVER_JOBS budget-override chain;
  - `gdb`    — single-process (no -n), env-gated by DRIFT_GDB_TEST=1.

Each lane becomes a `mode: serial` job on a shared host key, so flocker runs one
lane at a time host-wide (pytest is a black box that multiprocesses internally);
the lane's internal xdist width comes from `{jobs}` (the drift_test_run budget,
DRIFT_TEST_JOBS / physical-core default).  This mirrors how a daemon-like or
internally-parallel compiler job is treated: flocker counts the lane, not its
children.

Usage:
  tools/emit_test_plan.py --plan-out PATH
  # then:
  tools/drift_test_run.py --plan PATH --work-dir build/test-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Uniform shard-1 pytest lanes: (job id, test target dir).  Each maps 1:1 to a
# former `lang-<id>-test` recipe whose only distinguishing content was the dir.
# Keep this list as the single source of truth for the uniform lanes.
UNIFORM_LANES: tuple[tuple[str, str], ...] = (
	("stage1", "lang/tests/stage1"),
	("stage2", "lang/tests/stage2"),
	("stage3", "lang/tests/stage3"),
	("stage4", "lang/tests/stage4"),
	("parser", "lang/tests/parser"),
	("core", "lang/tests/core"),
	# NOTE: lang/codegen/llvm/tests is owned by the `lang-codegen-test` recipe
	# (kept), NOT here.  The separate `lang-llvm-test` recipe runs a llvmlite
	# smoke SCRIPT (tools/test-llvm/test_codegen.py), not pytest — also kept as
	# a recipe.  Neither belongs in this uniform-pytest set.
	("borrow", "lang/tests/borrow_checker"),
	("type_checker", "lang/tests/type_checker"),
	("method_registry", "lang/tests/method_registry"),
	("packages", "lang/tests/packages"),
	("traits", "lang/tests/traits"),
	# Repo-hygiene static audits (top-level lang/tests/*.py). These were
	# in NO gate leg before 2026-07-08 — the tmp-root audit silently
	# drifted red while /tmp hard-codings accumulated, and the tmpfs
	# ENOSPC incident followed. A lane target can be a file, not just a
	# directory; pytest collects it the same way.
	("repo_audits", "lang/tests/test_tmp_root_compliance.py lang/tests/test_drift_tmp_session_root.py"),
)

_PY = "./.venv/bin/python3"


def _preflight() -> None:
	"""The former per-recipe preamble, run ONCE here instead of 12x.

	Fails fast if pytest is missing; warns (does not fail) if xdist is missing,
	matching the old recipes' fallback intent — though the emitted plan always
	uses `-n {jobs}` (xdist is a hard dep of the parallel gate).  A team running
	without xdist should install it; the emitter surfaces that here rather than
	letting each lane discover it.
	"""
	r = subprocess.run([_PY, "-m", "pytest", "--version"],
	                   capture_output=True, text=True)
	if r.returncode != 0:
		sys.exit("emit_test_plan: pytest missing in .venv "
		         "(install: ./.venv/bin/python3 -m pip install pytest)")
	r = subprocess.run([_PY, "-c", "import xdist"], capture_output=True, text=True)
	if r.returncode != 0:
		print("emit_test_plan: warning: pytest-xdist missing; the plan uses "
		      "-n <jobs> and will fail without it "
		      "(install: ./.venv/bin/python3 -m pip install pytest-xdist)",
		      file=sys.stderr)


def build_plan() -> dict:
	jobs = []
	for jid, test_dir in UNIFORM_LANES:
		jobs.append({
			"id": jid,
			# pytest is a serial flocker black box (one lane at a time on the
			# shared host group); it fans out internally to {jobs} xdist workers.
			"mode": "serial",
			"group": "pytest-host",
			"cmd": [
				_PY, "-m", "pytest",
				"-n", "{jobs}", "--dist=worksteal", "-v",
				# A lane target may be several whitespace-separated
				# paths (dirs or files) — each becomes its own argv
				# element for pytest collection.
				*test_dir.split(),
			],
		})
	return {
		"name": "uniform-pytest-lanes",
		"phases": [{"name": "suite", "jobs": jobs}],
	}


def main(argv: list[str] | None = None) -> int:
	p = argparse.ArgumentParser(
		prog="emit_test_plan.py",
		description="Emit a drift_test_run plan for the uniform pytest lanes.",
	)
	p.add_argument("--plan-out", type=Path, required=True,
	               help="Write the JSON plan to this path (never stdout — stdout "
	                    "carries tool noise; the plan is a file).")
	p.add_argument("--skip-preflight", action="store_true",
	               help="Skip the pytest/xdist availability check.")
	args = p.parse_args(argv)

	if not args.skip_preflight:
		_preflight()

	plan = build_plan()
	args.plan_out.parent.mkdir(parents=True, exist_ok=True)
	args.plan_out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
	print(f"emit_test_plan: wrote {len(plan['phases'][0]['jobs'])}-job plan "
	      f"to {args.plan_out}", flush=True)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
