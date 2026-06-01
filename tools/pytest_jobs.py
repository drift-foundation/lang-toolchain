#!/usr/bin/env python3
"""Print the recommended `pytest -n` worker count.

Override order (highest to lowest):
  1. `DRIFT_TEST_JOBS` env var — the unified test-parallelism knob.
     Set this to control concurrency across every test runner the just
     recipes invoke (pytest-managed suites, codegen e2e runner, package
     consumer runner, pex e2e runner). Accepts a positive integer.
  2. Protocol default: the host's **physical core count** (from
     /proc/cpuinfo, logical `os.cpu_count()` as fallback). A lane uses the
     whole box by default.

Why full cores, not half: a half-default leaves half the box idle on
every run to hedge against a scenario that proper coordination already
prevents. Concurrency between lanes is bounded by `flocker` (see
doc/flocker.md), the host-local slot cap — lanes wrapped under a shared
flocker key run one-at-a-time, so a second lane cannot start while the
first holds the slot. The dev-loop (`just test`) runs lanes sequentially
anyway. The only way two lanes oversubscribe is an orchestrator fanning
them out **without** a shared flocker key; that caller owns the fix —
trim via `DRIFT_TEST_JOBS`, or `flocker`-wrap the lanes under one key.
Likewise a lane that OOMs at full cores (e.g. RAM-heavy valgrind on a
small host) is that lane's responsibility: mark it `mode: serial` in its
plan, or set `DRIFT_TEST_JOBS`. We do not tax every box with a half
default to protect those cases.

The justfile pipes this script's output into `pytest -n` for every
parallel pytest recipe, so a single env var change here propagates to
all the pytest invocations without per-recipe edits.

The standalone runners (`lang/tests/codegen/e2e/runner.py`,
`pex_e2e_runner.py`, `pkg_consumer_runner.py`,
`lang/codegen/ir_cases/e2e_runner.py`) also read `DRIFT_TEST_JOBS`
directly via their own env-var fallback paths, so the same knob
controls them too.
"""
from __future__ import annotations

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
	# 2. Protocol default: the host's physical core count (logical count as
	#    fallback).  A lane uses the whole box; flocker bounds *concurrency*
	#    between lanes (shared key → one at a time), and an un-coordinated
	#    orchestrator or a RAM-heavy lane trims via DRIFT_TEST_JOBS / serial
	#    mode.  See the module docstring for why full, not half.
	nproc = _physical_cpu_count_linux() or os.cpu_count() or 1
	return max(1, nproc)


def main() -> int:
	print(recommended_workers())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
