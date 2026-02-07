#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Signature-driven auto-borrow tests."""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceBase, PlaceKind
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable, TypeId
from lang.driftc.method_registry import CallableDecl, CallableSignature, CallableKind, Visibility, SelfMode
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.stage1.node_ids import assign_node_ids


def _bc_with_sig(
	table: TypeTable,
	sig: FnSignature,
	*,
	fn_id: FunctionId | None = None,
	call_resolutions=None,
	binding_mutable: dict[int, bool] | None = None,
):
	unk: TypeId = table.ensure_unknown()
	fn_types = {PlaceBase(PlaceKind.LOCAL, 1, "x"): unk}
	base_lookup = lambda hv: PlaceBase(
		PlaceKind.LOCAL,
		getattr(hv, "binding_id", -1) if getattr(hv, "binding_id", None) is not None else -1,
		hv.name if hasattr(hv, "name") else str(hv),
	)
	if fn_id is None:
		fn_id = FunctionId(module="main", name=sig.name, ordinal=0)
	return BorrowChecker(
		type_table=table,
		fn_types=fn_types,
		binding_types=None,
		binding_mutable=binding_mutable,
		base_lookup=base_lookup,
		enable_auto_borrow=True,
		signatures_by_id={fn_id: sig},
		call_resolutions=call_resolutions,
	)


def test_hcall_signature_driven_auto_borrow_prevents_move():
	# Auto-borrow ensures the call does not consume x when a ref parameter is expected.
	block = H.HBlock(
		statements=[
			H.HLet(name="foo", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=2),
			H.HLet(name="x", value=H.HLiteralString("s"), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("foo", binding_id=2), args=[H.HVar("x", binding_id=1)])),
			H.HExprStmt(expr=H.HVar("x", binding_id=1)),
		]
	)
	table = TypeTable()
	ref_param = table.ensure_ref(table.ensure_unknown())
	ref_sig = FnSignature(name="foo", param_type_ids=[ref_param])
	fn_id = FunctionId(module="main", name="foo", ordinal=0)
	assign_node_ids(block)
	call_expr = block.statements[2].expr
	call_resolutions = {}
	if isinstance(call_expr, H.HCall):
		call_resolutions[call_expr.node_id] = CallableDecl(
			callable_id=1,
			name="foo",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(ref_param,), result_type=table.ensure_unknown()),
			fn_id=fn_id,
		)
	diags = _bc_with_sig(table, ref_sig, fn_id=fn_id, call_resolutions=call_resolutions).check_block(block)
	assert diags == []


def test_hmethod_signature_driven_auto_borrow_prevents_move():
	table = TypeTable()
	recv_ref = table.ensure_ref(table.ensure_unknown())
	ref_sig = FnSignature(name="m", param_type_ids=[recv_ref])
	fn_id = FunctionId(module="main", name="m", ordinal=0)
	call_expr = H.HMethodCall(receiver=H.HVar("x", binding_id=1), method_name="m", args=[])
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralString("s"), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call_expr),
			H.HExprStmt(expr=H.HVar("x", binding_id=1)),
		]
	)
	decl = CallableDecl(
		callable_id=1,
		name="m",
		kind=CallableKind.METHOD_INHERENT,
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(recv_ref,), result_type=table.ensure_unknown()),
		impl_id=1,
		impl_target_type_id=table.ensure_unknown(),
		self_mode=SelfMode.SELF_BY_REF,
		fn_id=fn_id,
	)
	assign_node_ids(block)
	call_resolutions = {call_expr.node_id: MethodResolution(decl=decl, receiver_autoborrow=SelfMode.SELF_BY_REF)}
	diags = _bc_with_sig(table, ref_sig, fn_id=fn_id, call_resolutions=call_resolutions).check_block(block)
	assert diags == []


def test_method_value_receiver_moves_and_later_use_errors():
	table = TypeTable()
	int_ty = table.ensure_int()
	call_expr = H.HMethodCall(
		receiver=H.HMove(H.HVar("x", binding_id=1)),
		method_name="m",
		args=[],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call_expr),
			H.HExprStmt(expr=H.HVar("x", binding_id=1)),
		]
	)
	fn_id = FunctionId(module="main", name="m", ordinal=0)
	decl = CallableDecl(
		callable_id=10,
		name="m",
		kind=CallableKind.METHOD_INHERENT,
		module_id=0,
		visibility=Visibility.public(),
		signature=CallableSignature(param_types=(int_ty,), result_type=int_ty),
		impl_id=10,
		impl_target_type_id=int_ty,
		self_mode=SelfMode.SELF_BY_VALUE,
		fn_id=fn_id,
	)
	assign_node_ids(block)
	call_resolutions = {call_expr.node_id: MethodResolution(decl=decl, receiver_autoborrow=None)}
	ref_sig = FnSignature(name="m", param_type_ids=[int_ty])
	diags = _bc_with_sig(table, ref_sig, fn_id=fn_id, call_resolutions=call_resolutions).check_block(block)
	assert diags
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_hcall_kwarg_mut_autoborrow_temporary_allows_later_write():
	table = TypeTable()
	ref_mut = table.ensure_ref_mut(table.ensure_unknown())
	sig = FnSignature(name="foo", param_type_ids=[ref_mut], param_names=["x"])
	fn_id = FunctionId(module="main", name="foo", ordinal=0)
	call_expr = H.HCall(
		fn=H.HVar("foo", binding_id=2),
		args=[],
		kwargs=[H.HKwArg(name="x", value=H.HVar("x", binding_id=1))],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="foo", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=2),
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1, is_mutable=True),
			H.HExprStmt(expr=call_expr),
			H.HAssign(target=H.HVar("x", binding_id=1), value=H.HLiteralInt(2)),
		]
	)
	assign_node_ids(block)
	call_resolutions = {
		call_expr.node_id: CallableDecl(
			callable_id=1,
			name="foo",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(ref_mut,), result_type=table.ensure_unknown()),
			fn_id=fn_id,
		)
	}
	diags = _bc_with_sig(
		table,
		sig,
		fn_id=fn_id,
		call_resolutions=call_resolutions,
		binding_mutable={1: True},
	).check_block(block)
	assert diags == []


def test_hcall_kwarg_mut_autoborrow_rejects_immutable_binding():
	table = TypeTable()
	ref_mut = table.ensure_ref_mut(table.ensure_unknown())
	sig = FnSignature(name="foo", param_type_ids=[ref_mut], param_names=["x"])
	fn_id = FunctionId(module="main", name="foo", ordinal=0)
	call_expr = H.HCall(
		fn=H.HVar("foo", binding_id=2),
		args=[],
		kwargs=[H.HKwArg(name="x", value=H.HVar("x", binding_id=1))],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="foo", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=2),
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1, is_mutable=False),
			H.HExprStmt(expr=call_expr),
		]
	)
	assign_node_ids(block)
	call_resolutions = {
		call_expr.node_id: CallableDecl(
			callable_id=1,
			name="foo",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(ref_mut,), result_type=table.ensure_unknown()),
			fn_id=fn_id,
		)
	}
	diags = _bc_with_sig(
		table,
		sig,
		fn_id=fn_id,
		call_resolutions=call_resolutions,
		binding_mutable={1: False},
	).check_block(block)
	assert any("cannot take mutable borrow of immutable binding" in d.message for d in diags)
