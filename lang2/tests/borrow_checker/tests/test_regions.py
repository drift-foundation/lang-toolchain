#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Region-aware borrow lifetime tests."""

from lang2 import stage1 as H
from lang2.driftc.borrow_checker_pass import BorrowChecker
from lang2.driftc.borrow_checker import PlaceBase, PlaceKind
from lang2.core.types_core import TypeTable


def _bc(bindings: dict[str, tuple[int, str]]) -> BorrowChecker:
	table = TypeTable()
	type_ids = {}
	fn_types = {}
	int_ty = table.ensure_int()
	for name, (bid, kind) in bindings.items():
		if kind in ("Int", "int"):
			type_ids[name] = int_ty
		elif kind in ("RefInt", "&Int"):
			type_ids[name] = table.ensure_ref(int_ty)
		elif kind in ("RefMutInt", "&mut Int"):
			type_ids[name] = table.ensure_ref_mut(int_ty)
		elif kind in ("Bool", "bool"):
			type_ids[name] = table.ensure_bool()
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
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
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
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert any("borrow" in d.message for d in diags)


def test_borrow_in_one_branch_allows_mut_after_join_when_no_later_use():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("r", binding_id=2))]),
				else_block=H.HBlock(statements=[]),
			),
			# Region for r should end before this block because r is never used after the if.
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_borrow_used_after_join_keeps_mut_blocked_until_then():
	# Even if branches themselves do not use the ref, a later use should keep the loan live
	# through the join block and block &mut borrows placed before that use.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(statements=[]),
				else_block=H.HBlock(statements=[]),
			),
			# This mut borrow happens in the continuation block before the final use of r.
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
			H.HExprStmt(expr=H.HVar("r", binding_id=2)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert any("borrow" in d.message for d in diags)


def test_borrow_live_in_loop_allows_mut_after_loop_exit():
	# Borrow is used inside the loop; loan should not extend to the continuation after the loop.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HLoop(body=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("r", binding_id=2))])),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),  # should be allowed after loop
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_multiple_refs_union_region_blocks_mut_until_all_dead():
	# Two refs to the same target; loan should stay live until both are dead.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r1",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HExprStmt(expr=H.HVar("r1", binding_id=2)),
			H.HLet(
				name="r2",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=3,
			),
			H.HExprStmt(expr=H.HVar("r2", binding_id=3)),
			H.HIf(cond=H.HLiteralBool(True), then_block=H.HBlock(statements=[]), else_block=H.HBlock(statements=[])),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),  # only after both uses
		]
	)
	diags = _bc({"x": (1, "Int"), "r1": (2, "RefInt"), "r2": (3, "RefInt")}).check_block(block)
	assert diags == []


def test_borrow_only_in_one_loop_path_keeps_mut_blocked_inside_loop():
	# Even if the borrow is only taken in one branch inside the loop, the loop backedge keeps it live.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLoop(
				body=H.HBlock(
					statements=[
						H.HIf(
							cond=H.HLiteralBool(True),
							then_block=H.HBlock(
								statements=[
									H.HLet(
										name="r",
										value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
										declared_type_expr=None,
										binding_id=2,
									),
									H.HExprStmt(expr=H.HVar("r", binding_id=2)),
								]
							),
							else_block=H.HBlock(statements=[]),
						),
						H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
					]
				)
			),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert any("borrow active" in d.message for d in diags)


def test_nested_branches_borrow_dies_when_last_use_done():
	# Nested branch; borrow used only in inner then. After outer, mut borrow should be allowed.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(
					statements=[
						H.HIf(
							cond=H.HLiteralBool(True),
							then_block=H.HBlock(
								statements=[
									H.HLet(
										name="r",
										value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
										declared_type_expr=None,
										binding_id=2,
									),
									H.HExprStmt(expr=H.HVar("r", binding_id=2)),
								]
							),
							else_block=H.HBlock(statements=[]),
						)
					]
				),
				else_block=H.HBlock(statements=[]),
			),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_try_body_borrow_allows_mut_after_try():
	# Borrow in try body, used there, should end before code after try.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HLet(
							name="r",
							value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
							declared_type_expr=None,
							binding_id=2,
						),
						H.HExprStmt(expr=H.HVar("r", binding_id=2)),
					]
				),
				catches=[],
			),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_borrow_used_in_catch_blocks_mut_until_catch_done():
	# Borrow created before try and used in catch should keep loan live through catch.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HTry(
				body=H.HBlock(
					statements=[
						H.HThrow(value=H.HLiteralString("err")),
					]
				),
				catches=[
					H.HCatchArm(
						event_name=None,
						binder=None,
						block=H.HBlock(statements=[H.HExprStmt(expr=H.HVar("r", binding_id=2))]),
					)
				],
			),
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),  # should wait until catch done
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []
