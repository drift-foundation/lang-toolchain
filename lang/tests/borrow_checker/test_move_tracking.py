#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Straight-line move-tracking tests for the borrow checker."""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceBase, PlaceKind
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.method_registry import CallableDecl, CallableKind, CallableSignature, Visibility
from lang.driftc.stage1.node_ids import assign_node_ids


def _checker_with_types(var_types: dict[str, str]) -> BorrowChecker:
	"""Construct a BorrowChecker wired with the provided variable type names."""
	table = TypeTable()
	type_ids = {}
	for name, kind in var_types.items():
		if kind == "Int":
			type_ids[name] = table.ensure_int()
		elif kind == "Bool":
			type_ids[name] = table.ensure_bool()
		else:
			type_ids[name] = table.ensure_unknown()
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	fn_types = {PlaceBase(PlaceKind.LOCAL, 0, name): ty for name, ty in type_ids.items()}
	return BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)


def test_use_after_move_reports_diagnostic():
	"""Moving a non-Copy local then using it again should emit a diagnostic."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(expr=H.HMove(H.HVar("x"))),  # move (non-Copy)
			H.HExprStmt(expr=H.HVar("x")),  # use after move
		]
	)
	bc = _checker_with_types({"x": "Unknown"})
	diags = bc.check_block(block)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_use_after_move_detected_after_assignment():
	"""Assignment revalidates a place; later move triggers a use-after-move diagnostic."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HAssign(target=H.HVar("x"), value=H.HLiteralInt(2)),  # overwrite -> valid
			H.HExprStmt(expr=H.HMove(H.HVar("x"))),
			H.HExprStmt(expr=H.HVar("x")),  # use after move
		]
	)
	bc = _checker_with_types({"x": "Unknown"})
	diags = bc.check_block(block)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_copy_type_is_not_moved():
	"""Copy types remain usable after reads."""
	block = H.HBlock(
		statements=[
			H.HLet(name="b", value=H.HLiteralBool(True), declared_type_expr=None),
			H.HExprStmt(expr=H.HVar("b")),  # Bool is Copy => no move
			H.HExprStmt(expr=H.HVar("b")),
		]
	)
	bc = _checker_with_types({"b": "Bool"})
	diags = bc.check_block(block)
	assert diags == []


def test_array_is_not_copy_in_type_table():
	table = TypeTable()
	int_ty = table.ensure_int()
	arr_ty = table.new_array(int_ty)
	assert table.is_copy(arr_ty) is False


def test_array_value_moves_in_borrow_checker():
	table = TypeTable()
	int_ty = table.ensure_int()
	arr_ty = table.new_array(int_ty)
	fn_types = {PlaceBase(PlaceKind.LOCAL, 0, "a"): arr_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)
	block = H.HBlock(
		statements=[
			H.HLet(name="a", value=H.HArrayLiteral(elements=[]), declared_type_expr=None),
			H.HExprStmt(expr=H.HMove(H.HVar("a"))),  # move Array
			H.HExprStmt(expr=H.HVar("a")),  # use after move
		]
	)
	diags = bc.check_block(block)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_noncopy_subplace_move_via_by_value_call():
	table = TypeTable()
	unknown_ty = table.ensure_unknown()
	struct_ty = table.declare_struct(module_id="main", name="Box", field_names=["f"])
	table.define_struct_fields(struct_ty, [unknown_ty])
	s_base = PlaceBase(PlaceKind.LOCAL, 1, "s")
	fn_types = {s_base: struct_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", -1), hv.name)
	call = H.HCall(
		fn=H.HVar("foo"),
		args=[H.HField(subject=H.HVar("s", binding_id=1), name="f")],
		kwargs=[],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="s", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call),
			H.HExprStmt(expr=H.HField(subject=H.HVar("s", binding_id=1), name="f")),
		]
	)
	assign_node_ids(block)
	foo_id = FunctionId(module="main", name="foo", ordinal=0)
	call_resolutions = {
		call.node_id: CallableDecl(
			callable_id=1,
			name="foo",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(unknown_ty,), result_type=table.ensure_unknown()),
			fn_id=foo_id,
		)
	}
	bc = BorrowChecker(
		type_table=table,
		fn_types=fn_types,
		base_lookup=base_lookup,
		enable_auto_borrow=True,
		call_resolutions=call_resolutions,
	)
	diags = bc.check_block(block)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_noncopy_index_move_via_by_value_call():
	table = TypeTable()
	int_ty = table.ensure_int()
	unknown_ty = table.ensure_unknown()
	arr_ty = table.new_array(unknown_ty)
	a_base = PlaceBase(PlaceKind.LOCAL, 1, "a")
	fn_types = {a_base: arr_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", -1), hv.name)
	idx_expr = H.HLiteralInt(0)
	call = H.HCall(
		fn=H.HVar("foo"),
		args=[H.HIndex(subject=H.HVar("a", binding_id=1), index=idx_expr)],
		kwargs=[],
	)
	block = H.HBlock(
		statements=[
			H.HLet(name="a", value=H.HArrayLiteral(elements=[]), declared_type_expr=None, binding_id=1),
			H.HExprStmt(expr=call),
			H.HExprStmt(expr=H.HIndex(subject=H.HVar("a", binding_id=1), index=H.HLiteralInt(0))),
		]
	)
	assign_node_ids(block)
	foo_id = FunctionId(module="main", name="foo", ordinal=0)
	call_resolutions = {
		call.node_id: CallableDecl(
			callable_id=1,
			name="foo",
			kind=CallableKind.FREE_FUNCTION,
			module_id=0,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=(unknown_ty,), result_type=int_ty),
			fn_id=foo_id,
		)
	}
	bc = BorrowChecker(
		type_table=table,
		fn_types=fn_types,
		base_lookup=base_lookup,
		enable_auto_borrow=True,
		call_resolutions=call_resolutions,
	)
	diags = bc.check_block(block)
	assert any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_consuming_binary_does_not_move_operands():
	table = TypeTable()
	unknown_ty = table.ensure_unknown()
	a_base = PlaceBase(PlaceKind.LOCAL, 1, "a")
	b_base = PlaceBase(PlaceKind.LOCAL, 2, "b")
	fn_types = {a_base: unknown_ty, b_base: unknown_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", -1), hv.name)
	block = H.HBlock(
		statements=[
			H.HLet(name="a", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(name="b", value=H.HLiteralInt(2), declared_type_expr=None, binding_id=2),
			H.HLet(
				name="y",
				value=H.HBinary(op=H.BinaryOp.ADD, left=H.HVar("a", binding_id=1), right=H.HVar("b", binding_id=2)),
				declared_type_expr=None,
				binding_id=3,
			),
			H.HExprStmt(expr=H.HVar("a", binding_id=1)),
			H.HExprStmt(expr=H.HVar("b", binding_id=2)),
		]
	)
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)
	diags = bc.check_block(block)
	assert not any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_consuming_unary_does_not_move_operand():
	table = TypeTable()
	unknown_ty = table.ensure_unknown()
	a_base = PlaceBase(PlaceKind.LOCAL, 1, "a")
	fn_types = {a_base: unknown_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", -1), hv.name)
	block = H.HBlock(
		statements=[
			H.HLet(name="a", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="y",
				value=H.HUnary(op=H.UnaryOp.NEG, expr=H.HVar("a", binding_id=1)),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HExprStmt(expr=H.HVar("a", binding_id=1)),
		]
	)
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)
	diags = bc.check_block(block)
	assert not any(d.code == "E_USE_AFTER_MOVE" for d in diags)


def test_consuming_ternary_does_not_move_operands():
	table = TypeTable()
	unknown_ty = table.ensure_unknown()
	a_base = PlaceBase(PlaceKind.LOCAL, 1, "a")
	b_base = PlaceBase(PlaceKind.LOCAL, 2, "b")
	fn_types = {a_base: unknown_ty, b_base: unknown_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", -1), hv.name)
	block = H.HBlock(
		statements=[
			H.HLet(name="a", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(name="b", value=H.HLiteralInt(2), declared_type_expr=None, binding_id=2),
			H.HLet(
				name="y",
				value=H.HTernary(
					cond=H.HLiteralBool(True),
					then_expr=H.HVar("a", binding_id=1),
					else_expr=H.HVar("b", binding_id=2),
				),
				declared_type_expr=None,
				binding_id=3,
			),
			H.HExprStmt(expr=H.HVar("a", binding_id=1)),
			H.HExprStmt(expr=H.HVar("b", binding_id=2)),
		]
	)
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)
	diags = bc.check_block(block)
	assert not any(d.code == "E_USE_AFTER_MOVE" for d in diags)
