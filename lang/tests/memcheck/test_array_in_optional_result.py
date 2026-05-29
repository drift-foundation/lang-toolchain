# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Array inside Optional / Result — memcheck carrier for arrays
wrapped in variant containers.

**The shapes under test.**  Three composite-payload patterns where
the Array sits inside a variant's payload slot:

  1. **Optional<Array<String>> = Some(arr)** scope-exit drop —
     variant drop helper must dispatch to the Some-arm payload
     destructor, which in turn invokes the Array drop chain
     (releasing each contained String, then freeing the array
     buffer).

  2. **Optional<Array<String>>** match-destructured: `match opt {
     Some(arr) => use; None => ... }` — the binder takes ownership
     of the payload via per-field move; site-2 cleanup authoring
     must not double-drop the Array on the Some arm and must drop
     correctly on a non-bound path.

  3. **Result<Array<String>, Int>** with both Ok and Err
     populated branches — Ok variant carries the Array (full drop
     chain on scope-exit); Err variant carries Int and never
     constructs the Array slot.  Tests that the variant's arm-tag
     correctly dispatches to per-arm destructor or no-op.

**Why this carrier exists.**  The Array audit (2026-04-26) found:
  - `result_ok_array_match_move_no_double_free` and
    `fnresult_ok_array_byte` exist as e2e codegen tests but neither
    runs under valgrind.
  - `test_ownership_ledger_return_consume.py::test_json_array_result_ok_shape`
    covers Result::Ok(Array(...)) at stage2 unit level but does not
    catch refcount-level leaks.

This carrier closes that gap by running each shape under valgrind,
exercising:
  - variant zero-tag drop policy (None / Err: no Array constructed →
    no Array drop should run)
  - variant payload drop dispatch (Some / Ok: arm-specific Array
    drop must fire exactly once)
  - match-time per-field move accounting (binder takes ownership;
    scrutinee local must NOT be dropped after the move)

**Constraint.**  No Array semantic changes; no cleanup unification
patch.  If any test fails on the current compiler, freeze and
report LANGUAGE_BUG (per AGENTS.md regression-first).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from lang.codegen.llvm.test_utils import valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


# Shape 1: Optional<Array<String>> Some scope-exit drop, no match.
# Each producer call returns Optional::Some(arr) where arr carries
# heap-allocated Strings; the local goes out of scope at the end of
# main without being match-destructured.  The variant drop helper
# must dispatch to the Some arm and run the Array<String> drop chain.
OPTIONAL_ARRAY_SOME_SCOPE_DROP_SOURCE = """\
module main;

import std.format as fmt;

fn build_some(n: Int) nothrow -> Optional<Array<String>> {
\tvar arr: Array<String> = [];
\tvar i = 0;
\twhile i < n {
\t\tarr.push(fmt.format_int(i + 400));
\t\ti = i + 1;
\t}
\treturn Optional::Some(move arr);
}

fn build_none() nothrow -> Optional<Array<String>> {
\treturn Optional::None();
}

pub fn main() nothrow -> Int {
\tval s1 = build_some(2);
\tval s2 = build_some(3);
\tval n1 = build_none();
\tval s3 = build_some(1);
\t// All four locals (s1, s2, s3 carrying Array<String>; n1 the
\t// zero-tag None) must drop correctly on main scope-exit.  Some
\t// arms invoke the full chain; None is a no-op.
\treturn 0;
}
"""


# Shape 2: Optional<Array<String>> match-destructured.  The binder
# `arr` takes ownership of the payload via per-field move; on the
# Some arm, `arr` drops at the arm's scope-exit (or is consumed).
# On the None arm, no payload exists — only the variant tag is
# scoped.  Site-2 cleanup authoring must coordinate: scrutinee
# local must NOT be dropped after the per-field move.
OPTIONAL_ARRAY_MATCH_DESTRUCTURE_SOURCE = """\
module main;

import std.format as fmt;

fn build_some_or_none(n: Int) nothrow -> Optional<Array<String>> {
\tif n <= 0 {
\t\treturn Optional::None();
\t}
\tvar arr: Array<String> = [];
\tvar i = 0;
\twhile i < n {
\t\tarr.push(fmt.format_int(i + 500));
\t\ti = i + 1;
\t}
\treturn Optional::Some(move arr);
}

fn process(opt: Optional<Array<String>>) nothrow -> Int {
\tval out = match opt {
\t\tOptional::Some(arr) => {
\t\t\tvar total = 0;
\t\t\tvar i = 0;
\t\t\twhile i < arr.len {
\t\t\t\tval s_ref = &arr[i];
\t\t\t\ttotal = total + s_ref.byte_length();
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\ttotal
\t\t\t// arr drops here — must drop the Array<String> exactly once.
\t\t},
\t\tOptional::None() => { 0 }
\t};
\treturn out;
}

pub fn main() nothrow -> Int {
\tval a = process(build_some_or_none(2));
\tval b = process(build_some_or_none(0));
\tval c = process(build_some_or_none(4));
\tval d = process(build_some_or_none(0));
\treturn a + b + c + d;
}
"""


# Shape 3: Result<Array<String>, Int> with Ok and Err branches.
# Ok payload carries Array<String> (refcount drop chain); Err
# carries Int (no drop chain).  Match destructuring on both arms.
RESULT_ARRAY_MATCH_DESTRUCTURE_SOURCE = """\
module main;

import std.core as core;
import std.format as fmt;

fn build_ok_or_err(n: Int) nothrow -> core.Result<Array<String>, Int> {
\tif n <= 0 {
\t\treturn core.Result::Err(n);
\t}
\tvar arr: Array<String> = [];
\tvar i = 0;
\twhile i < n {
\t\tarr.push(fmt.format_int(i + 600));
\t\ti = i + 1;
\t}
\treturn core.Result::Ok(move arr);
}

fn process_result(r: core.Result<Array<String>, Int>) nothrow -> Int {
\tval out = match r {
\t\tcore.Result::Ok(arr) => {
\t\t\tvar total = 0;
\t\t\tvar i = 0;
\t\t\twhile i < arr.len {
\t\t\t\tval s_ref = &arr[i];
\t\t\t\ttotal = total + s_ref.byte_length();
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\ttotal
\t\t},
\t\tcore.Result::Err(_e) => { 0 },
\t\tdefault => { 0 }
\t};
\treturn out;
}

pub fn main() nothrow -> Int {
\tval a = process_result(build_ok_or_err(2));
\tval b = process_result(build_ok_or_err(0));
\tval c = process_result(build_ok_or_err(3));
\tval d = process_result(build_ok_or_err(-1));
\treturn a + b + c + d;
}
"""


def _compile_and_valgrind(tmp_path: Path, source: str, *, label: str) -> tuple[int, str]:
	"""Compile under raw stdlib and run under valgrind.  Returns
	(definitely_lost_bytes, valgrind_log_text)."""
	assert shutil.which("valgrind") is not None, "valgrind required"

	src = tmp_path / "main.drift"
	src.write_text(source)
	out_bin = tmp_path / f"bin_{label}"

	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"[{label}] compile failed: {res.stderr[:1500]}"
	assert out_bin.exists(), f"[{label}] binary not produced"

	vg_log = tmp_path / f"valgrind_{label}.log"
	subprocess.run(
		valgrind_cmd("--leak-check=full",
			"--show-leak-kinds=definite,indirect",
			"--errors-for-leak-kinds=definite,indirect",
			"--error-exitcode=97",
			f"--log-file={vg_log}",
			str(out_bin)),
		capture_output=True, text=True, timeout=120,
	)
	vg_output = vg_log.read_text() if vg_log.exists() else ""
	lost_match = re.search(r"definitely lost: (\d[\d,]*) bytes", vg_output)
	definitely_lost = int(lost_match.group(1).replace(",", "")) if lost_match else 0
	return definitely_lost, vg_output


def _assert_valgrind_clean(lost: int, vg_log: str, *, label: str, broken_state_hint: str) -> None:
	"""Assert zero definitely-lost bytes AND no valgrind errors."""
	assert lost == 0, (
		f"[{label}] LANGUAGE_BUG: variant-wrapped Array leak — "
		f"{lost} bytes definitely lost.\n"
		f"Expected symptom: {broken_state_hint}\n"
		f"Touch points: variant drop dispatch, site-2 cleanup authoring "
		f"(`match_cleanup_authoring.py`), Array recursive drop helper.\n\n"
		f"Valgrind log tail:\n{vg_log[-1500:]}"
	)
	if "Invalid read" in vg_log or "Invalid write" in vg_log or "Invalid free" in vg_log:
		raise AssertionError(
			f"[{label}] valgrind reported invalid memory access — "
			f"likely double-drop of the Array payload from both the "
			f"match binder and the scrutinee, OR drop dispatched to "
			f"the wrong variant arm.\n\n{vg_log[-1500:]}"
		)


def test_optional_array_some_scope_drop_no_leak(tmp_path: Path) -> None:
	"""Optional<Array<String>> Some scope-exit drop without match —
	tests variant drop helper's arm dispatch on full payload chain.

	Mixed Some/None locals stress the variant zero-tag drop policy:
	None must be a no-op while Some must run the full Array<String>
	drop chain.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, OPTIONAL_ARRAY_SOME_SCOPE_DROP_SOURCE, label="optional_array_scope"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="optional_array_scope",
		broken_state_hint=(
			"variant drop dispatch missed the Some arm's payload "
			"destructor → Array<String> never dropped → buffer + each "
			"String leaked (~24 bytes per String + array overhead)."
		),
	)


def test_optional_array_match_destructure_no_leak(tmp_path: Path) -> None:
	"""Optional<Array<String>> match-destructured — binder takes
	ownership via per-field move on Some arm; scrutinee local must
	NOT be dropped after the move.

	Alternating Some/None producer outputs ensure both arms are
	exercised in the same run; per-call leaks surface as multi-block
	valgrind reports.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, OPTIONAL_ARRAY_MATCH_DESTRUCTURE_SOURCE, label="optional_array_match"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="optional_array_match",
		broken_state_hint=(
			"site-2 cleanup authoring double-dropped the Array (binder "
			"`arr` AND scrutinee `opt`'s Some payload both fired Array "
			"drop) → Invalid free / Invalid read; OR the binder's "
			"end-of-arm drop was skipped → definitely-lost."
		),
	)


def test_result_array_match_destructure_no_leak(tmp_path: Path) -> None:
	"""Result<Array<String>, Int> match destructure — exercises
	Ok/Err arm dispatch with Array payload only on Ok.

	The Err arm's `_e` binder is Int (Copy, no drop); the Ok arm's
	`arr` binder is the Array<String> requiring full chain release.
	"""
	lost, vg_log = _compile_and_valgrind(
		tmp_path, RESULT_ARRAY_MATCH_DESTRUCTURE_SOURCE, label="result_array_match"
	)
	_assert_valgrind_clean(
		lost, vg_log,
		label="result_array_match",
		broken_state_hint=(
			"per-arm dispatch failed: either Ok arm's `arr` dropped "
			"twice (binder + Result residual) or Err arm's residual "
			"Result wrapper leaked the (uninitialised) Ok payload slot."
		),
	)
