# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3C — unit tests for `insert_drop_flags`.

Pins the runtime-drop-flag insertion contract documented in
`work/ownership-ledger/3c-design.md`:

- For each path-dependent destructible local, a Bool flag is
  allocated, initialised at entry (true for params, false for
  declared locals), set on every StoreLocal, cleared on every
  MoveOut, and a flag-guarded drop block is inserted at every
  Return / Unreachable terminator.
- Cleanup runs at the original source scope-exit point (no early
  drops; RAII timing preserved).
- Functions with no path-dependent destructible locals are
  unchanged (no-op).

These tests use hand-built MirFunc fixtures — they exercise the
pass directly without going through the full HIR→MIR pipeline.
The acceptance-marker tests in
`test_hir_to_mir_path_insensitive_moved_locals.py` cover the
end-to-end path through HIR lowering.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.drop_flags import insert_drop_flags
from lang.driftc.stage2.drop_policy_compute import compute_drop_policy


def _drop_policy_for(type_table: TypeTable):
	def fn(ty):
		return compute_drop_policy(type_table, ty)
	return fn


def _make_droppable_struct(type_table: TypeTable) -> int:
	"""Build an Arc-shaped destructible struct (has destructor_fns
	entry → has_drop=True)."""
	int_ty = type_table.ensure_int()
	arc_tid = type_table.declare_struct(module_id="test", name="DropMe", field_names=["inner"])
	type_table.define_struct_fields(arc_tid, field_types=[int_ty])
	destroy_fn = FunctionId(module="test", name="DropMe::destroy", ordinal=0)
	type_table.destructor_fns = {arc_tid: destroy_fn}
	# Mark non-Copy so DropPolicy.needs_drop = True.
	non_copy: set[int] = {arc_tid}
	type_table._copy_query = lambda tid: False if tid in non_copy else None  # type: ignore[attr-defined]
	return arc_tid


def _empty_fn(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _count_blocks_named(func: M.MirFunc, prefix: str) -> int:
	return sum(1 for n in func.blocks if n.startswith(prefix))


def _block_terminators(func: M.MirFunc) -> dict[str, M.MTerminator]:
	return {n: b.terminator for n, b in func.blocks.items() if b.terminator is not None}


# -- no-op invariant ---------------------------------------------------------


def test_function_with_no_path_dependent_locals_is_no_op() -> None:
	"""A function with a single local that is unconditionally live
	(no MoveOut, no MaybeUninit anywhere) should not gain a flag."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _empty_fn("simple", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t0"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	pre_locals = list(func.locals)
	pre_block_count = len(func.blocks)
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	assert func.locals == pre_locals, "no flag should be allocated when no local is path-dependent"
	assert len(func.blocks) == pre_block_count, "no new blocks should be added"


def test_function_with_unconditional_move_no_flag_needed() -> None:
	"""A local that is unconditionally moved (the move is on every
	exit path, no exit can still own the value) does NOT get a flag —
	the existing scope-drop emission is already correct, and adding
	flag plumbing would create dead drop blocks at exits where the
	ledger proves the local is `MovedOut`.  This is the (b) filter:
	criterion #2 (potentially-live-at-some-exit) excludes the
	unconditional-move case."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	func = _empty_fn("uncond", params=[], locals_=["x"], types={"x": drop_ty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t0"))
	# This MoveOut is a USER move (its dest `t1` is consumed by the
	# Return below, NOT by an immediately-following DropValue), so
	# criterion #1 (user-moveout) holds.
	entry.instructions.append(M.MoveOut(dest="t1", local="x", ty=drop_ty))
	entry.terminator = M.Return(value="t1")
	func.blocks["entry"] = entry
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	flag_locals = [name for name in func.locals if name.startswith("__drop_flag_")]
	assert flag_locals == [], (
		"unconditional-move case must NOT get a flag: at the only Return, "
		"the ledger proves x is MovedOut (criterion #2 excludes), so the "
		"existing scope-drop emission is correct and flag plumbing would "
		"be dead code"
	)
	# No new drop blocks added.
	drop_blocks = [n for n in func.blocks if n.startswith("entry_drop_x")]
	assert drop_blocks == [], f"unexpected drop-block(s) for x: {drop_blocks}"


# -- terminating-arm leak shape (acceptance shape #1) -----------------------


def _build_terminating_arm_fixture() -> tuple[M.MirFunc, TypeTable, int]:
	"""var x; if b { return move x; } return;
	On b=false: x is live at the trailing return → flag should drop.
	On b=true: the inner return fires; the trailing return is unreachable
	from that path."""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	drop_ty = _make_droppable_struct(type_table)
	func = _empty_fn(
		"f_terminating",
		params=["b"],
		locals_=["b", "x"],
		types={"b": bool_ty, "x": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t0"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="x", ty=drop_ty))
	then_block.terminator = M.Return(value="t_move")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	return func, type_table, drop_ty


def test_terminating_arm_leak_shape_inserts_flag_guarded_drop_at_trailing_return() -> None:
	func, type_table, _ = _build_terminating_arm_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	# Flag local allocated.
	assert "__drop_flag_x" in func.locals
	# Both Return-terminating blocks (then with `return move x`, join
	# with `return`) get a flag-guarded drop sequence — uniform per the
	# design.  We look for at least one new drop-block per Return.
	drop_block_names = [n for n in func.blocks if n.startswith("if_join_drop_x") or n.startswith("if_then_drop_x")]
	assert any(n.startswith("if_join_drop_x") for n in drop_block_names), (
		"flag-guarded drop block missing at trailing if_join Return — "
		"this is the bucket-6 leak shape; without the new drop block, "
		"x leaks on the b=false runtime path"
	)


def test_terminating_arm_leak_shape_flag_clears_on_move() -> None:
	func, type_table, _ = _build_terminating_arm_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	# The MoveOut(x) in if_then must be followed by a flag clear
	# (StoreLocal __drop_flag_x = false).
	then_block = func.blocks["if_then"]
	saw_move = False
	saw_clear_after_move = False
	const_false_dests: set[str] = set()
	for ins in then_block.instructions:
		if isinstance(ins, M.MoveOut) and ins.local == "x":
			saw_move = True
			continue
		if not saw_move:
			continue
		if isinstance(ins, M.ConstBool) and ins.value is False:
			const_false_dests.add(ins.dest)
		elif isinstance(ins, M.StoreLocal) and ins.local == "__drop_flag_x" and ins.value in const_false_dests:
			saw_clear_after_move = True
	assert saw_clear_after_move, (
		"after MoveOut(x), expected StoreLocal(__drop_flag_x, ConstBool false) — "
		"so the trailing flag-guarded drop sees flag=false on the move path "
		"and skips the drop (no double-drop)"
	)


# -- non-terminating conditional move shape (acceptance shape #2) ----------


def _build_non_terminating_fixture() -> tuple[M.MirFunc, TypeTable, int]:
	"""var x; if b { val t = move x; } return;
	On b=false: x is live at trailing return → drop fires.
	On b=true: x was moved → flag cleared → drop skipped (no double-drop)."""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	drop_ty = _make_droppable_struct(type_table)
	func = _empty_fn(
		"f_non_terminating",
		params=["b"],
		locals_=["b", "x", "t"],
		types={"b": bool_ty, "x": drop_ty, "t": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="x", ty=drop_ty))
	then_block.instructions.append(M.StoreLocal(local="t", value="t_move"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	return func, type_table, drop_ty


def test_non_terminating_conditional_move_inserts_flag_guarded_drop_at_join() -> None:
	func, type_table, _ = _build_non_terminating_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	assert "__drop_flag_x" in func.locals
	# A new drop-block exists for x at the trailing if_join Return.
	drop_blocks_for_x = [n for n in func.blocks if n.startswith("if_join_drop_x")]
	assert drop_blocks_for_x, (
		"flag-guarded drop block missing at if_join Return — "
		"on the b=false path (x not moved), x would leak"
	)
	# The if_join block's terminator should now be an IfTerminator on
	# the flag, not the original Return.
	join_term = func.blocks["if_join"].terminator
	assert isinstance(join_term, M.IfTerminator), (
		"if_join terminator should be flag-IfTerminator after pass; got "
		f"{type(join_term).__name__}"
	)


# -- RAII invariant ---------------------------------------------------------


def test_raii_invariant_drops_at_original_scope_exit_point() -> None:
	"""The flag-guarded drop must be inserted at the same source scope-
	exit point (i.e. immediately preceding the original terminator),
	NOT pushed earlier into a predecessor arm.  This preserves RAII:
	for a Mutex<T> local conditionally moved, the unlock fires at the
	source scope-exit boundary regardless of which path was taken."""
	func, type_table, drop_ty = _build_non_terminating_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	# The drop block for x must be reachable ONLY from the flag-
	# guarded IfTerminator at if_join — NOT from the if_then arm
	# (which would mean the drop was pushed earlier into the predecessor).
	# Also verify the if_then's terminator is still its Goto into the
	# if_join (unchanged).
	then_term = func.blocks["if_then"].terminator
	assert isinstance(then_term, M.Goto), (
		"if_then terminator should remain Goto(if_join) after pass; "
		"if it changed (e.g. into a drop-block), the pass pushed cleanup "
		"earlier — RAII violation"
	)
	assert then_term.target == "if_join", (
		f"if_then's Goto target should be if_join; got {then_term.target} — "
		"a different target means cleanup was rerouted earlier"
	)


# -- param init ------------------------------------------------------------


def test_path_dependent_no_user_move_currently_unrepresentable() -> None:
	"""Pin: in current Drift, a path-dependent destructible local with
	NO user `move` cannot be expressed.

	Drift's grammar requires every `let`/`var` declaration to carry an
	initializer (`let_stmt: binder binding_name type_spec? alias_clause?
	EQUAL expr` in `lang/driftc/parser/grammar.lark`).  There is no
	syntax for `var x: T;` (uninitialized) declarations.  Without
	uninitialized declarations and without user moves, every
	destructible local is `Live` from the StoreLocal that initialises
	it through scope-exit.  The 3A ledger's `MaybeUninit` raw state
	can ONLY arise through user moves on conditional paths.

	This means the 3C pass's two equivalent criteria — design-level
	"`MaybeUninit + needs_drop`" and impl-level "user-moveout +
	live-at-Return" — pick out the same set of locals on current Drift.

	If/when Drift gains uninitialized declarations, the equivalence
	breaks: this test should flip to a positive assertion (a
	hand-built MirFunc with a destructible local that is StoreLocal'd
	on some arms but never moved would need a flag).  At that point,
	`insert_drop_flags` MUST switch to the design-level criterion AND
	gain a `_block_already_drops`-then-strip-and-replace pattern so
	the legacy unconditional drop on the no-store arm is removed.
	The narrow design contract in `work/ownership-ledger/3c-design.md`
	calls this out explicitly."""
	from pathlib import Path
	grammar_path = Path(__file__).resolve().parents[3] / "lang/driftc/parser/grammar.lark"
	assert grammar_path.exists(), f"grammar file not found at expected path: {grammar_path}"
	grammar_text = grammar_path.read_text()
	# The let_stmt rule must require `EQUAL expr` — no alternative
	# branch allowing uninit decls.
	assert "let_stmt: binder binding_name type_spec? alias_clause? EQUAL expr" in grammar_text, (
		"grammar changed: `let_stmt` no longer requires `EQUAL expr` — "
		"uninitialized declarations may now be expressible.  If so, the "
		"3C pass's user-moveout criterion is no longer equivalent to "
		"the design-level MaybeUninit criterion.  Switch the pass to "
		"detect via the ledger AND replace `_block_already_drops`-skip "
		"with strip-and-replace.  See `work/ownership-ledger/3c-design.md` "
		"for the migration plan."
	)


def test_skip_on_existing_drop_under_user_move_invariant() -> None:
	"""Pin the soundness argument from `3c-design.md`'s
	"Interaction with legacy unconditional scope-drop emission":
	the pass's `_block_already_drops` skip-not-strip behaviour is
	sound only because, in current Drift, no local that meets the
	flag criteria can also have a legacy unconditional drop in the
	same Return block.

	The argument: the pass triggers iff the local has a user
	moveout, which sets `_moved_locals[L]` function-wide, which
	makes legacy `_emit_scope_drops` SKIP emitting a drop for L at
	any Return.  Therefore `_block_already_drops(blk, L)` is always
	False at the Return block we're considering, and the skip-vs-
	insert decision always inserts.

	This test verifies the empirical premise: build a function with
	the canonical bucket-6 carrier shape and confirm the legacy
	emission has emitted ZERO drops for the flagged local at the
	trailing Return BEFORE `insert_drop_flags` runs.  If a future
	HIR→MIR change starts emitting unconditional drops for moved
	locals, this test fires and signals that `_block_already_drops`
	must be revisited."""
	import lang.driftc.stage1 as H
	from lang.driftc.core.span import Span
	from lang.driftc.stage1 import HBlock, HIf, HLet, HLiteralString, HMove, HPlaceExpr, HReturn, HVar
	from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
	from lang.driftc.stage1.normalize import normalize_hir
	from lang.driftc.stage2 import HIRToMIR, make_builder, MoveOut as M2_MoveOut, DropValue as M2_DropValue, Return as M2_Return
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	string_ty = type_table.ensure_string()
	hir = HBlock(statements=[
		HLet(name="s", value=HLiteralString("owned"), declared_type_expr=None, is_mutable=True, binding_id=None),
		HIf(
			cond=HVar("b"),
			then_block=HBlock(statements=[
				HReturn(value=HMove(subject=HPlaceExpr(base=HVar("s"), projections=[], loc=Span()))),
			]),
			else_block=None,
		),
		HReturn(value=HLiteralString("fresh")),
	])
	hir = normalize_hir(hir)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	builder = make_builder(FunctionId(module="main", name="f", ordinal=0))
	builder.func.params = ["b"]
	lower = HIRToMIR(builder, type_table=type_table, param_types={"b": bool_ty}, return_type=string_ty)
	lower.lower_block(hir)
	# Inspect the trailing Return blocks BEFORE drop_flags runs.
	# Look for any MoveOut(s)+DropValue pair — there should be NONE,
	# because legacy `_moved_locals` poisoning skipped the scope-drop.
	pre_pass_drops_for_s = 0
	for blk in builder.func.blocks.values():
		if not isinstance(blk.terminator, M2_Return):
			continue
		moveout_dests: set[str] = set()
		for ins in blk.instructions:
			if isinstance(ins, M2_MoveOut) and getattr(ins, "local", None) == "s":
				moveout_dests.add(getattr(ins, "dest", ""))
			elif isinstance(ins, M2_DropValue) and getattr(ins, "value", None) in moveout_dests:
				pre_pass_drops_for_s += 1
	assert pre_pass_drops_for_s == 0, (
		f"3C soundness invariant violated: legacy `_emit_scope_drops` "
		f"emitted {pre_pass_drops_for_s} unconditional drops for `s` at "
		f"Return blocks despite `s` being user-moved on the if-then arm. "
		f"`insert_drop_flags`'s `_block_already_drops`-skip behaviour "
		f"would now be UNSAFE (would leave the unconditional drop in "
		f"place; the flag-guarded drop in a separate block would still "
		f"fire on the no-move path → but the ALREADY-EMITTED unconditional "
		f"drop also fires on every path → double-drop on the move path). "
		f"Either revert HIR→MIR's scope-drop emission to its previous "
		f"behaviour, or upgrade `insert_drop_flags` to strip-and-replace "
		f"existing scope-drops for flagged locals (see 3c-design.md "
		f"\"Interaction with legacy unconditional scope-drop emission\")."
	)


def test_param_flag_initialized_to_true() -> None:
	"""A function parameter is live at entry (params are initialized
	by the caller).  Its flag must init to `true`, not `false`."""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	drop_ty = _make_droppable_struct(type_table)
	func = _empty_fn(
		"f_param",
		params=["p", "b"],
		locals_=["p", "b"],
		types={"p": drop_ty, "b": bool_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="p", ty=drop_ty))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	assert "__drop_flag_p" in func.locals
	# First instructions in entry should be flag init.  Find the
	# StoreLocal(__drop_flag_p, <const>) and trace its value back to a
	# ConstBool.
	entry_instrs = func.blocks["entry"].instructions
	flag_init_value: bool | None = None
	const_dests: dict[str, bool] = {}
	for ins in entry_instrs:
		if isinstance(ins, M.ConstBool):
			const_dests[ins.dest] = ins.value
		elif isinstance(ins, M.StoreLocal) and ins.local == "__drop_flag_p":
			if ins.value in const_dests:
				flag_init_value = const_dests[ins.value]
				break
	assert flag_init_value is True, (
		f"param flag should init to True (param is live at entry); "
		f"got init value = {flag_init_value!r}"
	)
