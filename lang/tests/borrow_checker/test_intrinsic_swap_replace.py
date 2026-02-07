# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableDecl, CallableKind, CallableSignature, Visibility
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, IntrinsicKind
from lang.driftc.stage1.node_ids import assign_node_ids, assign_callsite_ids
from lang.driftc.type_checker import TypedFn


def _typed_fn_with_call(
	call: H.HCall,
	*,
	call_resolutions: dict[int, object] | None = None,
	call_info: CallInfo | None = None,
) -> TypedFn:
	body = H.HBlock(statements=[H.HExprStmt(expr=call)])
	assign_node_ids(body)
	assign_callsite_ids(body)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	if call_info is not None:
		csid = getattr(call, "callsite_id", None)
		if isinstance(csid, int):
			call_info_by_callsite_id[csid] = call_info
	return TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types={},
		binding_names={},
		binding_mutable={},
		call_resolutions=call_resolutions or {},
		call_info_by_callsite_id=call_info_by_callsite_id,
	)


def _place(name: str, binding_id: int) -> H.HPlaceExpr:
	return H.HPlaceExpr(base=H.HVar(name=name, binding_id=binding_id))


def _typed_fn_with_borrowed_place(
	call: H.HCall,
	*,
	binding_types: dict[int, int],
	binding_names: dict[int, str],
	binding_mutable: dict[int, bool],
	call_info: CallInfo,
) -> TypedFn:
	body = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), binding_id=1, is_mutable=True),
			H.HLet(name="y", value=H.HLiteralInt(2), binding_id=2, is_mutable=True),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=_place("x", 1), is_mut=False),
				binding_id=3,
				is_mutable=False,
			),
			H.HExprStmt(expr=call),
		]
	)
	assign_node_ids(body)
	assign_callsite_ids(body)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	csid = getattr(call, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = call_info
	return TypedFn(
		fn_id=FunctionId(module="main", name="main", ordinal=0),
		name="main",
		params=[],
		param_bindings=[],
		locals=[1, 2, 3],
		body=body,
		expr_types={},
		binding_for_var={},
		binding_types=binding_types,
		binding_names=binding_names,
		binding_mutable=binding_mutable,
		call_resolutions={},
		call_info_by_callsite_id=call_info_by_callsite_id,
	)


def test_user_defined_swap_is_not_intrinsic_without_callinfo() -> None:
	table = TypeTable()
	call = H.HCall(
		fn=H.HVar(name="swap"),
		args=[H.HLiteralInt(1), H.HLiteralInt(2)],
		kwargs=[],
	)
	fn_id = FunctionId(module="main", name="swap", ordinal=0)
	typed_fn = _typed_fn_with_call(
		call,
		call_resolutions={
			call.node_id: CallableDecl(
				callable_id=1,
				name="swap",
				kind=CallableKind.FREE_FUNCTION,
				module_id=0,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=(), result_type=table.ensure_unknown()),
				fn_id=fn_id,
			)
		},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id=None, enable_auto_borrow=True)
	diags = bc.check_block(typed_fn.body)
	assert diags == []


def test_intrinsic_swap_requires_callinfo() -> None:
	table = TypeTable()
	call = H.HCall(
		fn=H.HVar(name="swap"),
		args=[_place("x", 1), _place("y", 2)],
		kwargs=[],
	)
	info = CallInfo(
		target=CallTarget.intrinsic(IntrinsicKind.SWAP),
		sig=CallSig(param_types=(), user_ret_type=table.ensure_void(), can_throw=False),
	)
	int_ty = table.ensure_int()
	ref_ty = table.ensure_ref(int_ty)
	typed_fn = _typed_fn_with_borrowed_place(
		call,
		binding_types={1: int_ty, 2: int_ty, 3: ref_ty},
		binding_names={1: "x", 2: "y", 3: "r"},
		binding_mutable={1: True, 2: True, 3: False},
		call_info=info,
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id=None, enable_auto_borrow=True)
	diags = bc.check_block(typed_fn.body)
	assert any("cannot write to 'x' while it is borrowed" in d.message for d in diags)


def test_user_defined_replace_is_not_intrinsic_without_callinfo() -> None:
	table = TypeTable()
	call = H.HCall(
		fn=H.HVar(name="replace"),
		args=[H.HLiteralInt(1), H.HLiteralInt(2)],
		kwargs=[],
	)
	fn_id = FunctionId(module="main", name="replace", ordinal=0)
	typed_fn = _typed_fn_with_call(
		call,
		call_resolutions={
			call.node_id: CallableDecl(
				callable_id=1,
				name="replace",
				kind=CallableKind.FREE_FUNCTION,
				module_id=0,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=(), result_type=table.ensure_unknown()),
				fn_id=fn_id,
			)
		},
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id=None, enable_auto_borrow=True)
	diags = bc.check_block(typed_fn.body)
	assert diags == []


def test_intrinsic_replace_requires_callinfo() -> None:
	table = TypeTable()
	call = H.HCall(
		fn=H.HVar(name="replace"),
		args=[_place("x", 1), H.HLiteralInt(2)],
		kwargs=[],
	)
	info = CallInfo(
		target=CallTarget.intrinsic(IntrinsicKind.REPLACE),
		sig=CallSig(param_types=(), user_ret_type=table.ensure_void(), can_throw=False),
	)
	int_ty = table.ensure_int()
	ref_ty = table.ensure_ref(int_ty)
	typed_fn = _typed_fn_with_borrowed_place(
		call,
		binding_types={1: int_ty, 2: int_ty, 3: ref_ty},
		binding_names={1: "x", 2: "y", 3: "r"},
		binding_mutable={1: True, 2: True, 3: False},
		call_info=info,
	)
	bc = BorrowChecker.from_typed_fn(typed_fn, type_table=table, signatures_by_id=None, enable_auto_borrow=True)
	diags = bc.check_block(typed_fn.body)
	assert any("cannot write to 'x' while it is borrowed" in d.message for d in diags)
