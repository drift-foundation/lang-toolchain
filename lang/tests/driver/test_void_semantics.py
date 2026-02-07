from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature, Checker
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget


def _void_sig_table():
	table = TypeTable()
	void_ty = table.ensure_void()
	return table, void_ty


def _call_info_for_call(
	*,
	fn_name: str,
	callee_name: str,
	callsite_id: int,
	param_types: list[int],
	return_type: int,
	can_throw: bool = False,
) -> dict[str, dict[int, CallInfo]]:
	fn_id = FunctionId(module="main", name=fn_name, ordinal=0)
	callee_id = FunctionId(module="main", name=callee_name, ordinal=0)
	info = CallInfo(
		target=CallTarget.direct(callee_id),
		sig=CallSig(
			param_types=tuple(param_types),
			user_ret_type=return_type,
			can_throw=can_throw,
		),
	)
	return {fn_id: {callsite_id: info}}


def test_void_function_cannot_return_value():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="main", name="v", ordinal=0)
	sigs = {fn_id: FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False)}
	funcs = {fn_id: H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))])}

	checker = Checker(
		signatures_by_id=sigs,
		hir_blocks_by_id=funcs,
		call_info_by_callsite_id={},
		type_table=table,
	)
	checked = checker.check_by_id(funcs.keys())

	assert any("cannot return a value from a Void function" in d.message for d in checked.diagnostics)


def test_non_void_function_must_return_value():
	table = TypeTable()
	int_ty = table.ensure_int()
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	sigs = {fn_id: FnSignature(name="f", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	funcs = {fn_id: H.HBlock(statements=[H.HReturn(value=None)])}

	checker = Checker(
		signatures_by_id=sigs,
		hir_blocks_by_id=funcs,
		call_info_by_callsite_id={},
		type_table=table,
	)
	checked = checker.check_by_id(funcs.keys())

	assert any("non-void function must return a value" in d.message for d in checked.diagnostics)


def test_void_call_cannot_be_bound():
	table, void_ty = _void_sig_table()
	fn_id_v = FunctionId(module="main", name="v", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	sigs = {
		fn_id_v: FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		fn_id_main: FnSignature(name="main", param_type_ids=[], return_type_id=table.ensure_int(), declared_can_throw=False),
	}
	call = H.HCall(fn=H.HVar(name="v"), args=[])
	call.node_id = 1
	call.callsite_id = 1
	funcs = {
		fn_id_v: H.HBlock(statements=[H.HReturn(value=None)]),
		fn_id_main: H.HBlock(statements=[H.HLet(name="x", value=call), H.HReturn(value=H.HLiteralInt(value=0))]),
	}

	call_info_by_id = _call_info_for_call(
		fn_name="main",
		callee_name="v",
		callsite_id=call.callsite_id,
		param_types=[],
		return_type=void_ty,
	)
	checker = Checker(
		signatures_by_id=sigs,
		hir_blocks_by_id=funcs,
		type_table=table,
		call_info_by_callsite_id=call_info_by_id,
	)
	checked = checker.check_by_id(funcs.keys())

	assert any("cannot bind a Void value" in d.message for d in checked.diagnostics)


def test_void_call_allowed_in_expr_stmt():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	fn_id_v = FunctionId(module="main", name="v", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	sigs = {
		fn_id_v: FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		fn_id_main: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		fn_id_v: H.HBlock(statements=[H.HReturn(value=None)]),
		fn_id_main: H.HBlock(
			statements=[
				H.HExprStmt(expr=H.HCall(fn=H.HVar(name="v"), args=[])),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	_, checked = compile_stubbed_funcs(func_hirs=funcs, signatures=sigs, type_table=table, return_checked=True)

	assert checked.diagnostics == []


def test_void_not_allowed_in_ternary_branches():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	fn_id_v = FunctionId(module="main", name="v", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	sigs = {
		fn_id_v: FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		fn_id_main: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		fn_id_v: H.HBlock(statements=[H.HReturn(value=None)]),
		fn_id_main: H.HBlock(
			statements=[
				H.HLet(
					name="x",
					value=H.HTernary(
						cond=H.HLiteralBool(value=True),
						then_expr=H.HCall(fn=H.HVar(name="v"), args=[]),
						else_expr=H.HLiteralInt(value=1),
					),
				),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	call = funcs[fn_id_main].statements[0].value.then_expr
	call.node_id = 2
	call.callsite_id = 2
	call_info_by_id = _call_info_for_call(
		fn_name="main",
		callee_name="v",
		callsite_id=call.callsite_id,
		param_types=[],
		return_type=void_ty,
	)
	checker = Checker(
		signatures_by_id=sigs,
		hir_blocks_by_id=funcs,
		type_table=table,
		call_info_by_callsite_id=call_info_by_id,
	)
	checked = checker.check_by_id(funcs.keys())

	assert any("Void value is not allowed in a ternary expression" in d.message for d in checked.diagnostics)


def test_void_not_allowed_in_array_literal():
	table, void_ty = _void_sig_table()
	int_ty = table.ensure_int()
	fn_id_v = FunctionId(module="main", name="v", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	sigs = {
		fn_id_v: FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		fn_id_main: FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	funcs = {
		fn_id_v: H.HBlock(statements=[H.HReturn(value=None)]),
		fn_id_main: H.HBlock(
			statements=[
				H.HLet(
					name="arr",
					value=H.HArrayLiteral(elements=[H.HCall(fn=H.HVar(name="v"), args=[]), H.HLiteralInt(value=2)]),
				),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	call = funcs[fn_id_main].statements[0].value.elements[0]
	call.node_id = 3
	call.callsite_id = 3
	call_info_by_id = _call_info_for_call(
		fn_name="main",
		callee_name="v",
		callsite_id=call.callsite_id,
		param_types=[],
		return_type=void_ty,
	)
	checker = Checker(
		signatures_by_id=sigs,
		hir_blocks_by_id=funcs,
		type_table=table,
		call_info_by_callsite_id=call_info_by_id,
	)
	checked = checker.check_by_id(funcs.keys())

	assert any("Void value is not allowed in an array literal" in d.message for d in checked.diagnostics)
