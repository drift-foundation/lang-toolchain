#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Temporary borrow/NLL-approximation tests."""

from lang2 import stage1 as H
from lang2.borrow_checker_pass import BorrowChecker
from lang2.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


def _checker() -> BorrowChecker:
	table = TypeTable()
	type_ids = {"x": table.ensure_int()}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", 0) or 0, hv.name if hasattr(hv, "name") else str(hv))
	fn_types = {PlaceBase(PlaceKind.LOCAL, 0, name): ty for name, ty in type_ids.items()}
	return BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)


def test_temp_borrow_in_expr_stmt_does_not_block_later_mut_borrow():
	"""
	A borrow used only in an expression statement should end immediately,
	allowing a later borrow on the same place.
	"""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=False)),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=True)),
		]
	)
	diags = _checker().check_block(block)
	assert diags == []


def test_condition_borrow_does_not_block_later_mut_borrow():
	"""Borrow in an if condition is temporary and should not block later mut borrow."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HIf(
				cond=H.HBorrow(subject=H.HVar("x"), is_mut=False),
				then_block=H.HBlock(statements=[]),
				else_block=H.HBlock(statements=[]),
			),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=True)),
		]
	)
	diags = _checker().check_block(block)
	assert diags == []


def test_auto_borrow_at_call_is_call_scoped_when_enabled():
	"""
	With auto-borrow enabled, call-site borrows should be temporary (released after call).
	We approximate this by ensuring a later mutable borrow is allowed.
	"""
	table = TypeTable()
	type_ids = {"x": table.ensure_int(), "f": table.ensure_unknown()}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", 0) or 0, hv.name if hasattr(hv, "name") else str(hv))
	fn_types = {PlaceBase(PlaceKind.LOCAL, getattr(None, "binding_id", 0) or 0, name): ty for name, ty in type_ids.items()}
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup, enable_auto_borrow=True)

	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(expr=H.HCall(fn=H.HVar("f"), args=[H.HVar("x")])),  # auto-borrow x as temp
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=True)),   # should be allowed after call
		]
	)
	diags = bc.check_block(block)
	assert diags == []


def test_auto_borrow_method_receiver_still_evaluated():
	"""
	When auto-borrow is enabled, receivers that are not lvalues (e.g., call expressions)
	must still be evaluated; auto-borrow is layered on lvalues only.
	"""
	table = TypeTable()
	type_ids = {"x": table.ensure_int(), "make_obj": table.ensure_unknown()}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, getattr(hv, "binding_id", 0) or 0, hv.name if hasattr(hv, "name") else str(hv))
	fn_types = {PlaceBase(PlaceKind.LOCAL, getattr(None, "binding_id", 0) or 0, name): ty for name, ty in type_ids.items()}
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup, enable_auto_borrow=True)

	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(
				expr=H.HMethodCall(
					receiver=H.HCall(fn=H.HVar("make_obj"), args=[H.HVar("x")]),
					method_name="m",
					args=[],
				)
			),
		]
	)
	diags = bc.check_block(block)
	assert diags == []
