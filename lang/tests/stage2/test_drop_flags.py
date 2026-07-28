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


def test_terminating_arm_leak_shape_allocates_flag_and_attaches_metadata() -> None:
	"""Post Bug 2 architecture flip: `insert_drop_flags` is a pure
	planning pass.  It allocates the flag, instruments entry init and
	set/clear, and attaches metadata — but emits NO cleanup drops.

	The flag-guarded drop at the scope-exit `CleanupHook` is emitted by
	`cleanup_authoring.author_cleanup` (the sole emission point per the
	architecture).  End-to-end coverage of the emission shape lives in
	`test_cleanup_authoring_flag_guarded.py` plus the destructor-order
	e2e.  This test pins the planning contract."""
	func, type_table, _ = _build_terminating_arm_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	assert "__drop_flag_x" in func.locals, "flag local must be allocated for bucket-6 carrier"
	managed = getattr(func, "_drop_flag_managed_locals", set())
	assert "x" in managed, (
		"_drop_flag_managed_locals must include `x` — this is the metadata "
		"cleanup_authoring consults to decide guarded vs unguarded emission"
	)
	flag_for = getattr(func, "_drop_flag_for_local", {})
	assert flag_for.get("x") == "__drop_flag_x", (
		"_drop_flag_for_local must map `x` → its allocated flag local; "
		"cleanup_authoring uses this for `LoadLocal(flag)` and must not "
		"reconstruct the name from `flag_local_name_for` (collision-unsafe)"
	)
	# Planning ONLY: NO drop blocks should be inserted.  The Step-5
	# Return-block emission was retired in the Bug 2 architecture flip.
	drop_block_names = [n for n in func.blocks if "_drop_x" in n]
	assert drop_block_names == [], (
		f"insert_drop_flags must NOT emit cleanup drops post-flip; "
		f"got drop blocks: {drop_block_names}.  Cleanup drops are emitted "
		f"by cleanup_authoring at the CleanupHook positions HIR→MIR emits."
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


def test_non_terminating_conditional_move_allocates_flag_and_attaches_metadata() -> None:
	"""Post Bug 2 architecture flip: planning only.  See
	`test_terminating_arm_leak_shape_allocates_flag_and_attaches_metadata`
	for the contract.  Emission shape coverage is in
	`test_cleanup_authoring_flag_guarded.py`."""
	func, type_table, _ = _build_non_terminating_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	assert "__drop_flag_x" in func.locals
	flag_for = getattr(func, "_drop_flag_for_local", {})
	assert flag_for.get("x") == "__drop_flag_x"
	# The if_join terminator must be unchanged (planning does NOT
	# rewrite terminators).
	join_term = func.blocks["if_join"].terminator
	assert isinstance(join_term, M.Return), (
		"planning must NOT rewrite the Return terminator — the architecture "
		"flip moved emission into cleanup_authoring at the CleanupHook "
		f"position; got {type(join_term).__name__}"
	)


# -- RAII invariant ---------------------------------------------------------


def test_raii_invariant_planning_does_not_rewrite_terminators() -> None:
	"""Post-flip RAII invariant: planning does NOT rewrite terminators.
	The original `Goto(if_join)` from if_then must survive; rewriting
	it would imply cleanup was pushed earlier — RAII violation.

	End-to-end RAII timing (drop emitted at the original scope-exit
	point) is covered by `test_cleanup_authoring_flag_guarded.py` and
	the destructor-order e2e under
	`lang/tests/codegen/e2e/conditional_move_loop_destructor_order/`."""
	func, type_table, drop_ty = _build_non_terminating_fixture()
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	then_term = func.blocks["if_then"].terminator
	assert isinstance(then_term, M.Goto), (
		"if_then terminator should remain Goto(if_join) after pass; "
		"if it changed, the pass pushed cleanup earlier — RAII violation"
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


# -- Arm B admission pins (zero-storage-safe drop-flag retirement, ------
# -- 2026-07-20; checkpoint §7.1) ----------------------------------------


def _declare_destructible_variant_tf(type_table: TypeTable) -> int:
	from lang.driftc.core.types_core import (
		GenericTypeExpr,
		VariantArmSchema,
		VariantFieldSchema,
	)
	base = type_table.declare_variant(
		"test", "Vf", [],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr(name="String", args=[]))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	return type_table.ensure_variant_instantiated(base, [])


def _conditional_move_fixture(type_table: TypeTable, ty: int) -> M.MirFunc:
	"""The 2a carrier shape (`var x; if b { move x; } return;`) with a
	caller-chosen local type."""
	bool_ty = type_table.ensure_bool()
	func = _empty_fn(
		"f_armb", params=["b"], locals_=["b", "x", "t"],
		types={"b": bool_ty, "x": ty, "t": ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="x", ty=ty))
	then_block.instructions.append(M.StoreLocal(local="t", value="t_move"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	return func


def test_zero_storage_safe_types_never_flag_admitted_2a() -> None:
	"""2a positive/negative controls: on the IDENTICAL
	conditional-move live-at-exit carrier, a zero-UNSAFE struct is
	still admitted (positive — the exclusion must not disturb the
	surviving population), while Array and Variant are NEVER admitted
	(negative — `zero_storage_drop_safe` is an ADDITIONAL exclusion;
	their PathDependent cleanup is authored unguarded and a flag
	would gate nothing)."""
	# Positive: zero-unsafe struct → flag.
	tt = TypeTable()
	sty = _make_droppable_struct(tt)
	f_pos = _conditional_move_fixture(tt, sty)
	insert_drop_flags(f_pos, type_table=tt, drop_policy=_drop_policy_for(tt))
	assert "__drop_flag_x" in f_pos.locals
	assert getattr(f_pos, "_drop_flag_for_local", {}).get("x") == "__drop_flag_x"
	# Negative: Array<String> → never admitted.
	tt2 = TypeTable()
	arr_ty = tt2.new_array(tt2.ensure_string())
	f_arr = _conditional_move_fixture(tt2, arr_ty)
	f_arr, mutated_arr = insert_drop_flags(f_arr, type_table=tt2, drop_policy=_drop_policy_for(tt2))
	assert not mutated_arr
	assert "__drop_flag_x" not in f_arr.locals
	assert "x" not in (getattr(f_arr, "_drop_flag_for_local", {}) or {})
	# Negative: destructible Variant → never admitted.
	tt3 = TypeTable()
	vty = _declare_destructible_variant_tf(tt3)
	f_var = _conditional_move_fixture(tt3, vty)
	f_var, mutated_var = insert_drop_flags(f_var, type_table=tt3, drop_policy=_drop_policy_for(tt3))
	assert not mutated_var
	assert "__drop_flag_x" not in f_var.locals
	assert "x" not in (getattr(f_var, "_drop_flag_for_local", {}) or {})
	# User-moveout precondition control: zero-unsafe struct WITHOUT a
	# user move is never admitted regardless of type (existing
	# criterion, unchanged by the exclusion).
	tt4 = TypeTable()
	sty4 = _make_droppable_struct(tt4)
	f_nomove = _empty_fn("f_nomove", params=[], locals_=["x"], types={"x": sty4})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	f_nomove.blocks = {"entry": entry}
	f_nomove, mutated_nm = insert_drop_flags(f_nomove, type_table=tt4, drop_policy=_drop_policy_for(tt4))
	assert not mutated_nm
	assert "__drop_flag_x" not in f_nomove.locals


def test_2b_hook_criterion_excludes_zero_storage_safe_types() -> None:
	"""2b positive/negative controls at the criterion itself
	(`_has_zero_storage_unsafe_path_dependent_at_cleanup_hook`): a
	PathDependent candidate at a mid-fn CleanupHook admits a
	zero-UNSAFE struct and rejects Array/Variant on the identical
	carrier."""
	from lang.driftc.stage2.drop_flags import (
		_has_zero_storage_unsafe_path_dependent_at_cleanup_hook,
	)
	from lang.driftc.stage2.ownership_ledger import build_ledger

	def build(tt: TypeTable, ty: int) -> M.MirFunc:
		bool_ty = tt.ensure_bool()
		func = _empty_fn(
			"f_2b", params=["b"], locals_=["b", "x", "t"],
			types={"b": bool_ty, "x": ty, "t": ty},
		)
		entry = M.BasicBlock(name="entry")
		entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
		entry.terminator = M.IfTerminator(cond="b", then_target="mv", else_target="hook")
		mv = M.BasicBlock(name="mv")
		mv.instructions.append(M.MoveOut(dest="t_move", local="x", ty=ty))
		mv.instructions.append(M.StoreLocal(local="t", value="t_move"))
		mv.terminator = M.Goto(target="hook")
		hk = M.BasicBlock(name="hook")
		hk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", ty)]))
		hk.terminator = M.Return(value=None)
		func.blocks = {"entry": entry, "mv": mv, "hook": hk}
		return func

	def check(tt: TypeTable, ty: int) -> bool:
		func = build(tt, ty)
		ledger = build_ledger(func, drop_policy=_drop_policy_for(tt))
		return _has_zero_storage_unsafe_path_dependent_at_cleanup_hook(
			ledger=ledger, func=func, type_table=tt,
			drop_policy=_drop_policy_for(tt), local_name="x",
		)

	tt = TypeTable()
	assert check(tt, _make_droppable_struct(tt)) is True
	tt2 = TypeTable()
	assert check(tt2, tt2.new_array(tt2.ensure_string())) is False
	tt3 = TypeTable()
	assert check(tt3, _declare_destructible_variant_tf(tt3)) is False


def test_2b_admission_through_insert_drop_flags() -> None:
	"""Review amendment (2026-07-20): prove the FULL approved admission
	contract `needs_drop && !zs && user_moveout && (2a || 2b)` through
	the PRODUCTION entry point on a 2a-FALSE / 2b-TRUE carrier — the
	criterion-level helper pin above stays as supplemental coverage.

	Carrier: conditional move feeding a mid-fn CleanupHook where the
	candidate is PathDependent, in a function with NO `Return` block
	anywhere — its terminal block ends in `Unreachable` (the CFG also
	carries the diamond's `IfTerminator`/`Goto`); 2a counts Return
	blocks only, so 2a is False by construction.  The zero-unsafe
	Struct must be ADMITTED purely via 2b; Array and Variant on the
	IDENTICAL carrier must NOT be."""
	def build(tt: TypeTable, ty: int) -> M.MirFunc:
		bool_ty = tt.ensure_bool()
		func = _empty_fn(
			"f_2b_e2e", params=["b"], locals_=["b", "x", "t"],
			types={"b": bool_ty, "x": ty, "t": ty},
		)
		entry = M.BasicBlock(name="entry")
		entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
		entry.terminator = M.IfTerminator(cond="b", then_target="mv", else_target="hook")
		mv = M.BasicBlock(name="mv")
		mv.instructions.append(M.MoveOut(dest="t_move", local="x", ty=ty))
		mv.instructions.append(M.StoreLocal(local="t", value="t_move"))
		mv.terminator = M.Goto(target="hook")
		hk = M.BasicBlock(name="hook")
		hk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", ty)]))
		hk.terminator = M.Unreachable()
		func.blocks = {"entry": entry, "mv": mv, "hook": hk}
		return func

	# 2b-positive: zero-unsafe struct admitted through the production
	# admission function with 2a False.
	tt = TypeTable()
	sty = _make_droppable_struct(tt)
	f_pos = build(tt, sty)
	f_pos, mutated = insert_drop_flags(f_pos, type_table=tt, drop_policy=_drop_policy_for(tt))
	assert mutated
	assert "__drop_flag_x" in f_pos.locals
	assert getattr(f_pos, "_drop_flag_for_local", {}).get("x") == "__drop_flag_x"
	# 2b-negative: Array and Variant on the identical carrier.
	tt2 = TypeTable()
	f_arr = build(tt2, tt2.new_array(tt2.ensure_string()))
	f_arr, m_arr = insert_drop_flags(f_arr, type_table=tt2, drop_policy=_drop_policy_for(tt2))
	assert not m_arr and "__drop_flag_x" not in f_arr.locals
	tt3 = TypeTable()
	f_var = build(tt3, _declare_destructible_variant_tf(tt3))
	f_var, m_var = insert_drop_flags(f_var, type_table=tt3, drop_policy=_drop_policy_for(tt3))
	assert not m_var and "__drop_flag_x" not in f_var.locals


# -- criterion 2c: zero-unsafe PathDependent at an OVERWRITE site -------------


def test_overwrite_site_carrier_flagged_via_2c_not_exit_liveness() -> None:
	"""A local that is conditionally moved, OVERWRITTEN, then
	UNCONDITIONALLY moved out before the Return is PathDependent ONLY at
	its overwrite site: at the exit it is MovedOut (criterion 2a false) and
	there is no cleanup hook (2b false).  Criterion 2c must flag it — else
	the site-4 authority fails closed on the zero-unsafe PathDependent
	overwrite.

	CFG:
	    entry:  StoreLocal(x, t_init); If(cond, a, b)
	    a:      MoveOut(t_taken, x); Goto(join)        # moved on this branch
	    b:      Goto(join)                             # live on this branch
	    join:   StoreLocal(x, t_new)                   # OVERWRITE — 2c site
	            MoveOut(t_final, x); Return(t_final)    # unconditional move-out
	"""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.drop_flags import (
		_is_potentially_live_at_some_exit,
		_has_zero_storage_unsafe_path_dependent_at_cleanup_hook,
		_has_zero_storage_unsafe_path_dependent_at_overwrite_site,
	)

	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	bool_ty = type_table.ensure_bool()
	func = _empty_fn(
		"ov2c", params=["cond"], locals_=["cond", "x"],
		types={"cond": bool_ty, "x": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.IfTerminator(cond="cond", then_target="a", else_target="b")
	a = M.BasicBlock(name="a")
	a.instructions.append(M.MoveOut(dest="t_taken", local="x", ty=drop_ty))
	a.terminator = M.Goto(target="join")
	b = M.BasicBlock(name="b")
	b.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.instructions.append(M.StoreLocal(local="x", value="t_new"))
	join.instructions.append(M.MoveOut(dest="t_final", local="x", ty=drop_ty))
	join.terminator = M.Return(value="t_final")
	for blk in (entry, a, b, join):
		func.blocks[blk.name] = blk
	func.entry = "entry"

	dp = _drop_policy_for(type_table)

	# Prove the 2a/2b criteria do NOT admit x, but 2c does.
	ledger = build_ledger(func, drop_policy=dp)
	assert _is_potentially_live_at_some_exit(ledger, func, "x") is False, (
		"x is unconditionally moved out before the Return → not live at exit")
	assert _has_zero_storage_unsafe_path_dependent_at_cleanup_hook(
		ledger=ledger, func=func, type_table=type_table,
		drop_policy=dp, local_name="x") is False, "no cleanup hook in this MIR"
	assert _has_zero_storage_unsafe_path_dependent_at_overwrite_site(
		ledger=ledger, func=func, type_table=type_table,
		drop_policy=dp, local_name="x") is True, (
		"the join-block overwrite of x is zero-unsafe PathDependent → 2c admits")

	# Integration: insert_drop_flags therefore allocates a flag for x.
	insert_drop_flags(func, type_table=type_table, drop_policy=dp)
	flag_locals = [n for n in func.locals if n.startswith("__drop_flag_")]
	assert flag_locals, "criterion 2c must flag the overwrite-site carrier"
	assert func._drop_flag_for_local.get("x") in flag_locals


def test_match_binder_zero_unsafe_pathdependent_overwrite_is_flaggable() -> None:
	"""`drop_flags` provides proper compiler SUPPORT for a zero-storage-UNSAFE
	`__match_binder_*` that is PathDependent at an OVERWRITE site: it IS
	flag-manageable despite the leading `__` — otherwise the site-4
	drop-before-overwrite authority would fail closed on a valid
	compiler-authored binder (a `var` match binder conditionally moved then
	overwritten).  This is compiler support, NOT a source-side workaround
	(masking it by skipping was rejected in review, 2026-07-27); the real
	lowering is exercised end-to-end by
	`test_match_binder_guarded_site4_lowering.py`.

	The allowlist is `__match_binder_*` ONLY; all other `__` internal locals
	(`__borrow_tmp*`, `__match_scrut*`, `__try_err*`) stay skipped — see
	`test_allowlist_exempts_match_binder_only_not_borrow_tmp`."""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.drop_flags import (
		_has_zero_storage_unsafe_path_dependent_at_overwrite_site,
	)
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	bool_ty = type_table.ensure_bool()
	binder = "__match_binder_9_vspans"   # compiler-generated, but typed + destructible
	func = _empty_fn(
		"binder_ov", params=["cond"], locals_=["cond", binder],
		types={"cond": bool_ty, binder: drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local=binder, value="t_init"))
	entry.terminator = M.IfTerminator(cond="cond", then_target="a", else_target="b")
	a = M.BasicBlock(name="a")
	a.instructions.append(M.MoveOut(dest="t_taken", local=binder, ty=drop_ty))
	a.terminator = M.Goto(target="join")
	b = M.BasicBlock(name="b")
	b.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.instructions.append(M.StoreLocal(local=binder, value="t_new"))   # OVERWRITE (2c site)
	join.instructions.append(M.MoveOut(dest="t_final", local=binder, ty=drop_ty))
	join.terminator = M.Return(value="t_final")
	for blk in (entry, a, b, join):
		func.blocks[blk.name] = blk
	func.entry = "entry"

	dp = _drop_policy_for(type_table)
	ledger = build_ledger(func, drop_policy=dp)
	assert _has_zero_storage_unsafe_path_dependent_at_overwrite_site(
		ledger=ledger, func=func, type_table=type_table,
		drop_policy=dp, local_name=binder) is True

	insert_drop_flags(func, type_table=type_table, drop_policy=dp)
	# The __match_binder_ local IS flagged (allowlist / proper support).
	assert func._drop_flag_for_local.get(binder) is not None, (
		"drop_flags must flag a zero-unsafe PathDependent __match_binder_*")
	# Internal temps stay skipped.
	assert not any(n.startswith("__match_scrut") for n in func._drop_flag_for_local)


def test_allowlist_exempts_match_binder_only_not_borrow_tmp() -> None:
	"""The `drop_flags` allowlist exempts `__match_binder_*` ONLY.  This pins
	the FILTER boundary directly: a `__borrow_tmp` local built in the exact
	flaggable move-then-overwrite shape is STILL skipped (the `__` prefix
	filter exempts only `__match_binder_`, so `__borrow_tmp` never reaches the
	flag criteria).  We claim nothing about whether real materialised borrow
	temps ever take this shape — the point is that the allowlist does not
	extend to them, so no inert support is claimed."""
	type_table = TypeTable()
	drop_ty = _make_droppable_struct(type_table)
	bool_ty = type_table.ensure_bool()
	# Build the SAME PathDependent-overwrite shape but on a __borrow_tmp name.
	bt = "__borrow_tmp_3"
	func = _empty_fn(
		"bt_ov", params=["cond"], locals_=["cond", bt],
		types={"cond": bool_ty, bt: drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local=bt, value="t_init"))
	entry.terminator = M.IfTerminator(cond="cond", then_target="a", else_target="b")
	a = M.BasicBlock(name="a")
	a.instructions.append(M.MoveOut(dest="t_taken", local=bt, ty=drop_ty))
	a.terminator = M.Goto(target="join")
	b = M.BasicBlock(name="b")
	b.terminator = M.Goto(target="join")
	join = M.BasicBlock(name="join")
	join.instructions.append(M.StoreLocal(local=bt, value="t_new"))
	join.instructions.append(M.MoveOut(dest="t_final", local=bt, ty=drop_ty))
	join.terminator = M.Return(value="t_final")
	for blk in (entry, a, b, join):
		func.blocks[blk.name] = blk
	func.entry = "entry"

	dp = _drop_policy_for(type_table)
	insert_drop_flags(func, type_table=type_table, drop_policy=dp)
	flag_for = getattr(func, "_drop_flag_for_local", {}) or {}
	assert flag_for.get(bt) is None, (
		"the allowlist exempts __match_binder_* ONLY, so a __borrow_tmp local "
		"is skipped by the general `__` filter before the flag criteria")
