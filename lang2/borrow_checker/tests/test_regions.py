#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Region-aware borrow lifetime tests."""

from lang2 import stage1 as H
from lang2.borrow_checker_pass import BorrowChecker
from lang2.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


def _bc(bindings: dict[str, tuple[int, str]]) -> BorrowChecker:
	table = TypeTable()
	type_ids = {}
	fn_types = {}
	for name, (bid, kind) in bindings.items():
		if kind == "Int":
			type_ids[name] = table.ensure_int()
		else:
			type_ids[name] = table.ensure_unknown()
		fn_types[PlaceBase(PlaceKind.LOCAL, bid, name)] = type_ids[name]
	base_lookup = lambda hv: PlaceBase(
		PlaceKind.LOCAL,
		getattr(hv, "binding_id", -1) if getattr(hv, "binding_id", None) is not None else -1,
		hv.name if hasattr(hv, "name") else str(hv),
	)
	return BorrowChecker(type_table=table, fn_types=fn_types, base_lookup=base_lookup)


def test_borrow_ends_after_last_use_allows_later_mut():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),  # ref binding
			H.HExprStmt(expr=H.HVar("r", binding_id=2)),  # use ref
			# end of region for r should be here; place mut borrow in a later block
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(
					statements=[H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True))]
				),
				else_block=H.HBlock(statements=[]),
			),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "Int")}).check_block(block)
	assert diags == []


def test_borrow_still_live_before_first_use_blocks_mut():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),  # ref binding
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),  # should conflict with r
			H.HExprStmt(expr=H.HVar("r", binding_id=2)),  # use ref
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "Int")}).check_block(block)
	assert any("borrow" in d.message for d in diags)
