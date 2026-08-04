# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Extracted stored lambdas must not leak callsite side tables to the parent.

A deferred stored lambda is typed inside its enclosing function's checker
state, so its body's callsites populate the enclosing
`call_info_by_callsite_id`.  `_apply_fnptr_consts` then replaces the stored
`HLambda` with an `HFnPtrConst` — the body (and its callsites) leaves the
parent's finalized HIR — but the parent `TypedFn` used to detach the WHOLE
unpartitioned map.  The strict reverse intrinsic validator
(`_validate_intrinsic_callinfo`) then correctly reported the orphan:
`E_INTRINSIC_CALLINFO_MISSING_NODE` on a valid program (a stored lambda
containing `core.callback0`).

The fix partitions callsite-indexed tables by finalized-body OWNERSHIP at
`TypedFn` construction: a full post-rewrite walk that DOES descend into
lambdas still present in the HIR (immediate IIFEs / callback-argument
lambdas keep their entries — the hidden-lambda worklists read them from the
origin function's map), while extracted bodies' entries stay off the parent
(the extracted lambda's independent re-check re-records its own).  The
validator stays strict.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

STORED_NESTED_CALLBACK = """
module repro;

import std.core as core;

pub fn main() nothrow -> Int {
	val outer = | b: Bool | => {
		val inner: core.Callback0<String> = core.callback0(|| => {
			return "s";
		});
		val s = inner.call();
		(s.byte_length() - 1)
	};
	return outer(true);
}
"""

PARENT_AND_STORED_CALLBACKS = """
module repro;

import std.core as core;

pub fn main() nothrow -> Int {
	val direct: core.Callback0<Int> = core.callback0(|| => {
		return 3;
	});
	val outer = | b: Bool | => {
		val inner: core.Callback0<Int> = core.callback0(|| => {
			return 4;
		});
		inner.call()
	};
	return direct.call() + outer(true) - 7;
}
"""

IIFE_NESTED_CALLBACK = """
module repro;

import std.core as core;

pub fn main() nothrow -> Int {
	val r = (| b: Bool | => {
		val inner: core.Callback0<Int> = core.callback0(|| => {
			return 5;
		});
		inner.call()
	})(true);
	return r - 5;
}
"""


def _build_run(tmp_path: Path, source: str) -> None:
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
	err = build.stdout + build.stderr
	assert build.returncode == 0, err
	assert "E_INTRINSIC_CALLINFO_MISSING_NODE" not in err, err
	assert "Traceback" not in err, err
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)


def test_stored_lambda_with_nested_callback_compiles_and_runs(tmp_path: Path) -> None:
	# The minimal red shape: pre-fix this failed with
	# E_INTRINSIC_CALLINFO_MISSING_NODE (orphaned callback0 intrinsic
	# entry on the parent after extraction).
	_build_run(tmp_path, STORED_NESTED_CALLBACK)


def test_parent_and_extracted_callbacks_coexist(tmp_path: Path) -> None:
	# The parent's OWN direct callback0 entry must survive the partition
	# while the extracted lambda's entry moves with its re-check; both
	# callbacks execute.
	_build_run(tmp_path, PARENT_AND_STORED_CALLBACKS)


def test_immediate_iife_nested_callback_stays_green(tmp_path: Path) -> None:
	# Counter-boundary: an IIFE's body remains reachable in the parent HIR
	# — the ownership filter must not equate every nested lambda with an
	# extracted function boundary.
	_build_run(tmp_path, IIFE_NESTED_CALLBACK)


def test_parent_typedfn_owns_only_reachable_callsites() -> None:
	# Structural map pin (direct primary-checker harness): after checking
	# a function whose stored lambda was extracted to an HFnPtrConst,
	# every callsite-indexed entry detached on the parent TypedFn resolves
	# to a call node reachable from the parent's FINALIZED HIR (descending
	# into lambdas still present in that HIR).  Distinguishes the parent
	# map from the LambdaFnSpec/hidden-function view — it does not merely
	# suppress the intrinsic diagnostic.
	from lang.driftc import stage1 as H
	from lang.driftc.core.function_id import FunctionId
	from lang.driftc.core.types_core import TypeTable
	from lang.driftc.method_registry import CallableRegistry
	from lang.driftc.stage1.node_ids import default_should_descend, iter_hir_walk
	from lang.driftc.type_checker import TypeChecker

	# Stored captureless lambda whose body contains an inner immediate
	# lambda call; the stored lambda is invoked afterwards.
	inner_call = H.HCall(
		fn=H.HLambda(
			params=[],
			body_expr=None,
			body_block=H.HBlock(statements=[H.HExprStmt(expr=H.HLiteralInt(value=6))]),
		),
		args=[], kwargs=[],
	)
	stored = H.HLambda(
		params=[],
		body_expr=None,
		body_block=H.HBlock(statements=[H.HExprStmt(expr=inner_call)]),
	)
	outer_call = H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])
	body = H.HBlock(statements=[
		H.HLet(name="f", value=stored),
		H.HExprStmt(expr=outer_call),
	])
	table = TypeTable()
	result = TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		body,
		callable_registry=CallableRegistry(),
		visible_modules=(0,),
	)
	assert result.diagnostics == []
	typed = result.typed_fn
	reachable: set[int] = set()
	for obj in iter_hir_walk(typed.body, should_descend=default_should_descend):
		if isinstance(obj, (H.HCall, H.HMethodCall)) or (hasattr(H, "HInvoke") and isinstance(obj, H.HInvoke)):
			csid = getattr(obj, "callsite_id", None)
			if isinstance(csid, int):
				reachable.add(csid)
	orphans = sorted(set(typed.call_info_by_callsite_id) - reachable)
	assert orphans == [], (orphans, sorted(reachable))
	inst_orphans = sorted(set(typed.instantiations_by_callsite_id) - reachable)
	assert inst_orphans == [], (inst_orphans, sorted(reachable))
	# The pin is meaningful only if the stored lambda actually left the
	# finalized body (extraction happened).
	fnptr_consts = [
		obj for obj in iter_hir_walk(typed.body, should_descend=default_should_descend)
		if type(obj).__name__ == "HFnPtrConst"
	]
	assert fnptr_consts, "stored lambda was not extracted; pin would be vacuous"
