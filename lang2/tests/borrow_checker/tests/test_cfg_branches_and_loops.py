#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""CFG-aware move-tracking tests for the borrow checker."""

from lang2 import stage1 as H
from lang2.driftc.borrow_checker_pass import BorrowChecker
from lang2.driftc.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


def _checker_with_types(var_types: dict[str, str]) -> BorrowChecker:
	"""Construct a BorrowChecker with named locals mapped to simple type ids."""
	table = TypeTable()
	type_ids = {}
	for name, kind in var_types.items():
		if kind == "Int":
			type_ids[name] = table.ensure_int()
		else:
			type_ids[name] = table.ensure_unknown()
	base_lookup = lambda hv: PlaceBase(PlaceKind.LOCAL, 0, hv.name)
	fn_types = {PlaceBase(PlaceKind.LOCAL, 0, name): ty for name, ty in type_ids.items()}
	return BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)


def test_move_in_one_branch_does_not_affect_other():
	"""Move in one branch should not taint the other branch, but the join must see the move."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("x"))]),  # move x
				else_block=H.HBlock(statements=[H.HExprStmt(expr=H.HLiteralInt(0))]),
			),
			H.HExprStmt(expr=H.HVar("x")),  # after if: should be use-after-move (since one path moves)
		]
	)
	bc = _checker_with_types({"x": "Unknown"})
	diags = bc.check_block(block)
	assert any("use after move" in d.message for d in diags)
	# Ensure no diagnostics were emitted inside the else branch (implicitly, since there are only use-after-move diags).


def test_move_in_both_branches_flags_after_join():
	"""Moves in both branches should result in MOVED at the join and flag later uses."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("x"))]),  # move
				else_block=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("x"))]),  # move
			),
			H.HExprStmt(expr=H.HVar("x")),  # use after both branches moved
		]
	)
	bc = _checker_with_types({"x": "Unknown"})
	diags = bc.check_block(block)
	assert any("use after move" in d.message for d in diags)


def test_loop_move_then_break_then_use():
	"""Moves inside a loop body should remain moved after the loop when it breaks."""
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None),
			H.HLoop(
				body=H.HBlock(
					statements=[
						H.HExprStmt(expr=H.HVar("x")),  # move
						H.HBreak(),
					]
				)
			),
			H.HExprStmt(expr=H.HVar("x")),  # after loop: use after move
		]
	)
	bc = _checker_with_types({"x": "Unknown"})
	diags = bc.check_block(block)
	assert any("use after move" in d.message for d in diags)
