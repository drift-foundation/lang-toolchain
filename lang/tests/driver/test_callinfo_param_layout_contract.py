from __future__ import annotations

import re

from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, IntrinsicKind


def _checker_for(
	*,
	block: H.HBlock,
	call_info: CallInfo,
	callsite_id: int,
	table: TypeTable,
) -> tuple[Checker, FunctionId]:
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	sig = FnSignature(name="main", param_type_ids=[], return_type_id=table.ensure_int(), declared_can_throw=False)
	call_info_by_fn = {fn_id: {callsite_id: call_info}}
	checker = Checker(
		signatures_by_id={fn_id: sig},
		hir_blocks_by_id={fn_id: block},
		call_info_by_callsite_id=call_info_by_fn,
		type_table=table,
	)
	return checker, fn_id


def test_callinfo_param_layout_mismatch_for_indirect_method_reports_checker_bug() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HMethodCall(receiver=H.HLiteralInt(value=7), method_name="run", args=[H.HLiteralInt(value=1)])
	call.callsite_id = 1
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.indirect(99),
		sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=1, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("internal: CallInfo param layout mismatch for method call (checker bug)" in d.message for d in errors)


def test_callinfo_includes_callee_on_call_reports_checker_bug() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HCall(fn=H.HVar(name="f"), args=[H.HLiteralInt(value=1)])
	call.callsite_id = 2
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.direct(FunctionId(module="main", name="f", ordinal=0)),
		sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False, includes_callee=True),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=2, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("internal: CallInfo includes_callee set on call (checker bug)" in d.message for d in errors)


def test_callinfo_param_layout_valid_for_direct_method_no_contract_error() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HMethodCall(receiver=H.HLiteralInt(value=7), method_name="run", args=[H.HLiteralInt(value=1)])
	call.callsite_id = 3
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.direct(FunctionId(module="main", name="run", ordinal=0)),
		sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=3, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("internal: CallInfo param layout mismatch" in d.message for d in errors)
	assert not any("internal: CallInfo includes_callee set on" in d.message for d in errors)


def test_callinfo_param_layout_mismatch_for_invoke_reports_checker_bug() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	invoke = H.HInvoke(callee=H.HVar(name="f"), args=[H.HLiteralInt(value=1)])
	invoke.callsite_id = 4
	block = H.HBlock(statements=[H.HExprStmt(expr=invoke), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.indirect(200),
		sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False, includes_callee=True),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=4, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "internal: CallInfo param layout mismatch for invoke (checker bug)" in d.message]
	assert matches
	assert any("target_kind=INDIRECT" in note for note in (matches[0].notes or []))


def test_callinfo_param_layout_mismatch_for_constructor_call_reports_checker_bug() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	struct_ty = table.declare_struct("main", "Point", ["x", "y"])
	call = H.HCall(fn=H.HVar(name="Point"), args=[H.HLiteralInt(value=1)])
	call.callsite_id = 5
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.constructor_struct(struct_ty),
		sig=CallSig(param_types=(int_ty, int_ty), user_ret_type=struct_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=5, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "internal: CallInfo param layout mismatch for call (checker bug)" in d.message]
	assert matches
	assert any("target_kind=CONSTRUCTOR" in note for note in (matches[0].notes or []))


def test_callinfo_target_shape_invoke_requires_indirect_target() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	invoke = H.HInvoke(callee=H.HVar(name="f"), args=[H.HLiteralInt(value=1)])
	invoke.callsite_id = 6
	block = H.HBlock(statements=[H.HExprStmt(expr=invoke), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.direct(FunctionId(module="main", name="f", ordinal=0)),
		sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=6, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("internal: invoke CallInfo target must be INDIRECT (checker bug)" in d.message for d in errors)


def test_callinfo_target_shape_allows_indirect_target_on_call() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HCall(fn=H.HVar(name="f"), args=[H.HLiteralInt(value=1)])
	call.callsite_id = 7
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.indirect(700),
		sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=7, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("internal: call CallInfo target must not be INDIRECT" in d.message for d in errors)


def test_callinfo_target_shape_allows_intrinsic_callback_target_on_call() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	call = H.HCall(fn=H.HVar(name="callback1"), args=[H.HLiteralInt(value=1)])
	call.callsite_id = 8
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.intrinsic(IntrinsicKind.CALLBACK1),
		sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=8, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not any("internal: invoke CallInfo target must be INDIRECT" in d.message for d in errors)
	assert not any("internal: call CallInfo target must not be INDIRECT" in d.message for d in errors)


def test_call_signature_type_mismatch_uses_symbolic_types_and_span() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	byte_ty = table.ensure_byte()
	call = H.HCall(fn=H.HVar(name="f"), args=[H.HLiteralInt(value=1)], loc=Span(file="main.drift", line=4, column=12))
	call.callsite_id = 9
	block = H.HBlock(statements=[H.HExprStmt(expr=call), H.HReturn(value=H.HLiteralInt(value=0))])
	info = CallInfo(
		target=CallTarget.direct(FunctionId(module="main", name="f", ordinal=0)),
		sig=CallSig(param_types=(byte_ty,), user_ret_type=int_ty, can_throw=False),
	)
	checker, fn_id = _checker_for(block=block, call_info=info, callsite_id=9, table=table)
	checked = checker.check_by_id([fn_id])
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	matches = [d for d in errors if "argument 0 to f has type Int, expected Byte" in d.message]
	assert matches, errors
	assert all(re.search(r"type \\d+, expected \\d+", d.message) is None for d in matches), matches
	assert all(d.span.line is not None and d.span.column is not None for d in matches), matches
