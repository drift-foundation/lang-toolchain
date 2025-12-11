#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Borrow-related tests for the borrow checker (Phase 2 loans)."""

from lang2 import stage1 as H
from lang2.driftc.borrow_checker_pass import BorrowChecker
from lang2.driftc.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


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


def test_borrow_from_rvalue_is_error():
	"""Borrowing from a non-lvalue (literal) should emit a diagnostic."""
	block = H.HBlock(statements=[H.HExprStmt(expr=H.HBorrow(subject=H.HLiteralInt(1), is_mut=False))])
	diags = _checker_with_types({}).check_block(block)
	assert any("non-lvalue" in d.message for d in diags)


def test_borrow_from_moved_value_is_error():
	"""Borrowing after a move should be rejected."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(expr=H.HVar("x")),  # move
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=False)),  # borrow moved
		]
	)
	diags = _checker_with_types({"x": "Unknown"}).check_block(block)
	assert any("moved or uninitialized" in d.message for d in diags)


def test_multiple_shared_borrows_allowed():
	"""Shared borrows can coexist without diagnostics."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="r1", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),
			H.HLet(name="r2", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),
		]
	)
	diags = _checker_with_types({"x": "Unknown"}).check_block(block)
	assert diags == []


def test_mut_borrow_conflicts_with_existing_shared():
	"""Taking a mutable borrow when a shared borrow is active should error."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="r", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),  # shared
			H.HLet(name="m", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),   # mut conflict
		]
	)
	diags = _checker_with_types({"x": "Unknown", "r": "Unknown", "m": "Unknown"}).check_block(block)
	assert any("mutable borrow" in d.message for d in diags)


def test_mut_borrow_conflicts_with_existing_mut():
	"""Taking a second mutable borrow should error."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="m1", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),   # first mut
			H.HLet(name="m2", value=H.HBorrow(subject=H.HVar("x"), is_mut=True), declared_type_expr=None),   # second mut
		]
	)
	diags = _checker_with_types({"x": "Unknown", "m1": "Unknown", "m2": "Unknown"}).check_block(block)
	assert any("mutable borrow" in d.message for d in diags)


def test_move_while_borrowed_reports_diagnostic():
	"""Moving a non-Copy while any loan is active should error."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="r", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),  # shared loan
			H.HExprStmt(expr=H.HVar("x")),  # move under loan
		]
	)
	diags = _checker_with_types({"x": "Unknown", "r": "Unknown"}).check_block(block)
	assert any("while borrowed" in d.message for d in diags)


def test_assignment_kills_overlapping_loans():
	"""Assigning to a place should drop existing loans on that place."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=False)),  # shared
			H.HAssign(target=H.HVar("x"), value=H.HLiteralInt(2)),  # overwrite -> invalidate loans
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x"), is_mut=True)),   # now allowed
		]
	)
	diags = _checker_with_types({"x": "Unknown"}).check_block(block)
	assert diags == []
