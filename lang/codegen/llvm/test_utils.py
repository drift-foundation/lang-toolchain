# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import os
import struct


def host_word_bits() -> int:
	"""Return the host pointer width for tests until targets are plumbed."""
	return struct.calcsize("P") * 8


def valgrind_cmd(
	*args: str,
	tool: str | None = "memcheck",
	fair_sched: bool = True,
) -> list[str]:
	"""Build a `valgrind` argv with fair scheduling enabled by default.

	Centralizes the one flag that, when omitted, lets a non-cooperative
	busy-spin — a VT/executor test holding a carrier, a lock-free
	contention loop, a reactor spin — monopolize Valgrind's serialized
	single execution slot and starve the rest of the program.  That
	starvation surfaces intermittently as an opaque subprocess timeout
	(SIGKILL) rather than a real failure.  `--fair-sched=yes` forces fair
	round-robin scheduling so every thread keeps making progress.  See
	`test_vt_result_ownership_matrix.py` (R7) for the incident that
	motivated centralizing this.

	Build EVERY Drift-binary Valgrind invocation through here so the flag
	cannot be forgotten at an individual call site — a forgotten flag is
	how the starvation flake recurs.  `fair_sched` is a no-op for a
	genuinely single-threaded program (one thread → nothing to schedule
	fairly), so it is safe to leave on by default; pass
	`fair_sched=False` only if a specific test must reproduce the
	default-scheduler behaviour.

	`*args` are the remaining Valgrind options followed by the target
	program and its arguments, in order — Valgrind requires all of its
	own options to precede the program.  `tool` selects the Valgrind tool
	(`memcheck`, `massif`, ...); pass `tool=None` to omit `--tool` and
	let Valgrind use its default.
	"""
	cmd: list[str] = ["valgrind"]
	if fair_sched:
		cmd.append("--fair-sched=yes")
	if tool is not None:
		cmd.append(f"--tool={tool}")
	cmd.extend(str(a) for a in args)
	return cmd


def sanitizer_timeout(base: int) -> int:
	"""Inflate a subprocess timeout when the test runs in a contended
	environment.

	Two conditions trigger inflation:

	1. **Sanitizer mode** (`DRIFT_ASAN=1` / `DRIFT_UBSAN=1`): instrumented
	   binaries are slower at startup and runtime.
	2. **pytest-xdist parallel worker** (`PYTEST_XDIST_WORKER` set): the
	   test is one of N concurrent workers competing for CPU on a shared
	   machine. The driftc compile pipeline is single-threaded, so a
	   compile that takes T seconds solo can take 4-8x T under high
	   parallel load.

	The two multipliers compose: a sanitized run inside an xdist worker
	gets the largest budget. The previous "sanitizer-only" name is retained
	for compatibility with the existing call sites; despite the name, this
	function is the canonical way for any subprocess-driving test to
	declare a budget that survives both modes.

	Use this for the concrete subprocess invocations that wrap driftc or
	driftc-built binaries and have been observed to exceed their default
	budget. Do NOT apply blanket inflation to unrelated timeouts.
	"""
	multiplier = 1
	if os.environ.get("DRIFT_ASAN") in ("1", "true", "True"):
		multiplier *= 3
	if os.environ.get("DRIFT_UBSAN") in ("1", "true", "True"):
		multiplier *= 3
	if os.environ.get("DRIFT_MEMCHECK") in ("1", "true", "True"):
		multiplier *= 2
	if os.environ.get("PYTEST_XDIST_WORKER"):
		# Roughly accommodate 4-8x wall-clock slowdown under high
		# parallel load. Compose multiplicatively with sanitizer mode.
		multiplier *= 4
	return base * multiplier
