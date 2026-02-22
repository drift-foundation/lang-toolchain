#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2026-02-20
"""Borrow-checker regressions for HInvoke optional-ref loans and lambda escape."""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.type_checker import TypedFn


def test_optional_ref_return_via_invoke_keeps_source_borrowed() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_mut_int_ty = table.ensure_ref_mut(int_ty)
	opt_base = table.ensure_optional_base()
	opt_ref_mut_int_ty = table.ensure_variant_instantiated(opt_base, [ref_mut_int_ty])
	unknown_ty = table.ensure_unknown()
	x_id = 1
	fp_id = 2
	a_id = 3
	b_id = 4
	invoke1 = H.HInvoke(callee=H.HVar(name="fp", binding_id=fp_id), args=[H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=True)], kwargs=[], callsite_id=101)
	invoke2 = H.HInvoke(callee=H.HVar(name="fp", binding_id=fp_id), args=[H.HBorrow(subject=H.HVar(name="x", binding_id=x_id), is_mut=True)], kwargs=[], callsite_id=102)
	body = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
			H.HLet(name="fp", value=H.HLiteralInt(0), binding_id=fp_id, is_mutable=False),
			H.HLet(name="a", value=invoke1, binding_id=a_id, is_mutable=False),
			H.HLet(name="b", value=invoke2, binding_id=b_id, is_mutable=False),
		]
	)
	assign_node_ids(body)
	call_info = {
		101: CallInfo(target=CallTarget.indirect(callee_node_id=invoke1.callee.node_id), sig=CallSig(param_types=(ref_mut_int_ty,), user_ret_type=opt_ref_mut_int_ty, can_throw=False, includes_callee=False)),
		102: CallInfo(target=CallTarget.indirect(callee_node_id=invoke2.callee.node_id), sig=CallSig(param_types=(ref_mut_int_ty,), user_ret_type=opt_ref_mut_int_ty, can_throw=False, includes_callee=False)),
	}
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, fp_id, a_id, b_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, fp_id: unknown_ty, a_id: opt_ref_mut_int_ty, b_id: opt_ref_mut_int_ty},
		binding_names={x_id: "x", fp_id: "fp", a_id: "a", b_id: "b"},
		binding_mutable={x_id: True, fp_id: False, a_id: False, b_id: False},
		call_info_by_callsite_id=call_info,
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	diags = bc.check_block(typed_fn.body)
	assert any("mutable borrow" in d.message for d in diags), diags


def test_invoke_rejects_escaping_lambda_with_borrowed_capture() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	unknown_ty = table.ensure_unknown()
	x_id = 1
	sink_id = 2
	invoke = H.HInvoke(
		callee=H.HVar(name="sink", binding_id=sink_id),
		args=[
			H.HLambda(
				params=[],
				body_expr=H.HLiteralInt(0),
				body_block=H.HBlock(statements=[H.HAssign(target=H.HVar(name="x", binding_id=x_id), value=H.HLiteralInt(2))]),
			)
		],
		kwargs=[],
		callsite_id=201,
	)
	body = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=x_id, is_mutable=True),
			H.HLet(name="sink", value=H.HLiteralInt(0), binding_id=sink_id, is_mutable=False),
			H.HExprStmt(expr=invoke),
		]
	)
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[x_id, sink_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={x_id: int_ty, sink_id: unknown_ty},
		binding_names={x_id: "x", sink_id: "sink"},
		binding_mutable={x_id: True, sink_id: False},
		call_info_by_callsite_id={
			201: CallInfo(
				target=CallTarget.indirect(callee_node_id=invoke.callee.node_id),
				sig=CallSig(param_types=(unknown_ty,), user_ret_type=int_ty, can_throw=False, includes_callee=False),
			)
		},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	diags = bc.check_block(typed_fn.body)
	assert any("borrowed captures are non-escaping in v0" in d.message for d in diags), diags


def test_invoke_sig_construction_uses_canonical_fnsignature_fields() -> None:
	"""Pin that _resolve_sig_for_call for HInvoke produces a FnSignature with
	correct canonical fields (return_type_id, declared_can_throw, declared_throws)
	after the Phase 5 cleanup removed legacy field names."""
	table = TypeTable()
	int_ty = table.ensure_int()
	unknown_ty = table.ensure_unknown()
	fp_id = 1
	r_id = 2
	invoke = H.HInvoke(callee=H.HVar(name="fp", binding_id=fp_id), args=[H.HLiteralInt(42)], kwargs=[], callsite_id=301)
	body = H.HBlock(
		statements=[
			H.HLet(name="fp", value=H.HLiteralInt(0), binding_id=fp_id, is_mutable=False),
			H.HLet(name="r", value=invoke, binding_id=r_id, is_mutable=False),
		]
	)
	assign_node_ids(body)
	typed_fn = TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[fp_id, r_id],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={fp_id: unknown_ty, r_id: int_ty},
		binding_names={fp_id: "fp", r_id: "r"},
		binding_mutable={fp_id: False, r_id: False},
		call_info_by_callsite_id={
			301: CallInfo(
				target=CallTarget.indirect(callee_node_id=invoke.callee.node_id),
				sig=CallSig(param_types=(int_ty,), user_ret_type=int_ty, can_throw=False, includes_callee=False),
			)
		},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id={})
	diags = bc.check_block(typed_fn.body)
	assert diags == [], f"simple HInvoke with Int param should produce no borrow diagnostics: {diags}"
