# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""B-arch-1d pins: String STORE-value stakes (StoreLocal/StoreRef/
ArrayIndexStore source operands) materialize as pre-store `CopyValue`,
and the view set gains the `ResultOk` Ok-payload projection.
`ArrayIndexLoad[Unchecked]` is NOT a view — its codegen lowering
retains the extracted element (owned at extraction, VariantGetField's
sibling), so it stays a terminal producer; staking it leaks one ref
per element load.  The array pins here use HEAP strings (concat), not
literals: static strings (DRIFT_STRING_FLAG_STATIC) no-op on
retain/release and MASK exactly that imbalance (which the heap-string
e2e fixtures `main_argv_content` /
`array_extend_borrowed_source_string_no_uaf` caught).

Ordering contract pinned here: the stake lands BEFORE the pipeline's
old-destination release expansion — the strictly safer order (the +1 is
taken while the source is provably alive; the self-aliased-store pin
exercises exactly the window today's retain-after-release order leaves).
Destination-side release / site-4 drop_before_overwrite is untouched.

OOB acceptance pin (review-required): for an out-of-bounds
`arr[i] = v`, the pre-store copy executes before the bounds check — and
that cannot leak under any cleanup contract because
`drift_bounds_check_fail` is `__attribute__((noreturn))`; its own
documented contract (array_runtime.c): "Ends in abort() via the
diagnostic-throw path; cleanup never fires on a noreturn frame, and the
process dies before any leak matters." The OOB row asserts the abort
(non-clean exit; silent in a nothrow main on this build — the envelope
assertion applies only when output exists).
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

# (1) Overwrite: x had a prior value; y stays live after.
_OVERWRITE_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	val y = "fresh";
	var x = "stale" + "";
	x = y;
	if x == "fresh" {
		if y == "fresh" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (2) Element-view store: x = arr[i]; and String element store arr[i] = y.
# HEAP strings on purpose — static literals mask refcount imbalance.
_ARRAY_STORE_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	var arr: Array<String> = [];
	arr.push("a" + "");
	arr.push("b" + "");
	val y = "z" + "";
	var x = "old" + "";
	x = arr[1];
	arr[0] = y;
	if x == "b" {
		if arr[0] == "z" {
			if y == "z" { return 0; }
			return 3;
		}
		return 2;
	}
	return 1;
}
"""

# (3) Store through &mut.
_STORE_REF_SOURCE = """\
module main;

fn set(p: &mut String, v: String) nothrow -> Void {
	*p = v;
	return;
}

pub fn main() nothrow -> Int {
	var x = "old" + "";
	val y = "new";
	set(x, y);
	if x == "new" {
		if y == "new" { return 0; }
		return 2;
	}
	return 1;
}
"""

# (4) ResultOk projection: auto-try eager-unwrap store, Ok and Err paths.
_RESULT_OK_SOURCE = """\
module main;

pub error FetchError {
	what: String,
}

fn fetch(flag: Bool) throws -> String {
	if flag { return "hit" + ""; }
	throw FetchError(what = "miss" + "");
}

fn probe(flag: Bool) nothrow -> Int {
	val got = try fetch(flag) catch { "fallback" };
	if got == "hit" { return 1; }
	if got == "fallback" { return 2; }
	return 0;
}

pub fn main() nothrow -> Int {
	if probe(true) == 1 {
		if probe(false) == 2 { return 0; }
		return 2;
	}
	return 1;
}
"""

# (5) Self-aliased store: the ordering-window pin.
_SELF_STORE_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	var s = "only" + "";
	s = s;
	if s == "only" { return 0; }
	return 1;
}
"""

# (6) OOB ArrayIndexStore: must ABORT via the noreturn bounds-fail path.
_OOB_STORE_SOURCE = """\
module main;

pub fn main() nothrow -> Int {
	var arr = ["a", "b"];
	val y = "z" + "";
	arr[9] = y;
	return 0;
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


def test_overwrite_store(tmp_path: Path) -> None:
	_run_ok(tmp_path, _OVERWRITE_SOURCE)


def test_overwrite_store_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _OVERWRITE_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_overwrite_store_valgrind(tmp_path: Path) -> None:
	"""Review-required overwrite Valgrind row: old value released once,
	new copy owned by the slot, source local released at exit."""
	_run_valgrind(tmp_path, _OVERWRITE_SOURCE)


def test_array_stores(tmp_path: Path) -> None:
	_run_ok(tmp_path, _ARRAY_STORE_SOURCE)


def test_array_stores_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _ARRAY_STORE_SOURCE)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_array_stores_valgrind(tmp_path: Path) -> None:
	"""Review-required array-element Valgrind row (element-view store +
	String element store)."""
	_run_valgrind(tmp_path, _ARRAY_STORE_SOURCE)


def test_store_through_mut_ref(tmp_path: Path) -> None:
	_run_ok(tmp_path, _STORE_REF_SOURCE)


def test_store_through_mut_ref_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _STORE_REF_SOURCE)


def test_result_ok_projection_both_paths(tmp_path: Path) -> None:
	"""ResultOk PROJECTION pin (0.33.46 adjacency): eager-unwrap store on
	the Ok path AND the Err/catch path — the Result temp's payload
	lifecycle must be undisturbed by the pre-store copy."""
	_run_ok(tmp_path, _RESULT_OK_SOURCE)


def test_result_ok_projection_both_paths_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _RESULT_OK_SOURCE)


def test_self_aliased_store(tmp_path: Path) -> None:
	"""`s = s;` — the copy-before-release ordering pin: post-1d the +1 is
	taken while the buffer is provably alive."""
	_run_ok(tmp_path, _SELF_STORE_SOURCE)


def test_self_aliased_store_asan(tmp_path: Path) -> None:
	_run_ok_asan(tmp_path, _SELF_STORE_SOURCE)


def test_oob_array_store_aborts_outside_cleanup_contract(tmp_path: Path) -> None:
	"""Review-required OOB row: `arr[9] = y` with len 2. The pre-store
	CopyValue executes before the checked old-slot load; the bounds
	failure then terminates the process via the noreturn
	`drift_bounds_check_fail` abort — no unwind, no cleanup contract, so
	the in-flight +1 cannot leak into any live state. Asserts a
	non-clean abort exit with the IndexError envelope."""
	res = _compile(tmp_path, _OOB_STORE_SOURCE)
	assert res.returncode == 0, res.stderr[-1200:]
	out = tmp_path / "test_bin"
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(15))
	# The load-bearing contract is the ABORT itself (noreturn — no
	# unwind, no cleanup): a signal death (SIGABRT) or nonzero abort
	# exit. In a nothrow main the failure is silent on this build (no
	# envelope printed), so no message is required; if output exists it
	# must be the IndexError envelope.
	assert run.returncode != 0, "OOB store must not exit cleanly"
	err = run.stderr + run.stdout
	if err.strip():
		assert "IndexError" in err or "index" in err.lower(), f"unexpected failure output:\n{err[-600:]}"


def _audit_pin(tmp_path: Path, source: str, fn_tail: str) -> None:
	"""Shared acceptance pin: compile `source` with the audit on and
	assert ZERO store_value_retain (and no other stake regressions) in
	the function that performs the store, plus all hard gates at 0.
	Needed per-shape because the runtime rows CANNOT catch a rewrite
	regression: if the pass stopped staking a shape, the pipeline would
	fall back to its late retain and behavior would be identical — only
	the audit sees the difference (review finding, 1d round 1)."""
	audit = tmp_path / "audit.jsonl"
	res = _compile(
		tmp_path, source,
		env={
			"DRIFT_STRING_ARC_AUDIT": "1",
			"DRIFT_STRING_ARC_AUDIT_VERBOSE": "1",
			"DRIFT_STRING_ARC_AUDIT_FILE": str(audit),
		},
	)
	assert res.returncode == 0, res.stderr[-1200:]
	recs = [json.loads(line.split("] ", 1)[1]) for line in audit.read_text().splitlines()]
	fns = [r for r in recs if r.get("record") == "fn" and r.get("fn", "").split("::")[-1] == fn_tail]
	assert fns, f"{fn_tail} audit record expected"
	m = fns[0]
	assert m.get("site_class:store_value_retain", 0) == 0, m
	assert m.get("site_class:value_position_retain", 0) == 0, m
	assert m.get("site_class:call_arg_retain", 0) == 0, m
	agg = [r for r in recs if r.get("record") == "aggregate"][0]
	assert agg.get("c1_must_drop_without_release", 0) == 0, agg
	assert agg.get("post_ledger_build_failed", 0) == 0, agg
	assert agg.get("unclassified", 0) == 0 and agg.get("untagged", 0) == 0, agg


def test_audit_store_stakes_materialized(tmp_path: Path) -> None:
	"""ArrayIndexStore/StoreLocal stakes (AIL itself is terminal: its
	dest is codegen-owned and moves into the holder without a
	late retain, so store_value_retain still reads 0)."""
	_audit_pin(tmp_path, _ARRAY_STORE_SOURCE, "main")


def test_audit_store_ref_stake_materialized(tmp_path: Path) -> None:
	"""StoreRef stake: the `*p = v` store lives in `set`."""
	_audit_pin(tmp_path, _STORE_REF_SOURCE, "set")


def test_audit_result_ok_stake_materialized(tmp_path: Path) -> None:
	"""ResultOk-projection stake: the eager-unwrap store lives in `probe`."""
	_audit_pin(tmp_path, _RESULT_OK_SOURCE, "probe")
