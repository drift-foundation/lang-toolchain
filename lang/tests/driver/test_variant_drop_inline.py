# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Authority-level enum/match cleanup optimization (2026-07-25 review
directive): single-value variant drops go through a BY-VALUE
`alwaysinline` helper (`__drift_variant_drop_<key>`) with no element
loop and no caller-side alloca/store — so LLVM folds the tag switch
wherever the tag is dominated, and a Result::Ok(Copy) path pays
NOTHING for the inactive destructible Err arm.  General: applies to
every variant drop (Result, Optional, user variants), not a byte_at
special case.  Real ARRAYS keep the loop-shaped `__drift_array_drop_`
helper.

Proof set here (per directive):
  * IR shape teeth — the variant helper is defined
    `internal ... alwaysinline`, takes the variant BY VALUE, and drop
    call sites pass the SSA value (no alloca+len=1 array call);
    array-of-droppable still uses the loop helper;
  * behavior — Ok-heavy loops, Err paths, EARLY RETURNS with live
    Results, loop-carried Results, a USER variant and Optional<String>
    with RUNTIME-UNKNOWN tags (both arms), an Array<String>
    loop-helper control, and a plain-String control
    all drop exactly once (checksums + clean exit; the valgrind proof
    is lang/tests/memcheck/test_variant_drop_inline_memcheck.py);
  * ownership counters — the optimization is CODEGEN-only (MIR is
    untouched), so ownership-audit counters are structurally
    identical; the corpus gate enforces that end-to-end;
  * performance — the tier gate (test_string_view_perf_tiers.py)
    carries the measured effect (public Result accessors within 2x
    of raw scans).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SOURCE = r"""module main;

import std.core as core;
import std.console as cons;

error HeavyErr { msg: String, code: Int }

variant Shape {
	Tag(name: String),
	Plain
}

fn make(n: Int, s: &String) nothrow -> core.Result<Int, HeavyErr> {
	if n < 0 {
		return core.Result::Err(HeavyErr(msg = s.clone(), code = n));
	}
	return core.Result::Ok(n * 2);
}

fn early(n: Int, s: &String) nothrow -> Int {
	// EARLY RETURN with a live droppable Result in scope.
	val r = make(0 - 1, s);
	if n > 10 {
		return 100;   // r (Err, droppable) must drop exactly once here
	}
	match r {
		Ok(v) => { return v; },
		Err(e) => { return e.code; }
	}
}

pub fn main() nothrow -> Int {
	var payload = "heavy-";
	payload = payload + "payload";   // heap-backed

	// Ok-heavy loop: the Err arm's String never exists; the Ok drops
	// must cost/do nothing beyond the tag check.
	var acc = 0;
	var i = 0;
	while i < 10000 {
		match make(i, &payload) {
			Ok(v) => { acc = acc + v; },
			Err(e) => { return 1; }
		}
		i = i + 1;
	}
	if acc != 99990000 { return 2; }

	// Err paths in a loop: each Err carries a retained String; each
	// must drop exactly once (valgrind-proven in the memcheck twin).
	var errs = 0;
	i = 0;
	while i < 100 {
		match make(0 - i - 1, &payload) {
			Ok(v) => { return 3; },
			Err(e) => {
				if e.msg != payload { return 4; }
				errs = errs + 1;
			}
		}
		i = i + 1;
	}
	if errs != 100 { return 5; }

	// Early-return path with a live Err Result.
	if early(99, &payload) != 100 { return 6; }
	if early(1, &payload) != 0 - 1 { return 7; }

	// Loop-carried droppable Result with break.
	var last = 0;
	i = 0;
	while i < 50 {
		val r = make(0 - 5, &payload);
		if i == 25 {
			match r { Ok(v) => { last = v; }, Err(e) => { last = e.code; } }
			break;
		}
		i = i + 1;
	}
	if last != 0 - 5 { return 8; }

	// Non-Result droppable control: a plain String local still drops
	// exactly once through scope exit.
	{
		var control = payload.clone();
		control = control + "-x";
		if control.byte_length() != payload.byte_length() + 2 { return 9; }
	}

	// USER VARIANT control with RUNTIME-UNKNOWN tags: both arms taken
	// (parity-driven), the droppable arm carries a retained String —
	// each value drops exactly once through the generic variant-drop
	// helper (tag genuinely unknown at the drop site).
	var tagged = 0;
	i = 0;
	while i < 200 {
		var sh = Shape::Plain();
		if i - (i / 2) * 2 == 0 {
			sh = Shape::Tag(name = payload.clone());
		}
		match sh {
			Tag(nm) => { tagged = tagged + nm.byte_length(); },
			Plain() => { tagged = tagged + 1; }
		}
		i = i + 1;
	}
	if tagged != 100 * payload.byte_length() + 100 { return 10; }

	// Optional<String> control, runtime-unknown tags both arms.
	var opts = 0;
	i = 0;
	while i < 200 {
		var o = Optional<String>::None();
		if i - (i / 2) * 2 == 1 {
			o = Optional::Some(payload.clone());
		}
		match o {
			Some(sv) => { opts = opts + sv.byte_length(); },
			None() => { opts = opts + 1; }
		}
		i = i + 1;
	}
	if opts != 100 * payload.byte_length() + 100 { return 11; }

	// ARRAY control: droppable elements still go through the
	// loop-shaped array-drop helper (runtime len), not the
	// single-value variant helper.
	{
		var arr: Array<String> = [];
		i = 0;
		while i < 10 {
			arr.push(payload.clone());
			i = i + 1;
		}
		if arr.len != 10 { return 12; }
	}

	cons.println("variant drop OK");
	return 0;
}
"""


def _compile(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "vd.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed:\n{(res.stdout + res.stderr)[:2000]}"
	return out_bin


def test_variant_drop_behavior_exactly_once(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"exit={run.returncode}\n{run.stderr[:400]}"
	assert "variant drop OK" in run.stdout


def test_variant_drop_ir_shape(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path)
	ir = Path(str(out_bin) + ".ll").read_text()

	# The single-value variant helper: internal + alwaysinline + BY
	# VALUE (a %Variant_ param, not the (i64, ptr) array signature).
	defs = re.findall(r"define internal void @(__drift_variant_drop_\S+)\((%Variant_\S+) %v\) alwaysinline \{", ir)
	assert defs, "no by-value alwaysinline variant drop helper emitted"

	# Call sites pass the SSA variant VALUE.
	calls = re.findall(r"call void @__drift_variant_drop_\S+\(%Variant_\S+ %\S+\)", ir)
	assert calls, "variant drop call sites do not pass the variant by value"

	# No variant drop goes through the len=1 array-helper shape anymore.
	assert not re.search(r"call void @__drift_array_drop_\S*Result\S*\(i64 1,", ir), (
		"a Result drop still routes through the loop-shaped array helper"
	)

	# The USER variant and Optional<String> drops also use by-value
	# helpers (runtime-unknown tags included) ...
	assert re.search(r"call void @__drift_variant_drop_\S*Shape\S*\(%Variant_\S+ %\S+\)", ir), (
		"user-variant drops do not use the by-value helper"
	)
	# ... while ARRAYS of droppables keep the loop-shaped helper with a
	# RUNTIME length argument.
	assert re.search(r"call void @__drift_array_drop_\S+\(i64 %\S+, ptr", ir), (
		"array drops lost the loop-shaped helper"
	)
