# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression pins for the two ICEs that blocked drift-query Slice 12
(reported 2026-07-12 against certified driftc 0.33.81 | abi 21, git d49486e0)
plus their same-root-cause siblings.

Regression-first discipline: the first two tests were committed FAILING (each
compile aborted with an internal contract failure) and flipped green with the
root-cause fixes; the sibling pins were verified to fail on the pre-fix
compiler during the fix's review.

1. issues/generic-lambda-match-result-ssa-ice/ — TWO distinct root causes:
   a) E-AUTO-90fc29aa: a lambda hoisted out of a generic INSTANTIATION was
      re-checked standalone without the origin's type-param bindings, so
      `core.Result<R, ...>` written inside the lambda body resolved `R` as an
      unknown nominal (ICE main repro + the E_VARIANT_CTOR_ARG_TYPE
      wrong-diagnostic sibling). Fixed by forwarding the origin TypedFn's
      `preseed_type_params` at both lambda re-check sites.
   b) E-AUTO-30f18b1b (spawn manifestation): the checker's lambda return
      inference (`_find_return_expr`) descended into NESTED lambda bodies, so
      an inner lambda's `return` typed the OUTER lambda's signature. Fixed by
      treating H.HLambda as a function boundary in the walk.

2. issues/mir-missing-binding-id-conditional-move-ice/
   E-AUTO-91e8ffe5: the checker resolved an unqualified module-level const
   inside a method body but never stamped `expr.module_id`; MIR lowering then
   re-derived the module from the MirFunc name, which for interface-impl
   methods ("Type::Iface::method") carries no module prefix — lookup missed
   and strict typed mode ICE'd. The 12-method interface in the report was a
   red herring; the const read behind the mode check was the trigger. Fixed
   by stamping the resolved module on the HVar at const resolution.

3. Duplicate extern "C" declares: found while verifying the spawn repro pair —
   two modules in one compilation unit each declaring the same C symbol
   emitted two LLVM `declare` lines and clang rejected the module. Pinned in
   lang/tests/driver/test_extern_c_declare_dedup.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_ISSUE_1 = ROOT / "issues" / "generic-lambda-match-result-ssa-ice" / "repro_ssa_ice.drift"
_ISSUE_1_SIBLING = ROOT / "issues" / "generic-lambda-match-result-ssa-ice" / "repro_variant_ctor_diag.drift"
_ISSUE_2 = ROOT / "issues" / "mir-missing-binding-id-conditional-move-ice" / "repro_single_file.drift"

# Reduced spawn-lambda manifestation (E-AUTO-30f18b1b family) without the
# original bundle's LMDB/FFI dependency: a cross-module generic driver call
# discarded (`val _ =`) inside a spawned Callback0 lambda, with the inner
# callback lambda's `return` being the first HReturn in the outer body's
# HIR walk.  Pre-fix, `_find_return_expr` crossed the lambda boundary and
# the outer lambda's signature became the inner's Result type
# ("SSA return type does not match declared signature ... in entry").
_SPAWN_STORE_SOURCE = """\
module store;

import std.core as core;

pub struct StoreError { pub msg: String }

pub fn with_txn<R>(label: String, var body: core.Callback1<Int, core.Result<R, StoreError>>) nothrow -> core.Result<R, StoreError> {
	val _ = label;
	return body.call(7);
}

pub fn txn_put(t: Int) nothrow -> core.Result<Void, StoreError> {
	val _ = t;
	return core.Result::Ok(core.void_value());
}
"""

_SPAWN_MAIN_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import store as st;

pub fn main() nothrow -> Int {
	val _vt = conc.spawn(core.callback0(|| => {
		val _ = st.with_txn("t", core.callback1(| t: Int | => {
			return st.txn_put(t);
		}));
		return 0;
	}));
	val _s = conc.sleep(conc.Duration(millis = 20));
	return 0;
}
"""

# Tight pin on ICE 2's actual root cause, minus the report's interface bulk:
# an unqualified module const read inside an interface-impl method AND an
# inherent method (both MirFunc name shapes lack a module prefix, so the
# pre-fix MIR fallback guessed the wrong module for the const lookup).
_CONST_IN_METHOD_SOURCE = """\
module main;

const K: Int = 5;

pub struct Box2 { v: Int }

pub interface Iface { fn get(self: &Self) nothrow -> Int; }

implement Iface for Box2 {
	pub fn get(self: &Box2) nothrow -> Int { return self.v + K; }
}

implement Box2 {
	pub fn bump(self: &Box2) nothrow -> Int { return self.v + K; }
}

pub fn main() nothrow -> Int {
	val b = Box2(v = 1);
	if b.get() != 6 { return 1; }
	if b.bump() != 6 { return 2; }
	return 0;
}
"""


def _compile_and_run(tmp_path: Path, source_files: list[Path]) -> None:
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *[str(p) for p in source_files], "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	run = subprocess.run([str(out)], capture_output=True, text=True,
	                     timeout=sanitizer_timeout(60))
	assert run.returncode == 0, f"run failed (exit {run.returncode}):\n{run.stderr[-800:]}"


def test_generic_lambda_match_result_compiles(tmp_path: Path) -> None:
	"""Pin for E-AUTO-90fc29aa (generic-lambda-match-result-ssa-ice)."""
	_compile_and_run(tmp_path, [_ISSUE_1])


def test_generic_lambda_variant_ctor_sibling_compiles(tmp_path: Path) -> None:
	"""Same root cause as E-AUTO-90fc29aa surfacing as a WRONG diagnostic:
	the concrete instantiation's payload was checked against an
	unsubstituted `R` (E_VARIANT_CTOR_ARG_TYPE 'have Int, expected R')."""
	_compile_and_run(tmp_path, [_ISSUE_1_SIBLING])


def test_spawn_lambda_discarded_generic_call_compiles(tmp_path: Path) -> None:
	"""Pin for the E-AUTO-30f18b1b spawn manifestation, reduced to drop the
	original bundle's LMDB dependency (verified to reproduce the identical
	SSA-contract failure on the pre-fix checker)."""
	store = tmp_path / "store.drift"
	store.write_text(_SPAWN_STORE_SOURCE)
	main = tmp_path / "main.drift"
	main.write_text(_SPAWN_MAIN_SOURCE)
	_compile_and_run(tmp_path, [main, store])


def test_impl_method_conditional_move_compiles(tmp_path: Path) -> None:
	"""Pin for E-AUTO-91e8ffe5 (mir-missing-binding-id-conditional-move-ice)."""
	_compile_and_run(tmp_path, [_ISSUE_2])


def test_const_read_in_methods_compiles(tmp_path: Path) -> None:
	"""Tight pin on ICE 2's root cause: unqualified module const read inside
	interface-impl and inherent methods (MirFunc names without a module
	prefix)."""
	src = tmp_path / "main.drift"
	src.write_text(_CONST_IN_METHOD_SOURCE)
	_compile_and_run(tmp_path, [src])
