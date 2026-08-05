# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Associated calls (`Type::fn(...)`) wrap Callback params canonically.

Pre-fix, `resolve_nonvariant_qualified_static_call` admitted a bare lambda
or fn-typed value at a concrete `Callback*` param through
`coerce_args_for_params`' silent INTERFACE retyping — no wrapper was ever
constructed, and lowering received a raw non-interface value under an
interface-typed slot: a bare lambda produced INVALID LLVM IR ("global
variable reference must have pointer type" at clang) and a named-fn arg an
internal traceback (`interface impl not found for interface value`); an
arity-mismatched lambda was also checker-silent.  The free-function path
already wrapped correctly (control below) — the gap was the
associated/static family only (Site 1 of the implicit-callback-wrap
matrix; confirmed pre-existing on clean HEAD, fixed here per
review-2026-08-05T04-27-45Z).

Now the assoc-call success path routes through the SAME canonical wrapper
authority as ctor fields / typed-let / return position
(`_try_wrap_arg_for_callback_field` → `_implicit_callback_wrap`), and an
arity-mismatched function value at a Callback slot is a real checker
diagnostic instead of silently-accepted invalid IR.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str) -> tuple[subprocess.CompletedProcess, Path]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	return build, out


def _build_run(tmp_path: Path, source: str) -> None:
	build, out = _compile(tmp_path, source)
	err = build.stdout + build.stderr
	assert build.returncode == 0, err
	assert "Traceback" not in err, err
	assert "clang failed" not in err, err
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)


def test_assoc_call_bare_lambda_to_callback1_compiles_and_runs(tmp_path: Path) -> None:
	# Pre-fix: checker-clean, invalid LLVM IR at clang.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	return S::take_cb(| x: Int | nothrow => x + 1) - 7;
}
""",
	)


def test_assoc_call_named_fn_to_callback1_compiles_and_runs(tmp_path: Path) -> None:
	# Pre-fix: checker-clean, NotImplementedError ("interface impl not
	# found for interface value") in the vtable lookup.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn add1(x: Int) nothrow -> Int { return x + 1; }
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	return S::take_cb(add1) - 7;
}
""",
	)


def test_assoc_call_explicit_wrap_still_accepted(tmp_path: Path) -> None:
	# Already-explicit `core.callback1(...)` must not be double-wrapped.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn add1(x: Int) nothrow -> Int { return x + 1; }
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	return S::take_cb(core.callback1(add1)) - 7;
}
""",
	)


def test_assoc_call_arity_mismatch_is_a_real_diagnostic(tmp_path: Path) -> None:
	# Pre-fix the arity-1 lambda at a Callback2 param was checker-silent
	# (failing only as invalid IR).  The boundary is now a clean checker
	# rejection — never a clang error or a traceback.
	build, _out = _compile(
		tmp_path,
		"""
module repro;
import std.core as core;
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback2<Int, Int, Int>) nothrow -> Int {
		return cb.call(1, 2);
	}
}
pub fn main() nothrow -> Int {
	return S::take_cb(| x: Int | nothrow => x + 1);
}
""",
	)
	err = build.stdout + build.stderr
	assert build.returncode != 0, err
	assert "Traceback" not in err, err
	assert "clang failed" not in err, err
	assert "arity" in err or "no overload" in err.lower(), err


def test_free_fn_bare_lambda_control_still_runs(tmp_path: Path) -> None:
	# Control: the free-function argument path wrapped correctly before
	# this fix and must stay green.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
	return cb.call(6);
}
pub fn main() nothrow -> Int {
	return take_cb(| x: Int | nothrow => x + 1) - 7;
}
""",
	)


# ---------------------------------------------------------------------------
# Round-3 pins (review-2026-08-05T05-08-53Z): the fn-typed-argument contract
# covers stored BINDINGS, not only bare names — static provenance is seeded
# at `val f = add1` (the initializer node's registered fnptr const) and
# propagates across immutable alias hops; a runtime-only fn value fails
# CLOSED with a checker diagnostic instead of the MIR iface-init invariant.
# ---------------------------------------------------------------------------

def test_assoc_call_stored_named_fn_binding_compiles_and_runs(tmp_path: Path) -> None:
	# Reviewer repro (probe_assoc_callback_fn_alias.drift): pre-fix this
	# failed with "MIR invariant violation: MoveOut of uninitialized
	# iface local 'f'".
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn add1(x: Int) nothrow -> Int { return x + 1; }
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	val f = add1;
	return S::take_cb(f) - 7;
}
""",
	)


def test_assoc_call_second_alias_hop_compiles_and_runs(tmp_path: Path) -> None:
	# Immutable second alias — the propagation boundary the review asked
	# to pin.
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
fn add1(x: Int) nothrow -> Int { return x + 1; }
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	val f = add1;
	val g = f;
	return S::take_cb(g) - 7;
}
""",
	)


def test_assoc_call_stored_lambda_binding_compiles_and_runs(tmp_path: Path) -> None:
	# Finalized-pending-lambda binding at the assoc Callback param (the
	# binding-provenance table's original producer).
	_build_run(
		tmp_path,
		"""
module repro;
import std.core as core;
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	val f = | x: Int | => { x + 1 };
	return S::take_cb(f) - 7;
}
""",
	)


def test_assoc_call_mutable_fn_binding_is_a_clean_rejection(tmp_path: Path) -> None:
	# A `var` binding can be re-assigned away from any recorded constant,
	# so v1 cannot construct a static callback from it: clean checker
	# diagnostic, never the MIR iface-init invariant.
	build, _out = _compile(
		tmp_path,
		"""
module repro;
import std.core as core;
fn add1(x: Int) nothrow -> Int { return x + 1; }
struct S {}
implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(6);
	}
}
pub fn main() nothrow -> Int {
	var f = add1;
	return S::take_cb(f) - 7;
}
""",
	)
	err = build.stdout + build.stderr
	assert build.returncode != 0, err
	assert "callback argument must be a statically-known function" in err, err
	assert "MIR invariant" not in err, err
	assert "Traceback" not in err, err
