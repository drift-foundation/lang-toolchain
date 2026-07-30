# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Release-elision pins: String locals whose return-boundary ledger
verdict is MUST_NOT_DROP are elided from site 3's scope-exit release
sweep (the strings analog of the Phase 4 destructible consultation,
unblocked by B-arch-1: with C2 = 0 the 0.27.145 wrong-MOVED_OUT class is
structurally gone, and every elided slot holds zeroed bytes — UNINIT,
MOVED_OUT expansion zero-store, or String tombstone ≡ zero, proven).

Guardrails pinned: PATH_DEPENDENT keeps the unconditional null-safe
release; DropPolicy-backed needs_drop axis (String is needs_drop=True
despite structural Copy); no-ledger → legacy; arrays / site 4 / C3
untouched. Because this slice REMOVES runtime releases, the leak lanes
(ASAN + Valgrind) are the primary gate here, not a side check.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

# (1) Match binders on unbound arms — the dominant UNINIT population.
_MATCH_BINDER_SOURCE = """\
module main;

fn pick(o: &Optional<String>) nothrow -> Int {
	val code = match o {
		Some(v) => {
			match v == "x" {
				true => { 1 },
				false => { 2 },
			}
		},
		None => { 3 },
	};
	return code;
}

pub fn main() nothrow -> Int {
	val a: Optional<String> = Some("x");
	val b: Optional<String> = Optional::None();
	if pick(a) == 1 {
		if pick(b) == 3 { return 0; }
		return 2;
	}
	return 1;
}
"""

# (2) Multi-path move — the C4 population: local moved into a composite
# on one path, plain exit on the other; the exit release of the
# moved-out slot is elided.
_MULTI_PATH_MOVE_SOURCE = """\
module main;

struct Cfg { app: String }

fn build(flag: Bool) nothrow -> Int {
	var name = "tool" + "";
	val r = match flag {
		true => {
			val c = Cfg(app = move name);
			match c.app == "tool" { true => { 1 }, false => { 9 } }
		},
		false => { 2 },
	};
	return r;
}

pub fn main() nothrow -> Int {
	if build(true) == 1 {
		if build(false) == 2 { return 0; }
		return 2;
	}
	return 1;
}
"""

# (3) Live local at exit — MUST NOT be elided (the leak direction).
_LIVE_AT_EXIT_SOURCE = """\
module main;

fn hold() nothrow -> Int {
	val s = "alive" + "";
	if s == "alive" { return 1; }
	return 0;
}

pub fn main() nothrow -> Int {
	if hold() == 1 { return 0; }
	return 1;
}
"""

# (4) Tombstone via std.mem.replace — the canonical move-field-out;
# the replaced-out local's slot is tombstoned (≡ zero for String).
_REPLACE_SOURCE = """\
module main;

import std.mem as mem;

pub fn main() nothrow -> Int {
	var s = "first" + "";
	val taken = mem.replace(s, "second" + "");
	if taken == "first" {
		if s == "second" { return 0; }
		return 2;
	}
	return 1;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	run_env = {**os.environ, **(env or {})}
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
		env=run_env,
	)


def _run_ok(tmp_path: Path, source: str, *extra: str) -> None:
	res = _compile(tmp_path, source, *extra)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"


def _run_ok_asan(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source, "--sanitize=address,undefined")
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-800:]


def _run_valgrind(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source)
	assert res.returncode == 0, res.stderr[-1200:]
	out = tmp_path / "test_bin"
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost.group(1).replace(",", "")) if lost else 0
	assert vg.returncode == 0, f"valgrind errors:\n{vg_output[-1200:]}"
	assert definitely_lost == 0, f"definitely lost: {definitely_lost} bytes"


def test_match_binder_elision(tmp_path: Path) -> None:
	_run_ok(tmp_path, _MATCH_BINDER_SOURCE)


def test_match_binder_elision_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _MATCH_BINDER_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_match_binder_elision_valgrind(tmp_path: Path) -> None:
	_run_valgrind(tmp_path, _MATCH_BINDER_SOURCE)


def test_multi_path_move_elision(tmp_path: Path) -> None:
	_run_ok(tmp_path, _MULTI_PATH_MOVE_SOURCE)


def test_multi_path_move_elision_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _MULTI_PATH_MOVE_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_multi_path_move_elision_valgrind(tmp_path: Path) -> None:
	_run_valgrind(tmp_path, _MULTI_PATH_MOVE_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_live_at_exit_still_released_valgrind(tmp_path: Path) -> None:
	"""The LEAK direction: a LIVE String at exit must keep its release —
	Valgrind definitely-lost 0 proves the elision never fires on
	MUST_DROP verdicts."""
	_run_valgrind(tmp_path, _LIVE_AT_EXIT_SOURCE)


def test_replace_tombstone_shape(tmp_path: Path) -> None:
	_run_ok(tmp_path, _REPLACE_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_replace_tombstone_shape_valgrind(tmp_path: Path) -> None:
	"""mem.replace tombstones the source slot (String tombstone ≡ zero,
	proven) — both strings released exactly once."""
	_run_valgrind(tmp_path, _REPLACE_SOURCE)


def test_audit_elision_signature(tmp_path: Path) -> None:
	"""Acceptance signature on a whole compile of the match-binder
	shape: c1_release_without_must_drop == 0 AND c4_allowlisted == 0
	(both were nonzero per-compile pre-slice), path-dependent releases
	PRESERVED (> 0 — the kept unconditional-release population), and the
	leak gate + classification gates at 0."""
	audit = tmp_path / "audit.jsonl"
	res = _compile(
		tmp_path, _MATCH_BINDER_SOURCE,
		env={
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit),
		},
	)
	assert res.returncode == 0, res.stderr[-1200:]
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	agg = [r for r in recs if r.get("record") == "aggregate"][0]
	assert agg.get("c1_release_without_must_drop", 0) == 0, agg
	assert agg.get("c4_allowlisted", 0) == 0, agg
	assert agg.get("c1_path_dependent", 0) > 0, agg
	assert agg.get("c1_must_drop_without_release", 0) == 0, agg
	assert agg.get("post_ledger_build_failed", 0) == 0, agg
	assert agg.get("unclassified", 0) == 0 and agg.get("untagged", 0) == 0, agg
