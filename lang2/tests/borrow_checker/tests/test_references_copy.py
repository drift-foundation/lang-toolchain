#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Ensure reference types are treated as Copy by the borrow checker."""

from lang2 import stage1 as H
from lang2.driftc.borrow_checker_pass import BorrowChecker
from lang2.driftc.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


def test_shared_reference_is_copy():
	"""Borrowed refs (&T) should not be moved when used multiple times."""
	table = TypeTable()
	int_ty = table.ensure_int()
	ref_int = table.ensure_ref(int_ty)
	type_ids = {"r": ref_int, "make_ref": table.ensure_unknown(), "x": int_ty}
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	fn_types = {PlaceBase(PlaceKind.LOCAL, 0, name): ty for name, ty in type_ids.items()}
	bc = BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)

	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLet(name="r", value=H.HBorrow(subject=H.HVar("x"), is_mut=False), declared_type_expr=None),
			H.HExprStmt(expr=H.HVar("r")),
			H.HExprStmt(expr=H.HVar("r")),
		]
	)
	diags = bc.check_block(block)
	assert diags == []
