# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Executable CallInfo probes for P1.3.

The filename intentionally avoids pytest's default test_*.py discovery.  Run it
explicitly while this remains a handoff artifact under work/.
"""

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableRegistry
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.stage1.call_info import CallTargetKind
from lang.driftc.type_checker import TypeChecker


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
	assert info_callee_id(result, call) == call.fn.binding_id


def info_callee_id(result: object, call: H.HCall | H.HInvoke) -> int | None:
	info = result.typed_fn.call_info_by_callsite_id[call.callsite_id]
	return info.target.callee_node_id


def test_synthetic_hinvoke_lambda_infers_callinfo_without_expected_result() -> None:
	lam = _value_block(8)
	call = H.HInvoke(callee=lam, args=[], kwargs=[])
	table, result = _check([H.HExprStmt(expr=call)])
	_assert_inferred_int_callinfo(table, result, call)
	assert info_callee_id(result, call) == lam.node_id


def test_surface_direct_and_stored_calls_are_both_hcall_nodes() -> None:
	source = Path(__file__).with_name("repro_no_expected_result.drift")
	module, _table, _exceptions, diagnostics = parse_drift_to_hir(source)
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
