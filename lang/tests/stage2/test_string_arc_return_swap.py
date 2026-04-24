# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3B step 2 — `string_arc_return` consumer-swap pin.

Pins the consumer-swap contract for site 3 in `string_arc.py`'s
function-exit cleanup loop:

- Site 3 emits drops at function-exit Return blocks for destructible
  locals via `_drop_all_destructibles`, gated by `skip_cleanup_locals`
  and `initialized_at_return`.
- After Phase 3C `insert_drop_flags` runs, some locals carry a
  `__drop_flag_<L>` runtime drop-flag.  3C's inserted drop block (via
  the original Return's `IfTerminator(flag)`) is the sole authority
  on those locals' scope-exit cleanup.
- Site 3 must SKIP emission for any flagged local: emission would
  produce a second `MoveOut(L) + DropValue(L)` after the 3C drop
  block, double-dropping on the path that traverses the 3C drop.
- Detection uses the canonical `is_flag_managed(func, L)` helper from
  `drop_flags`, NOT ad-hoc string matching at the site.

These tests build minimal MIR fixtures, run `insert_drop_flags`
followed by `insert_string_arc`, and assert MIR-shape outcomes:

  - Flagged local: one drop emission (3C's, in the
    `<orig>_drop_<L>` block) + zero from site 3.
  - Unflagged destructible at the same Return: drop emitted by site 3
    in the original Return block's instruction list.
  - Negative double-drop guard: no second `MoveOut(flagged_local) +
    DropValue` anywhere outside 3C's drop block.
  - Observe records: skipped flagged locals get the distinct
    `drop_flag_owned` reason so observe triage can see the
    responsibility split.
"""

from __future__ import annotations

import os

from lang.driftc.checker import FnInfo
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import (
	GenericTypeExpr,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.drop_flags import insert_drop_flags, is_flag_managed, flag_local_name_for
from lang.driftc.stage2.drop_policy_compute import compute_drop_policy
from lang.driftc.stage2.ownership_ledger import build_ledger
from lang.driftc.stage2.string_arc import insert_string_arc, variant_zero_tag_drop_safe


def _drop_policy_for(type_table: TypeTable):
	def fn(ty):
		return compute_drop_policy(type_table, ty)
	return fn


def _make_droppable_struct(type_table: TypeTable, name: str = "DropMe") -> int:
	"""String-bearing destructible struct.  String-as-field gets it
	into `string_arc.destructible_locals` (via recursive
	`_type_needs_drop`); destructor_fns entry makes it non-nullsafe
	so it follows the conditional `initialized_at_return` flow."""
	string_ty = type_table.ensure_string()
	tid = type_table.declare_struct(module_id="test", name=name, field_names=["inner"])
	type_table.define_struct_fields(tid, field_types=[string_ty])
	if not hasattr(type_table, "destructor_fns"):
		type_table.destructor_fns = {}
	type_table.destructor_fns[tid] = FunctionId(module="test", name=f"{name}::destroy", ordinal=0)
	non_copy = {tid}
	prev_query = getattr(type_table, "_copy_query", None)
	def _query(t):
		if t in non_copy:
			return False
		return prev_query(t) if prev_query else None
	type_table._copy_query = _query  # type: ignore[attr-defined]
	return tid


def _make_func(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _attach_ledger(func: M.MirFunc) -> None:
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)


def _build_one_flagged_one_unflagged(type_table: TypeTable) -> M.MirFunc:
	"""Function with two destructible locals at the same Return:
	  - `flagged`: gets a 3C flag (user move on a conditional arm).
	  - `unflagged`: no user move, just a destructible held to the
	    return.

	Shape:
	  fn f(b: Bool) {
	      var flagged = make();
	      var unflagged = make();
	      if b { val t = move flagged; }   // makes flagged path-dependent
	      return;
	  }
	"""
	bool_ty = type_table.ensure_bool()
	drop_ty = _make_droppable_struct(type_table, name="DropMe")
	func = _make_func(
		"f",
		params=["b"],
		locals_=["b", "flagged", "unflagged", "t"],
		types={"b": bool_ty, "flagged": drop_ty, "unflagged": drop_ty, "t": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="flagged", value="t_init_flagged"))
	entry.instructions.append(M.StoreLocal(local="unflagged", value="t_init_unflagged"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="flagged", ty=drop_ty))
	then_block.instructions.append(M.StoreLocal(local="t", value="t_move"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	return func


# -- helper --------------------------------------------------------------


def _all_drop_destructible_pairs_for(func: M.MirFunc, local_name: str) -> list[tuple[str, int]]:
	"""Return [(block_name, instr_idx_of_DropValue), ...] for every
	post-string_arc drop emission of `local_name` — i.e. each
	`LoadLocal(t, L) ... DropValue(t)` sequence.

	Both site 3's `_drop_destructible_local` and Phase 3C's drop-block
	`MoveOut+DropValue` are expanded by string_arc into
	`LoadLocal+ZeroValue+StoreLocal+DropValue`.  Counting the post-
	expansion shape catches both, which is what double-drop accounting
	requires."""
	hits: list[tuple[str, int]] = []
	for blk_name, blk in func.blocks.items():
		instrs = blk.instructions
		loadlocal_dests: dict[str, int] = {}
		for i, ins in enumerate(instrs):
			if isinstance(ins, M.LoadLocal) and getattr(ins, "local", None) == local_name:
				dest = getattr(ins, "dest", None)
				if isinstance(dest, str):
					loadlocal_dests[dest] = i
			elif isinstance(ins, M.DropValue):
				v = getattr(ins, "value", None)
				if v in loadlocal_dests:
					hits.append((blk_name, i))
	return hits


# -- swap behaviour ------------------------------------------------------


def test_site3_skips_flagged_local_at_return() -> None:
	"""Site 3 must NOT emit a drop for `flagged` at the function-exit
	Return.  3C has already inserted a flag-guarded drop block; site 3
	emission would double-drop on the path through that block."""
	type_table = TypeTable()
	func = _build_one_flagged_one_unflagged(type_table)
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	# Confirm 3C added a flag for `flagged`.
	assert is_flag_managed(func, "flagged"), "test setup: 3C did not flag `flagged`"
	assert not is_flag_managed(func, "unflagged"), "test setup: `unflagged` was unexpectedly flagged"
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	# Count drops for `flagged`: 3C's drop block contributes exactly
	# one `MoveOut(flagged) + DropValue` pair; site 3 must contribute
	# zero.  Total = 1.
	flagged_drops = _all_drop_destructible_pairs_for(func, "flagged")
	assert len(flagged_drops) == 1, (
		f"site 3 emitted a double-drop for `flagged`: expected 1 drop "
		f"(from 3C's drop block), got {len(flagged_drops)} — "
		f"locations: {flagged_drops}.  This is the bug option 2 was "
		f"chosen to prevent."
	)
	# The single drop should be in 3C's drop block, not in the
	# original `if_join` Return block.
	(blk_name, _) = flagged_drops[0]
	assert "drop_flagged" in blk_name or "_drop_" in blk_name, (
		f"the single drop for `flagged` is in block {blk_name!r}; "
		f"expected 3C's `if_join_drop_flagged` block.  If the drop is "
		f"in the original Return block, site 3 emitted it and 3C "
		f"didn't — violates the option-2 split."
	)


def test_site3_still_drops_unflagged_destructible_at_same_return() -> None:
	"""Site 3 must continue to emit a drop for `unflagged` (no flag)
	at the Return.  The skip is targeted at flagged locals only."""
	type_table = TypeTable()
	func = _build_one_flagged_one_unflagged(type_table)
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	unflagged_drops = _all_drop_destructible_pairs_for(func, "unflagged")
	assert len(unflagged_drops) >= 1, (
		f"site 3 silently skipped `unflagged` (a destructible local "
		f"with no 3C flag): no drop emitted at the Return → leak.  "
		f"Site 3 must skip ONLY flagged locals; unflagged destructibles "
		f"continue on the legacy/ledger emission path."
	)


def test_no_second_drop_pair_for_flagged_local_outside_3c_drop_block() -> None:
	"""Negative double-drop guard: every drop-emission pair for
	`flagged` (post-string_arc `LoadLocal(t, flagged) ... DropValue(t)`)
	MUST live in either:
	  - `if_then` (the user move's expansion that consumed `flagged`
	    into binder `t`), OR
	  - 3C's `<orig>_drop_flagged` block (the flag-guarded drop).

	A pair anywhere else (notably the original `if_join` Return block
	or the `if_join_dropfinal` block) means site 3 emitted a duplicate
	scope-drop for the flagged local — exactly the double-drop option 2
	was chosen to prevent."""
	type_table = TypeTable()
	func = _build_one_flagged_one_unflagged(type_table)
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	pairs = _all_drop_destructible_pairs_for(func, "flagged")
	allowed_blocks = {"if_then"}
	for blk_name in func.blocks:
		if "_drop_flagged" in blk_name:
			allowed_blocks.add(blk_name)
	for blk_name, _idx in pairs:
		assert blk_name in allowed_blocks, (
			f"unexpected drop-pair for `flagged` in block {blk_name!r}; "
			f"allowed only in {sorted(allowed_blocks)}.  Site 3 emitted "
			f"a duplicate scope-drop for the flagged local — double-"
			f"drop on the path through 3C's drop block."
		)


# -- observe records ----------------------------------------------------


def test_site3_emits_drop_flag_owned_observe_record_for_skipped_flagged_local(capfd) -> None:
	"""Under observe mode, site 3 emits a `drop_flag_owned` record
	for every flagged local it skips.  This documents the
	responsibility split in the observe stream so triage doesn't
	misread the missing site-3 emission as a bucket-5/6 regression."""
	type_table = TypeTable()
	func = _build_one_flagged_one_unflagged(type_table)
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	_attach_ledger(func)
	old_env = os.environ.get("DRIFT_COMPILER_DEBUG")
	os.environ["DRIFT_COMPILER_DEBUG"] = '{"ownership_ledger":true}'
	from lang.driftc import debug as drift_debug
	drift_debug._cached_flags = None
	try:
		insert_string_arc(func, type_table=type_table, fn_infos={})
	finally:
		drift_debug._cached_flags = None
		if old_env is None:
			os.environ.pop("DRIFT_COMPILER_DEBUG", None)
		else:
			os.environ["DRIFT_COMPILER_DEBUG"] = old_env
	captured = capfd.readouterr()
	assert "drop_flag_owned" in captured.err, (
		"site 3 did not emit a `drop_flag_owned` observe record for "
		"the skipped flagged local — observe triage will see this as "
		"a missing site-3 emission and report a false bucket-5/6 "
		"regression on the next observe re-run"
	)
	# And the flagged local's name should appear in such a record.
	assert "flagged" in captured.err, (
		"`drop_flag_owned` record missing the flagged local's name"
	)


# -- helper API ---------------------------------------------------------


def test_is_flag_managed_recognizes_base_and_suffixed_names() -> None:
	"""The detection helper must work for both `__drop_flag_<L>` and
	`__drop_flag_<L>_<n>` (numeric collision suffix)."""
	fn_id = FunctionId(module="test", name="f", ordinal=0)
	func = M.MirFunc(
		name="test::f",
		params=[],
		locals=["x", "y", flag_local_name_for("x"), flag_local_name_for("y") + "_1"],
		fn_id=fn_id,
		local_types={},
	)
	# Mirror what `insert_drop_flags` does: explicitly mark which
	# source locals are flag-managed.
	setattr(func, "_drop_flag_managed_locals", {"x", "y"})
	assert is_flag_managed(func, "x")
	assert is_flag_managed(func, "y")
	assert not is_flag_managed(func, "z")  # no flag local exists for z
	# The flag local itself is not "flag-managed."
	assert not is_flag_managed(func, "__drop_flag_x")


def test_is_flag_managed_does_not_misattribute_collision_suffixed_flag(type_table_factory=None) -> None:
	"""K-found collision shape (regression).

	Build a function with two named locals `x` and `x_1`.  Apply
	`insert_drop_flags`; a user move on `x` causes
	`_allocate_flag_name` to choose `__drop_flag_x` first — but if
	`x_1` is already a local AND `__drop_flag_x` happens to collide
	(e.g. because both `x` and the existing local list intersect with
	allocation), the suffix-resolved name `__drop_flag_x_1` could be
	misread by a name-parsing helper as evidence that `x_1` is
	flag-managed.

	The realistic shape: two source locals named `x` and
	`__drop_flag_x_1` (the latter is a contrived but valid Drift
	local name — there is no language rule reserving the
	`__drop_flag_` prefix on user code).  When `x` gets a flag, the
	canonical name `__drop_flag_x` is taken (by `__drop_flag_x_1`?
	no — by NOTHING; let me reshape the test).

	The cleaner repro: pre-populate `func.locals` with `x`, `x_1`,
	AND `__drop_flag_x` (simulating a prior pass that named a local
	with the canonical-flag-prefix shape).  Then run `insert_drop_flags`
	with a user move on `x`.  `_allocate_flag_name` sees
	`__drop_flag_x` is taken → suffixes to `__drop_flag_x_1` which
	IS now a flag-local for `x`.  After the pass, a name-parsing
	`is_flag_managed("x_1")` would read `__drop_flag_x_1` and
	(incorrectly) conclude that `x_1` is itself flag-managed —
	even though `x_1` is just a regular local with no flag.

	Site 3 would then skip cleanup for `x_1` and emit `drop_flag_owned`
	for the wrong local — a real correctness bug.  The metadata-based
	helper (post-fix) reads `func._drop_flag_managed_locals` which
	contains `{"x"}` and correctly says `x_1` is NOT flag-managed.
	"""
	type_table = TypeTable()
	bool_ty = type_table.ensure_bool()
	drop_ty = _make_droppable_struct(type_table, name="DropMe")
	# Pre-populate with `__drop_flag_x` so the canonical name is
	# taken; force `_allocate_flag_name` to pick the `_1` suffix.
	func = _make_func(
		"collision",
		params=["b"],
		locals_=["b", "x", "x_1", "__drop_flag_x", "t"],
		types={"b": bool_ty, "x": drop_ty, "x_1": drop_ty, "__drop_flag_x": bool_ty, "t": drop_ty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init_x"))
	entry.instructions.append(M.StoreLocal(local="x_1", value="t_init_x1"))
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.MoveOut(dest="t_move", local="x", ty=drop_ty))
	then_block.instructions.append(M.StoreLocal(local="t", value="t_move"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	from lang.driftc.stage2.drop_flags import insert_drop_flags
	insert_drop_flags(func, type_table=type_table, drop_policy=_drop_policy_for(type_table))
	# Confirm the collision actually happened — `__drop_flag_x_1`
	# should now be in func.locals as `x`'s flag (because
	# `__drop_flag_x` was already taken).
	assert "__drop_flag_x_1" in func.locals, (
		"test setup invariant broken: expected `_allocate_flag_name` "
		"to suffix `_1` due to the pre-existing `__drop_flag_x` "
		"collision; flag allocation may have changed shape"
	)
	# `x` IS flag-managed (the pass allocated a flag for it).
	assert is_flag_managed(func, "x"), (
		"`x` should be flag-managed: insert_drop_flags ran with a user "
		"move on `x` and a destructible type"
	)
	# `x_1` is NOT flag-managed even though `__drop_flag_x_1` exists
	# in func.locals — that name belongs to `x`, not `x_1`.
	assert not is_flag_managed(func, "x_1"), (
		"K-found false-positive: `is_flag_managed(\"x_1\")` returned "
		"True because `__drop_flag_x_1` exists in func.locals — but "
		"that name is the COLLISION-SUFFIXED flag for `x`, NOT a flag "
		"for `x_1`.  Site 3 would skip cleanup for `x_1` incorrectly "
		"and emit `drop_flag_owned` records for the wrong local.  Fix: "
		"have `insert_drop_flags` attach explicit metadata "
		"(`func._drop_flag_managed_locals: set[str]`) and have "
		"`is_flag_managed` consult that instead of reverse-parsing "
		"local names."
	)


# -- Phase 4 sub-step 3: variant zero-tag widening, ledger-driven --------


def _declare_destructible_variant(type_table: TypeTable, name: str = "V") -> int:
	"""Declare a variant `V { Some(value: String), None }` and return
	its concrete TypeId.  String fields make the type destructible
	(via `_type_needs_drop` recursion), so it lands in
	`destructible_locals`."""
	base = type_table.declare_variant(
		"test",
		name,
		[],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr(name="String", args=[]))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	tid = type_table.ensure_variant_instantiated(base, [])
	# Mark non-Copy so it goes through the destructible path.
	prev_query = getattr(type_table, "_copy_query", None)
	def _query(t):
		if t == tid:
			return False
		return prev_query(t) if prev_query else None
	type_table._copy_query = _query  # type: ignore[attr-defined]
	return tid


# -- Helper-level: variant_zero_tag_drop_safe predicate -----------------


def test_variant_zero_tag_drop_safe_true_for_variant() -> None:
	type_table = TypeTable()
	vty = _declare_destructible_variant(type_table)
	assert variant_zero_tag_drop_safe(vty, type_table) is True


def test_variant_zero_tag_drop_safe_false_for_struct() -> None:
	type_table = TypeTable()
	sty = _make_droppable_struct(type_table, name="DropMeForVariantTest")
	assert variant_zero_tag_drop_safe(sty, type_table) is False


def test_variant_zero_tag_drop_safe_false_for_scalar() -> None:
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	string_ty = type_table.ensure_string()
	assert variant_zero_tag_drop_safe(int_ty, type_table) is False
	assert variant_zero_tag_drop_safe(string_ty, type_table) is False


# -- Site-3 widening: PathDependent + variant → fold into init set ------


def _build_conditional_variant_init(type_table: TypeTable, vty: int) -> M.MirFunc:
	"""Mimic the 0.27.145 carrier shape at MIR level:
	  fn f(b: Bool) {
	      var v: V;             // declared, not initialized
	      if b { v = Some(...); }   // conditional init
	      return;
	  }

	At the join Return: ledger reports `v` as MAYBE_UNINIT (LIVE
	from the `then` branch joined with UNINIT from the implicit
	else).  Site 3 must include `v` in `initialized_at_return` so
	the drop fires (live path leaks otherwise; uninit path's
	tag-0 destroy is a no-op)."""
	bool_ty = type_table.ensure_bool()
	func = _make_func(
		"cond_init_variant",
		params=["b"],
		locals_=["b", "v"],
		types={"b": bool_ty, "v": vty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	# Materialize a variant value via ConstructVariant + StoreLocal.
	then_block.instructions.append(
		M.ConstructVariant(dest="t_v", variant_ty=vty, ctor="None", args=[])
	)
	then_block.instructions.append(M.StoreLocal(local="v", value="t_v"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	return func


def test_site3_widens_path_dependent_variant_into_init_set() -> None:
	"""Variant local conditionally initialized → ledger says
	PathDependent → site-3 widening (now ledger-driven) folds it
	into `initialized_at_return` so a drop is emitted at the join
	Return.  Pinned by the 0.27.145 memcheck carrier
	(`scope_drop_conditional_move`); this test mirrors that shape
	at the MIR/unit level so a regression flips here first."""
	type_table = TypeTable()
	vty = _declare_destructible_variant(type_table)
	func = _build_conditional_variant_init(type_table, vty)
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	# A drop sequence for `v` must appear in the join block (or
	# expanded out of `_drop_destructible_local` in any block).
	drops = _all_drop_destructible_pairs_for(func, "v")
	assert drops, (
		"variant zero-tag widening regressed: a conditionally-"
		"initialized variant local must still be dropped at the join "
		"Return (live path leaks otherwise; uninit path's tag-0 "
		"destroy is a runtime no-op).  The new ledger-driven "
		"widening should emit the drop via PathDependent + variant "
		"type policy."
	)


def test_site3_does_not_widen_non_variant_path_dependent_local() -> None:
	"""Negative: a destructible STRUCT in the same conditionally-
	initialized shape must NOT be widened.  Variant zero-tag safety
	is a variant-specific policy; structs do not have a tag-0 no-op
	destructor, so widening them would risk dropping zero-PHI'd
	storage that the destructor reads as live data → SIGSEGV."""
	type_table = TypeTable()
	sty = _make_droppable_struct(type_table, name="DropMeNonVariant")
	bool_ty = type_table.ensure_bool()
	func = _make_func(
		"cond_init_struct",
		params=["b"],
		locals_=["b", "s"],
		types={"b": bool_ty, "s": sty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_block = M.BasicBlock(name="if_then")
	then_block.instructions.append(M.StoreLocal(local="s", value="t_init"))
	then_block.terminator = M.Goto(target="if_join")
	join_block = M.BasicBlock(name="if_join")
	join_block.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_block, "if_join": join_block}
	_attach_ledger(func)
	insert_string_arc(func, type_table=type_table, fn_infos={})
	drops = _all_drop_destructible_pairs_for(func, "s")
	assert not drops, (
		"non-variant local was widened — `variant_zero_tag_drop_safe` "
		"policy should reject struct types because struct drops are "
		"NOT no-op on zero storage (the destructor reads field bytes "
		"and would crash on PHI-zero data)."
	)
