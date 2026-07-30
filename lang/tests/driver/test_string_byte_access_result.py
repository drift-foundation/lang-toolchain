# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Public byte access: bounds failure is DATA (binding API decision,
2026-07-25) — and the low-level primitive is fail-closed.

	pub fn byte_at(self: &String, index: Int) nothrow
	    -> core.Result<Byte, std.err:IndexError>       (std.text impl)
	pub fn byte_at(self: &StringByteView, index: Int) nothrow
	    -> core.Result<Byte, std.err:IndexError>

History: OOB `core.string_byte_at` originally ABORTED silently (the
C-side bounds path ends in the abort()-stub drift_error_raise; no
C→Drift throw channel), was briefly made throwing, and is now pinned
NOTHROW + FAIL-CLOSED: an out-of-range primitive read aborts with an
AssertLoc diagnostic; the PUBLIC methods return Result and never
throw.  The three authorities agree: the intrinsic declaration is
`nothrow`, CallInfo carries nothrow, and the MIR lowering emits the
assert-shaped guard + an unchecked load whose provenance is
mechanically validated at the MIR→codegen boundary
(unchecked_load_validator + its stage2 teeth).

Placement note (recorded deviation): the String method lives in
std.text (`implement String`), NOT std.core — `Result<Byte,
err.IndexError>` cannot be declared in std.core because std.err
imports std.core (cycle).  Callers import std.text for method
visibility.  Gating the primitive away from user code was judged
INFEASIBLE: 71 e2e fixtures call it directly (frozen corpus); it is
documented-internal instead.

Pinned here:
  1. String.byte_at: exact Ok(Byte) and exact Err(IndexError) fields
     (container id `std.core:String`, requested index) for negative
     AND positive OOB — all in a NOTHROW caller, no try anywhere;
  2. StringByteView.byte_at: the same with VIEW-RELATIVE index and
     container id `std.text:StringByteView`;
  3. NO exception path: a deliberately wrapped try/catch around both
     methods never enters its catch arm — OOB arrives as Err data;
  4. internal fast path: valid primitive reads succeed; OOB is
     FAIL-CLOSED — exactly SIGABRT with the AssertLoc diagnostic
     ("string byte access out of range ...") on stderr;
  5. Result reads do not retain/allocate/copy — proven count-exactly
     by op_reads in test_string_byte_view_counts.py (cross-ref);
  6. parser/regex hot paths keep the range-proven internal source
     (_StringByteSource.read) — no per-byte Results (regex zero-retain
     subtraction proof lives in the counts harness; this file pins
     that the source path still compiles nothrow and reads correctly).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

RESULT_SRC = r"""module main;

import std.core as core;
import std.text as text;
import std.err;
import std.console as cons;

pub fn main() nothrow -> Int {
	var s = "ab";
	s = s + "c";

	// 1. String.byte_at — exact Ok and exact Err, nothrow caller.
	match s.byte_at(1) {
		Ok(b) => { if cast<Int>(b) != 98 { return 1; } },
		Err(e) => { return 2; }
	}
	match s.byte_at(9999) {
		Ok(b) => { return 3; },
		Err(e) => {
			if e.index != 9999 { return 4; }
			if e.container_id != "std.core:String" { return 5; }
		}
	}
	match s.byte_at(0 - 4) {
		Ok(b) => { return 6; },
		Err(e) => {
			if e.index != 0 - 4 { return 7; }
			if e.container_id != "std.core:String" { return 8; }
		}
	}

	// 2. view byte_at — view-relative, its own container id.
	var sub = text.byte_view_all(s);
	match text.byte_view(s, 1, 2) { Ok(x) => { sub = move x; }, Err(e) => { return 9; } }
	match sub.byte_at(0) {
		Ok(b) => { if cast<Int>(b) != 98 { return 10; } },
		Err(e) => { return 11; }
	}
	match sub.byte_at(2) {
		Ok(b) => { return 12; },
		Err(e) => {
			if e.index != 2 { return 13; }
			if e.container_id != "std.text:StringByteView" { return 14; }
		}
	}
	match sub.byte_at(0 - 1) {
		Ok(b) => { return 15; },
		Err(e) => { if e.index != 0 - 1 { return 16; } }
	}

	// 3. NO exception path: catches around both methods never fire —
	// OOB arrives as Err data inside the try body.
	var err_seen = 0;
	try {
		match s.byte_at(12345) {
			Ok(b) => { return 17; },
			Err(e) => { err_seen = err_seen + 1; }
		}
		match sub.byte_at(12345) {
			Ok(b) => { return 18; },
			Err(e) => { err_seen = err_seen + 1; }
		}
	} catch std.err:IndexError(e) {
		return 19;
	} catch {
		return 20;
	}
	if err_seen != 2 { return 21; }

	// 6. the range-proven internal source path: nothrow, correct.
	val src = text._byte_source(sub);
	if src.size() != 2 { return 22; }
	if cast<Int>(src.read(1)) != 99 { return 23; }

	// internal primitive, valid read.
	if cast<Int>(core.string_byte_at(s, 0)) != 97 { return 24; }

	cons.println("byte access result OK");
	return 0;
}
"""

PRIMITIVE_ABORT_SRC = r"""module main;

import std.core as core;

pub fn main() nothrow -> Int {
	val s = "abc";
	// documented-internal primitive: OOB is FAIL-CLOSED (abort with
	// diagnostic), never a value, never an exception.
	return cast<Int>(core.string_byte_at(s, 9999));
}
"""


def _compile(tmp_path: Path, src_text: str, name: str):
	src = tmp_path / f"{name}.drift"
	src.write_text(src_text)
	out_bin = tmp_path / f"{name}.bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	assert res.returncode == 0, f"compile failed:\n{(res.stdout + res.stderr)[:2000]}"
	return out_bin


def test_public_byte_access_is_result_data(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path, RESULT_SRC, "result_api")
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, (
		f"exit={run.returncode} (failing pin #)\n{run.stderr[:500]}"
	)
	assert "byte access result OK" in run.stdout


def test_primitive_oob_fails_closed_with_diagnostic(tmp_path: Path) -> None:
	out_bin = _compile(tmp_path, PRIMITIVE_ABORT_SRC, "prim_abort")
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == -6, f"expected SIGABRT, got {run.returncode}: {run.stderr[:300]}"
	assert "string byte access out of range" in run.stderr, run.stderr[:400]
	assert "assertion failed" in run.stderr, run.stderr[:400]
