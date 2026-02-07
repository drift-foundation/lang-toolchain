# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.checker import Checker, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget


def _catch_arm(result_expr: H.HExpr) -> H.HTryExprArm:
	return H.HTryExprArm(
		event_fqn=None,
		binder=None,
		block=H.HBlock(statements=[]),
		result=result_expr,
	)


def _try_expr(attempt: H.HExpr, result_expr: H.HExpr) -> H.HTryExpr:
	return H.HTryExpr(attempt=attempt, arms=[_catch_arm(result_expr)])


def test_try_expr_rejects_void_attempt() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	void_ty = table.ensure_void()

	sigs = {
		"v": FnSignature(name="v", param_type_ids=[], return_type_id=void_ty, declared_can_throw=False),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	fn_id_v = FunctionId(module="main", name="v", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	call_v = H.HCall(fn=H.HVar(name="v"), args=[])
	call_v.node_id = 1
	call_v.callsite_id = 1
	funcs = {
		"v": H.HBlock(statements=[H.HReturn(value=None)]),
		"main": H.HBlock(
			statements=[
				H.HLet(
					name="x",
					value=_try_expr(
						call_v,
						H.HLiteralInt(value=0),
					),
				),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	call_info_by_id = {
		fn_id_main: {
			1: CallInfo(
				target=CallTarget.direct(fn_id_v),
				sig=CallSig(param_types=(), user_ret_type=void_ty, can_throw=False),
			)
		}
	}
	checker = Checker(
		signatures_by_id={fn_id_v: sigs["v"], fn_id_main: sigs["main"]},
		hir_blocks_by_id={fn_id_v: funcs["v"], fn_id_main: funcs["main"]},
		call_info_by_callsite_id=call_info_by_id,
		type_table=table,
	)
	checked = checker.check_by_id([fn_id_v, fn_id_main])
	msgs = [d.message for d in checked.diagnostics]
	assert any("try/catch attempt must produce a value (not Void)" in m for m in msgs)


def test_try_expr_rejects_nothrow_ternary_attempt() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()

	sigs = {"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	attempt = H.HTernary(
		cond=H.HLiteralBool(value=True),
		then_expr=H.HLiteralInt(value=1),
		else_expr=H.HLiteralInt(value=2),
	)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	funcs = {
		"main": H.HBlock(
			statements=[
				H.HLet(name="x", value=_try_expr(attempt, H.HLiteralInt(value=0))),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		)
	}

	checker = Checker(
		signatures_by_id={fn_id_main: sigs["main"]},
		hir_blocks_by_id={fn_id_main: funcs["main"]},
		call_info_by_callsite_id={fn_id_main: {}},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id_main])
	msgs = [d.message for d in checked.diagnostics]
	assert any("'try' applied to a nothrow expression" in m for m in msgs)


def test_try_expr_allows_may_throw_ternary_attempt() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()

	sigs = {
		"may_fail": FnSignature(
			name="may_fail",
			param_type_ids=[int_ty],
			return_type_id=int_ty,
			declared_can_throw=True,
			error_type_id=table.ensure_error(),
		),
		"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False),
	}
	call_fail = H.HCall(fn=H.HVar(name="may_fail"), args=[H.HLiteralInt(value=1)])
	call_fail.node_id = 2
	call_fail.callsite_id = 2
	attempt = H.HTernary(
		cond=H.HLiteralBool(value=True),
		then_expr=call_fail,
		else_expr=H.HLiteralInt(value=2),
	)
	fn_id_fail = FunctionId(module="main", name="may_fail", ordinal=0)
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	funcs = {
		"may_fail": H.HBlock(statements=[H.HReturn(value=H.HLiteralInt(value=1))]),
		"main": H.HBlock(
			statements=[
				H.HLet(name="x", value=_try_expr(attempt, H.HLiteralInt(value=0))),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		),
	}

	call_info_by_id = {
		fn_id_main: {
			2: CallInfo(
				target=CallTarget.direct(fn_id_fail),
				sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=True),
			)
		}
	}
	checker = Checker(
		signatures_by_id={fn_id_fail: sigs["may_fail"], fn_id_main: sigs["main"]},
		hir_blocks_by_id={fn_id_fail: funcs["may_fail"], fn_id_main: funcs["main"]},
		call_info_by_callsite_id=call_info_by_id,
		type_table=table,
	)
	checked = checker.check_by_id([fn_id_fail, fn_id_main])
	assert not checked.diagnostics


def test_try_expr_allows_indexing_attempt() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()

	sigs = {"main": FnSignature(name="main", param_type_ids=[], return_type_id=int_ty, declared_can_throw=False)}
	arr_literal = H.HArrayLiteral(elements=[H.HLiteralInt(value=1), H.HLiteralInt(value=2)])
	attempt = H.HIndex(subject=H.HVar(name="arr"), index=H.HLiteralInt(value=0))
	fn_id_main = FunctionId(module="main", name="main", ordinal=0)
	funcs = {
		"main": H.HBlock(
			statements=[
				H.HLet(name="arr", value=arr_literal),
				H.HLet(name="x", value=_try_expr(attempt, H.HLiteralInt(value=0))),
				H.HReturn(value=H.HLiteralInt(value=0)),
			]
		)
	}

	checker = Checker(
		signatures_by_id={fn_id_main: sigs["main"]},
		hir_blocks_by_id={fn_id_main: funcs["main"]},
		call_info_by_callsite_id={fn_id_main: {}},
		type_table=table,
	)
	checked = checker.check_by_id([fn_id_main])
	msgs = [d.message for d in checked.diagnostics]
	assert not [m for m in msgs if "'try' applied to a nothrow expression" in m]
