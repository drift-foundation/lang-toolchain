# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""No-context lambda-call CallInfo inference boundary (P1.3).

The annotated-binding driver cases (`val r: Int = ...`) are CONTEXTUAL
typing: the declared type flows into the lambda's expected return before
its body is typed, so they stay green even if no-context inference
regresses to Unknown.  These tests pin the actual INFERENCE boundary: with
no expected result anywhere, each lambda-call route must record a concrete
`Int` in CallInfo (`sig.user_ret_type`), an INDIRECT target, and a
concrete call expression type.

Routes pinned independently:
- live direct IIFE: `HCall(fn=HLambda)`;
- actual stored-source shape: `HCall(fn=HVar)` resolving a pending stored
  lambda (this is what `val f = ...; f()` parses to — NOT `HInvoke`);
- the synthetic-HIR `HInvoke(callee=HLambda)` contract (ordinary
  stored-lambda syntax never emits this node — see the producer-shape
  pin);
- a full compile/run companion with unannotated bindings.

There is exactly ONE `HCall(fn=HLambda)` authority in `resolve_call_expr`;
the historical unreachable duplicate branch was deleted with this pin's
installation.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableRegistry
from lang.driftc.parser import parse_drift_to_hir, stdlib_root
from lang.driftc.stage1.call_info import CallTargetKind
from lang.driftc.type_checker import TypeChecker

ROOT = Path(__file__).resolve().parents[3]

NO_CONTEXT_SOURCE = """
module repro;

pub fn main() nothrow -> Int {
	val direct = (|| => { 6 })();
	val f = || => { 7 };
	val stored = f();
	return direct + stored - 13;
}
"""


def _value_block(result: int) -> H.HLambda:
	return H.HLambda(
		params=[],
		body_expr=None,
		body_block=H.HBlock(statements=[
			H.HExprStmt(expr=H.HLiteralInt(value=result)),
		]),
	)


def _check(statements: list[H.HStmt]) -> tuple[TypeTable, object]:
	table = TypeTable()
	result = TypeChecker(table).check_function(
		FunctionId(module="main", name="main", ordinal=0),
		H.HBlock(statements=statements),
		callable_registry=CallableRegistry(),
		visible_modules=(0,),
	)
	assert result.diagnostics == []
	return table, result


def _assert_inferred_int_callinfo(table: TypeTable, result: object, call: H.HExpr) -> None:
	assert isinstance(call.callsite_id, int)
	info = result.typed_fn.call_info_by_callsite_id[call.callsite_id]
	assert info.sig.user_ret_type == table.ensure_int()
	assert info.target.kind is CallTargetKind.INDIRECT
	assert result.typed_fn.expr_types[call.node_id] == table.ensure_int()


def _info_callee_id(result: object, call: "H.HCall | H.HInvoke") -> int | None:
	info = result.typed_fn.call_info_by_callsite_id[call.callsite_id]
	return info.target.callee_node_id


def test_direct_hcall_lambda_infers_callinfo_without_expected_result() -> None:
	lam = _value_block(6)
	call = H.HCall(fn=lam, args=[], kwargs=[])
	table, result = _check([H.HExprStmt(expr=call)])
	_assert_inferred_int_callinfo(table, result, call)
	assert result.typed_fn.expr_types[lam.node_id] != table.ensure_unknown()


def test_stored_source_shape_hcall_var_infers_callinfo_without_expected_result() -> None:
	lam = _value_block(7)
	call = H.HCall(fn=H.HVar(name="f"), args=[], kwargs=[])
	table, result = _check([
		H.HLet(name="f", value=lam),
		H.HExprStmt(expr=call),
	])
	_assert_inferred_int_callinfo(table, result, call)
	assert isinstance(call.fn, H.HVar)
	assert isinstance(call.fn.binding_id, int)
	assert _info_callee_id(result, call) == call.fn.binding_id


def test_synthetic_hinvoke_lambda_infers_callinfo_without_expected_result() -> None:
	# Synthetic-HIR contract only: ordinary stored-lambda syntax emits
	# HCall(fn=HVar), never HInvoke (see the producer-shape pin below).
	lam = _value_block(8)
	call = H.HInvoke(callee=lam, args=[], kwargs=[])
	table, result = _check([H.HExprStmt(expr=call)])
	_assert_inferred_int_callinfo(table, result, call)
	assert _info_callee_id(result, call) == lam.node_id


def test_surface_direct_and_stored_calls_are_both_hcall_nodes(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(NO_CONTEXT_SOURCE, encoding="utf-8")
	module, _table, _exceptions, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	main = next(block for fn_id, block in module.func_hirs.items() if fn_id.name == "main")
	lets = [stmt for stmt in main.statements if isinstance(stmt, H.HLet)]
	assert len(lets) == 3
	assert isinstance(lets[0].value, H.HCall)
	assert isinstance(lets[0].value.fn, H.HLambda)
	assert isinstance(lets[1].value, H.HLambda)
	assert isinstance(lets[2].value, H.HCall)
	assert isinstance(lets[2].value.fn, H.HVar)
	assert not any(isinstance(let.value, H.HInvoke) for let in lets)


def test_no_context_lambda_calls_compile_and_run(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(NO_CONTEXT_SOURCE, encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240))
	assert build.returncode == 0, build.stderr
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (run.returncode, run.stderr)
