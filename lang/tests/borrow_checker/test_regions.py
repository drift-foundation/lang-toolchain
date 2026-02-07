#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""Borrow lifetime tests.

Current policy: explicit borrow bindings are shortened to last use within
their lexical scope (NLL-lite). This keeps borrows live before first use and
across joins when the ref is still used, but releases them after the last use.
"""

from lang.driftc import stage1 as H
from lang.driftc.borrow_checker_pass import BorrowChecker
from lang.driftc.borrow_checker import PlaceBase, PlaceKind
from lang.driftc.core.types_core import TypeTable


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


def test_borrow_ends_after_last_use_allows_mut():
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
			# NLL-lite: the borrow ends after the last use of r, so &mut is allowed.
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


def test_borrow_used_in_one_branch_allows_mut_after_join():
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
			# NLL-lite: r is only used in the then branch, so &mut after the join is ok.
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


def test_borrow_used_in_loop_allows_mut_after_exit():
	# NLL-lite: the borrow ends after the last use inside the loop, so &mut after the loop is ok.
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
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_borrow_use_in_assign_target_keeps_live():
	# The borrow is used in a later assignment target (index expression). That
	# use should keep the loan live and block an intervening &mut borrow.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(1), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HExprStmt(expr=H.HVar("r", binding_id=2)),
			# This should be rejected because r is still used later in the target.
			H.HExprStmt(expr=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True)),
			H.HLet(
				name="arr",
				value=H.HArrayLiteral(elements=[H.HLiteralInt(0)]),
				declared_type_expr=None,
				binding_id=3,
			),
			H.HAssign(
				target=H.HPlaceExpr(
					base=H.HVar("arr", binding_id=3),
					projections=[H.HPlaceIndex(index=H.HVar("r", binding_id=2))],
				),
				value=H.HLiteralInt(1),
			),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefInt"), "arr": (3, "Unknown")}).check_block(block)
	assert any("borrow" in d.message for d in diags)


def test_borrow_last_use_same_block_allows_write():
	# NLL-lite with statement-level liveness drops the loan after the last use.
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HExprStmt(expr=H.HVar("r", binding_id=2)),
			H.HAssign(target=H.HVar("x", binding_id=1), value=H.HLiteralInt(1)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefMutInt")}).check_block(block)
	assert diags == []


def test_unused_borrow_same_block_still_blocks_write():
	# Unused borrows stay live until the end of their lexical scope (conservative).
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="p",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HAssign(target=H.HVar("x", binding_id=1), value=H.HLiteralInt(1)),
		]
	)
	diags = _bc({"x": (1, "Int"), "p": (2, "RefInt")}).check_block(block)
	assert any("cannot write" in d.message for d in diags)


def test_unused_borrow_in_inner_scope_allows_after_scope():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=1),
			H.HIf(
				cond=H.HLiteralBool(True),
				then_block=H.HBlock(
					statements=[
						H.HLet(
							name="p",
							value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=False),
							declared_type_expr=None,
							binding_id=2,
						)
					]
				),
				else_block=H.HBlock(statements=[]),
			),
			H.HAssign(target=H.HVar("x", binding_id=1), value=H.HLiteralInt(1)),
		]
	)
	diags = _bc({"x": (1, "Int"), "p": (2, "RefInt")}).check_block(block)
	assert diags == []


def test_ref_copy_keeps_borrow_live_until_last_use():
	block = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HLiteralInt(0), declared_type_expr=None, binding_id=1),
			H.HLet(
				name="r",
				value=H.HBorrow(subject=H.HVar("x", binding_id=1), is_mut=True),
				declared_type_expr=None,
				binding_id=2,
			),
			H.HLet(
				name="s",
				value=H.HVar("r", binding_id=2),
				declared_type_expr=None,
				binding_id=3,
			),
			H.HExprStmt(expr=H.HVar("s", binding_id=3)),
			H.HAssign(target=H.HVar("x", binding_id=1), value=H.HLiteralInt(1)),
			H.HExprStmt(expr=H.HVar("s", binding_id=3)),
		]
	)
	diags = _bc({"x": (1, "Int"), "r": (2, "RefMutInt"), "s": (3, "RefMutInt")}).check_block(block)
	assert any("cannot write" in d.message for d in diags)


def test_multiple_refs_allow_mut_after_last_uses():
	# Two refs to the same target; NLL-lite releases after the last use of both refs.
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


def test_borrow_declared_in_branch_scope_does_not_block_after_branch():
	# Borrow declared inside the branch block is out of scope after the branch,
	# so the later &mut is allowed (lexical scoping).
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
	assert diags == []


def test_nested_branches_borrow_dies_when_last_use_done():
	# Nested branch; borrow declared inside the inner then block is out of scope after
	# leaving that block (lexical lifetime), so &mut is allowed after the outer if.
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


def test_borrow_used_in_catch_allows_mut_after_catch():
	# NLL-lite: borrow ends after catch use, so &mut after try/catch is allowed.
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
						H.HThrow(value=H.HExceptionInit(event_fqn="m:Evt", pos_args=[], kw_args=[])),
					]
				),
				catches=[
					H.HCatchArm(
						event_fqn=None,
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
