# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Repo-root pytest hooks.

Long-tail mitigation: tests marked @pytest.mark.heavy are sorted to the
front of the collection so xdist (with --dist=worksteal) dispatches them
to workers first. This prevents a slow test from becoming the lone
straggler at the end of a run while 15 other workers sit idle.

Tag a test with @pytest.mark.heavy when --durations=N reveals it as a
top contributor to wall time.

Dual-runtime lane observability:
  pytest_configure       prints a one-line banner at session start naming
                         the active runtime lane (normal vs debug-style)
                         and the related env state.
  pytest_sessionfinish   walks the test build roots, runs `nm` on every
                         linked ELF binary it finds, and asserts every
                         binary carries the sentinel matching the active
                         lane.  Mixed sentinels or zero binaries scanned
                         on a binary-producing suite mean a plumbing leak.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lang.test_support.drift_tmp import session_root as _drift_session_root

# ── Janitor-safe scratch root ─────────────────────────────────────────
#
# Initialize the Drift session root at module import time — before pytest's
# TempPathFactory is constructed — so every tmp_path / tmp_path_factory and
# every bare tempfile.* call lands inside the Drift namespace.
#
# Three knobs are exported:
#   $DRIFT_TMP_ROOT            primary contract; child processes inherit it
#   $PYTEST_DEBUG_TEMPROOT     pytest's tmpdir parent (relocates tmp_path)
#   $TMPDIR + tempfile.tempdir catch bare tempfile.* and subprocess mktemp
#
# Rationale: /tmp is tmpfs; SIGKILL/OOM skips cleanup hooks. A predictable
# Drift-owned namespace lets the janitor reclaim space safely later.
_DRIFT_TMP_ROOT = _drift_session_root()
_PYTEST_TMP = _DRIFT_TMP_ROOT / "pytest"
_PYTEST_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_PYTEST_TMP))
os.environ.setdefault("TMPDIR", str(_DRIFT_TMP_ROOT))
tempfile.tempdir = str(_DRIFT_TMP_ROOT)


# ── Helpers ──────────────────────────────────────────────────────────


def _env_true(name: str) -> bool:
	return os.environ.get(name, "") in ("1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON")


def _active_lane() -> str:
	"""Return "debug-style" if DRIFT_DEBUG=1 is set, else "normal"."""
	return "debug-style" if _env_true("DRIFT_DEBUG") else "normal"


def _expected_sentinel(lane: str) -> str:
	return "__drift_rt_mode_debug" if lane == "debug-style" else "__drift_rt_mode_normal"


def _other_sentinel(lane: str) -> str:
	return "__drift_rt_mode_normal" if lane == "debug-style" else "__drift_rt_mode_debug"


# Test build roots that may contain produced binaries.  Walked at session
# end by the sentinel audit.  Order matters only for output stability.
#
# The pytest tree now lives under $DRIFT_TMP_ROOT/pytest/pytest-of-<user>/
# (see PYTEST_DEBUG_TEMPROOT wiring at module top), so we resolve relative
# to _DRIFT_TMP_ROOT rather than hard-coding /tmp.
_AUDIT_ROOTS = (
	# pytest tmp_path per-session, relocated under the Drift session root.
	str(_PYTEST_TMP / "pytest-of-{user}" / "pytest-current"),
	# Repo-local test build artifacts (e.g. lang/tests/codegen/e2e cases).
	"build/tests",
)


def _audit_root_paths(repo_root: Path) -> list[Path]:
	user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
	roots: list[Path] = []
	for raw in _AUDIT_ROOTS:
		expanded = raw.format(user=user)
		p = Path(expanded)
		if not p.is_absolute():
			p = (repo_root / p).resolve()
		if p.exists():
			roots.append(p)
	return roots


def _looks_like_elf(path: Path) -> bool:
	"""Cheap ELF magic check.  Avoids nm-ing every random file under tmp."""
	try:
		with path.open("rb") as fh:
			return fh.read(4) == b"\x7fELF"
	except (OSError, PermissionError):
		return False


_AUDIT_SKIP_MARKER = ".drift-lane-audit-skip"


def _scan_sentinels(roots: list[Path], expected: str, other: str, *, max_files: int = 5000) -> dict:
	"""Walk roots, nm any ELF, count sentinel occurrences.

	Returns {expected: int, other: int, neither: int, scanned: int, leaks: list[str]}.
	"leaks" lists up to 10 binary paths that carry the wrong sentinel — the
	smoking-gun evidence of a plumbing leak.

	Subtrees containing a `.drift-lane-audit-skip` marker file are skipped
	entirely.  This is the opt-out for tests that intentionally produce
	mixed-variant binaries (e.g. the sentinel selection regression itself,
	which builds the same consumer in BOTH lanes from one staged toolchain
	to prove the selection contract).
	"""
	nm = shutil.which("nm")
	stats = {"expected": 0, "other": 0, "neither": 0, "scanned": 0, "leaks": [], "nm_failed": 0, "skipped_subtrees": 0}
	if nm is None:
		return stats

	for root in roots:
		for dirpath, dirs, files in os.walk(str(root)):
			# Honor opt-out marker: prune the entire subtree.
			if _AUDIT_SKIP_MARKER in files:
				stats["skipped_subtrees"] += 1
				dirs[:] = []
				continue
			for fname in files:
				if stats["scanned"] >= max_files:
					return stats
				p = Path(dirpath) / fname
				# Skip object files, archives, IR — only inspect linked binaries.
				if p.suffix in (".o", ".a", ".ll", ".bc", ".so", ".log", ".out"):
					# Note: .so and .out filtered to avoid valgrind logs / shared libs.
					if p.suffix not in (".so",):
						continue
				if not _looks_like_elf(p):
					continue
				try:
					res = subprocess.run(
						[nm, "--defined-only", str(p)],
						text=True,
						capture_output=True,
						timeout=10,
					)
				except (subprocess.SubprocessError, OSError):
					stats["nm_failed"] += 1
					continue
				if res.returncode != 0:
					stats["nm_failed"] += 1
					continue
				stats["scanned"] += 1
				has_expected = expected in res.stdout
				has_other = other in res.stdout
				if has_expected and not has_other:
					stats["expected"] += 1
				elif has_other and not has_expected:
					stats["other"] += 1
					if len(stats["leaks"]) < 10:
						stats["leaks"].append(str(p))
				elif has_expected and has_other:
					# Both sentinels in one binary = paired contract violated.
					stats["other"] += 1
					if len(stats["leaks"]) < 10:
						stats["leaks"].append(f"{p} (BOTH SENTINELS)")
				else:
					stats["neither"] += 1
	return stats


# ── Hooks ────────────────────────────────────────────────────────────


def pytest_collection_modifyitems(config, items):
	items.sort(key=lambda it: 0 if it.get_closest_marker("heavy") else 1)


def pytest_configure(config):
	"""(A) Lane banner — print active runtime lane + related env state at session start.

	Suppressed under xdist worker processes (where there is no terminal) so
	the banner only fires once on the controller.
	"""
	if hasattr(config, "workerinput"):
		# xdist worker — controller already printed the banner.
		return
	lane = _active_lane()
	drift_debug = "1" if _env_true("DRIFT_DEBUG") else "(unset)"
	cdbg = os.environ.get("DRIFT_COMPILER_DEBUG", "(unset)")
	asan = "1" if _env_true("DRIFT_ASAN") else "(unset)"
	ubsan = "1" if _env_true("DRIFT_UBSAN") else "(unset)"
	memcheck = "1" if _env_true("DRIFT_MEMCHECK") else "(unset)"
	massif = "1" if _env_true("DRIFT_MASSIF") else "(unset)"
	# Use stderr so the banner appears even in -q mode and is captured by
	# CI log scrapers without disturbing pytest's stdout summary.
	print(
		f"\n[drift-lane] active lane: {lane}  "
		f"DRIFT_DEBUG={drift_debug}  DRIFT_COMPILER_DEBUG={cdbg}",
		file=sys.stderr,
		flush=True,
	)
	print(
		f"[drift-lane] sanitizer state: "
		f"DRIFT_ASAN={asan}  DRIFT_UBSAN={ubsan}  "
		f"DRIFT_MEMCHECK={memcheck}  DRIFT_MASSIF={massif}",
		file=sys.stderr,
		flush=True,
	)


def pytest_sessionfinish(session, exitstatus):
	"""(B) Sentinel audit — assert every linked binary the suite produced carries
	the sentinel matching the active runtime lane.

	Suppressed:
	  - under xdist worker processes (the controller runs the audit once
	    against the shared tmp tree at session end).
	  - when DRIFT_LANE_AUDIT=0 (escape hatch for debugging individual tests).

	The audit is best-effort observability, not a hard gate: it prints a
	clear summary line and only marks the session as failed when it finds
	an actual sentinel leak (binary with the wrong sentinel) or when zero
	binaries were scanned despite a binary-producing suite running.
	"""
	if hasattr(session.config, "workerinput"):
		return
	if os.environ.get("DRIFT_LANE_AUDIT", "1") == "0":
		print("[drift-lane-audit] skipped (DRIFT_LANE_AUDIT=0)", file=sys.stderr, flush=True)
		return

	repo_root = Path(__file__).resolve().parent
	lane = _active_lane()
	expected = _expected_sentinel(lane)
	other = _other_sentinel(lane)

	roots = _audit_root_paths(repo_root)
	if not roots:
		print(
			f"[drift-lane-audit] no audit roots present — skipping "
			f"(expected sentinel: {expected})",
			file=sys.stderr,
			flush=True,
		)
		return

	stats = _scan_sentinels(roots, expected, other)
	if stats["scanned"] == 0:
		print(
			f"[drift-lane-audit] {len(roots)} root(s) walked, 0 ELF binaries "
			f"scanned — nothing to verify (lane: {lane})",
			file=sys.stderr,
			flush=True,
		)
		return

	leak_count = stats["other"]
	verdict = "PASS" if leak_count == 0 else "FAIL"
	summary = (
		f"[drift-lane-audit] lane={lane}  expected={expected}  "
		f"scanned={stats['scanned']}  matching={stats['expected']}  "
		f"leaks={leak_count}  no-sentinel={stats['neither']}  "
		f"nm_failed={stats['nm_failed']}  "
		f"skipped_subtrees={stats['skipped_subtrees']}  → {verdict}"
	)
	print(summary, file=sys.stderr, flush=True)
	if leak_count:
		print("[drift-lane-audit] leaked binaries (up to 10):", file=sys.stderr, flush=True)
		for leak in stats["leaks"]:
			print(f"  {leak}", file=sys.stderr, flush=True)
		# Mark the session as failed: a sentinel leak means a binary-producing
		# test path bypassed the active lane selector — exactly the kind of
		# bug the dual-runtime workstream needs to catch.
		session.exitstatus = max(session.exitstatus, 1)
