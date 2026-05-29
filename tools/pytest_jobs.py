#!/usr/bin/env python3
"""Print the recommended `pytest -n` worker count.

Override order (highest to lowest):
  1. `DRIFT_TEST_JOBS` env var — the unified test-parallelism knob.
     Set this to control concurrency across every test runner the just
     recipes invoke (pytest-managed suites, codegen e2e runner, package
     consumer runner, pex e2e runner). Accepts a positive integer.
  2. Protocol default: `ceil(nproc / 2)` — each test lane self-limits to
     half the host's cores. `nproc` is the physical core count from
     /proc/cpuinfo (logical `os.cpu_count()` as fallback).

Why half, not all: lanes do not run in isolation. The cross-lane /
cross-process budget is enforced by `flocker` (see docs/flocker.md), a
host-local slot cap keyed per resource. With each lane at ceil(nproc/2)
and flocker capping the shared pool, several lanes (plain / ASAN /
valgrind) can run concurrently without their per-lane counts multiplying
past the host's RAM. A lane that claimed all cores would oversubscribe
the moment a second lane started.

The justfile pipes this script's output into `pytest -n` for every
parallel pytest recipe, so a single env var change here propagates to
all 12 pytest invocations without per-recipe edits.

The standalone runners (`lang/tests/codegen/e2e/runner.py`,
`pex_e2e_runner.py`, `pkg_consumer_runner.py`,
`lang/codegen/ir_cases/e2e_runner.py`) also read `DRIFT_TEST_JOBS`
directly via their own env-var fallback paths, so the same knob
controls them too.
"""
from __future__ import annotations

import math
import os
from pathlib import Path


def _physical_cpu_count_linux() -> int | None:
	cpuinfo = Path("/proc/cpuinfo")
	if not cpuinfo.exists():
		return None
	blocks = [b for b in cpuinfo.read_text(encoding="utf-8", errors="ignore").split("\n\n") if b.strip()]
	cores: set[tuple[str, str]] = set()
	for block in blocks:
		physical_id: str | None = None
		core_id: str | None = None
		for line in block.splitlines():
			if ":" not in line:
				continue
			k, v = line.split(":", 1)
			key = k.strip().lower()
			val = v.strip()
			if key == "physical id":
				physical_id = val
			elif key == "core id":
				core_id = val
		if physical_id is not None and core_id is not None:
			cores.add((physical_id, core_id))
	if cores:
		return len(cores)
	return None


def recommended_workers() -> int:
	# 1. Unified env var first — explicit operator override, honored as-is.
	env_jobs = os.environ.get("DRIFT_TEST_JOBS", "").strip()
	if env_jobs:
		try:
			n = int(env_jobs)
			if n > 0:
				return n
		except ValueError:
			pass
	# 2. Protocol default: ceil(nproc / 2).  Physical core count when we can
	#    read it, logical count otherwise.  flocker enforces the cross-lane
	#    cap, so each lane self-limits to half the cores.
	nproc = _physical_cpu_count_linux() or os.cpu_count() or 1
	return max(1, math.ceil(nproc / 2))


def main() -> int:
	print(recommended_workers())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
