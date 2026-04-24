# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 4 site-1 patch 1 — `cleanup_authoring.author_cleanup` pins.

Carrier shapes for the function-exit cleanup re-authoring pass:

  1. LIVE local + needs_drop=True → MUST_DROP → emit `MoveOut +
     DropValue`.
  2. MOVED_OUT local → MUST_NOT_DROP → skip.
  3. UNINIT local → MUST_NOT_DROP → skip.
  4. PathDependent + variant local → variant zero-tag widening →
     emit (carrier shape: 0.27.145 conditionally-initialized
     variant).
  5. PathDependent + non-variant local → tripwire (skip + observe
     telemetry).  Today this matches site 1 legacy behaviour
     (`_moved_locals` skip); the telemetry record makes the case
     visible if any real Drift hits it.
  6. Multiple candidates in a single hook are processed
     independently and emitted in the order the hook recorded.
  7. After authoring, the `CleanupHook` instruction is removed.
  8. No CleanupHook in any block → no-op.
  9. No `_ownership_ledger` attached → no-op (hook stays; surfaces
     downstream).

Tests build minimal `MirFunc`s and use `build_ledger` to attach the
ledger.  No HIR lowering involved.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import (
	GenericTypeExpr,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.cleanup_authoring import author_cleanup
from lang.driftc.stage2.ownership_ledger import build_ledger


def _make_func(name: str, *, params: list[str], locals_: list[str], types: dict[str, int]) -> M.MirFunc:
	fn_id = FunctionId(module="test", name=name, ordinal=0)
	return M.MirFunc(
		name=f"test::{name}",
		params=list(params),
		locals=list(locals_),
		fn_id=fn_id,
		local_types=dict(types),
	)


def _make_droppable_struct(type_table: TypeTable, name: str = "DropMe") -> int:
	"""Struct with one String field — destructible via `_type_needs_drop`
	recursion."""
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


def _declare_destructible_variant(type_table: TypeTable, name: str = "V") -> int:
	"""V := Some(value: String) | None — destructible variant.  Sub-step-3
	`variant_zero_tag_drop_safe` policy applies."""
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
	prev_query = getattr(type_table, "_copy_query", None)
	def _query(t):
		if t == tid:
			return False
		return prev_query(t) if prev_query else None
	type_table._copy_query = _query  # type: ignore[attr-defined]
	return tid


def _attach_ledger(func: M.MirFunc) -> None:
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)


def _has_drop_chain_for(func: M.MirFunc, local: str) -> bool:
	"""True iff some block contains `MoveOut(local) → DropValue`."""
	for blk in func.blocks.values():
		instrs = blk.instructions
		for i, ins in enumerate(instrs):
			if isinstance(ins, M.MoveOut) and ins.local == local:
				if i + 1 < len(instrs):
					nxt = instrs[i + 1]
					if isinstance(nxt, M.DropValue) and nxt.value == ins.dest:
						return True
	return False


def _has_cleanup_hook(func: M.MirFunc) -> bool:
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, M.CleanupHook):
				return True
	return False


# -- Carrier 1: LIVE + needs_drop=True → MUST_DROP → emit -----------------


def test_authoring_emits_drop_for_live_destructible() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("live_drop", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 1
	assert _has_drop_chain_for(func, "x")
	assert not _has_cleanup_hook(func)


# -- Carrier 2: MOVED_OUT → MUST_NOT_DROP → skip --------------------------


def test_authoring_skips_moved_out_local() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("moved_skip", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.MoveOut(dest="t_user_move", local="x", ty=dty))
	# A consumer of t_user_move that's NOT a DropValue — marks it as a
	# user move (not a scope-drop pattern), so the local is MOVED_OUT
	# in the ledger but doesn't get the scope-drop pair.
	entry.instructions.append(M.StoreLocal(local="sink", value="t_user_move"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.locals.append("sink")
	func.local_types["sink"] = dty
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0
	# Original MoveOut for the user move is still present; no NEW
	# scope-drop pair was added.  The user MoveOut is at index 1; if
	# authoring had emitted a scope-drop for x, there'd be a SECOND
	# MoveOut(local=x).
	moveouts_for_x = [
		(blk_name, i)
		for blk_name, blk in func.blocks.items()
		for i, ins in enumerate(blk.instructions)
		if isinstance(ins, M.MoveOut) and ins.local == "x"
	]
	assert len(moveouts_for_x) == 1, (
		"authoring emitted a scope-drop for an already-MOVED_OUT "
		"local — the ledger said MUST_NOT_DROP and no second drop "
		"chain should appear."
	)


# -- Carrier 3: UNINIT → MUST_NOT_DROP → skip ----------------------------


def test_authoring_skips_uninit_local() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("uninit_skip", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	# x declared but never stored — UNINIT at the hook.
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0
	assert not _has_drop_chain_for(func, "x")


# -- Carrier 4: PathDependent + variant → emit (zero-tag widening) ------


def test_authoring_emits_drop_for_path_dependent_variant() -> None:
	"""Conditionally-initialized variant local at the function-exit
	join: ledger reports MAYBE_UNINIT → PathDependent.  The variant
	zero-tag policy says drop is safe (tag-0 destructor is a no-op
	on uninit paths) and necessary (live paths leak otherwise).  This
	is the 0.27.145 carrier shape, now lattice-driven via the
	authoring pass."""
	type_table = TypeTable()
	vty = _declare_destructible_variant(type_table)
	bool_ty = type_table.ensure_bool()
	func = _make_func(
		"cond_variant",
		params=["b"],
		locals_=["b", "v"],
		types={"b": bool_ty, "v": vty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_blk = M.BasicBlock(name="if_then")
	then_blk.instructions.append(M.ConstructVariant(dest="t_v", variant_ty=vty, ctor="None", args=[]))
	then_blk.instructions.append(M.StoreLocal(local="v", value="t_v"))
	then_blk.terminator = M.Goto(target="if_join")
	join_blk = M.BasicBlock(name="if_join")
	join_blk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("v", vty)]))
	join_blk.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_blk, "if_join": join_blk}
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 1, (
		"variant zero-tag widening regressed: PathDependent variant at "
		"function-exit join must emit (live path leaks otherwise)."
	)
	assert _has_drop_chain_for(func, "v")


# -- Carrier 5: PathDependent + non-variant → skip (tripwire) -----------


def test_authoring_skips_path_dependent_non_variant() -> None:
	"""Conditionally-initialized destructible STRUCT at the join:
	ledger says PathDependent.  The variant policy does NOT apply
	(structs have no tag-0 no-op destructor; their destructor reads
	field bytes and would crash on PHI-zero data).  Authoring skips,
	matching site 1 legacy behaviour."""
	type_table = TypeTable()
	sty = _make_droppable_struct(type_table, name="StructPathDep")
	bool_ty = type_table.ensure_bool()
	func = _make_func(
		"cond_struct",
		params=["b"],
		locals_=["b", "s"],
		types={"b": bool_ty, "s": sty},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.IfTerminator(cond="b", then_target="if_then", else_target="if_join")
	then_blk = M.BasicBlock(name="if_then")
	then_blk.instructions.append(M.StoreLocal(local="s", value="t_init"))
	then_blk.terminator = M.Goto(target="if_join")
	join_blk = M.BasicBlock(name="if_join")
	join_blk.instructions.append(M.CleanupHook(scope_id=0, candidates=[("s", sty)]))
	join_blk.terminator = M.Return(value=None)
	func.blocks = {"entry": entry, "if_then": then_blk, "if_join": join_blk}
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0
	assert not _has_drop_chain_for(func, "s")


# -- Carrier 6: multiple candidates in one hook -------------------------


def test_authoring_processes_multiple_candidates_independently() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func(
		"multi_cand",
		params=[],
		locals_=["live", "moved", "uninit"],
		types={"live": dty, "moved": dty, "uninit": dty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="live", value="t_init_live"))
	entry.instructions.append(M.StoreLocal(local="moved", value="t_init_moved"))
	entry.instructions.append(M.MoveOut(dest="t_user_move", local="moved", ty=dty))
	entry.instructions.append(M.StoreLocal(local="sink", value="t_user_move"))
	# uninit declared but never stored.
	entry.instructions.append(
		M.CleanupHook(
			scope_id=0,
			candidates=[("live", dty), ("moved", dty), ("uninit", dty)],
		)
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	func.locals.append("sink")
	func.local_types["sink"] = dty
	_attach_ledger(func)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 1
	assert _has_drop_chain_for(func, "live")
	# Only the user MoveOut for `moved` exists; no scope-drop pair.
	moveouts_for_moved = [
		ins for blk in func.blocks.values() for ins in blk.instructions
		if isinstance(ins, M.MoveOut) and ins.local == "moved"
	]
	assert len(moveouts_for_moved) == 1
	assert not _has_drop_chain_for(func, "uninit")


# -- Carrier 7: hook is removed after authoring -------------------------


def test_authoring_removes_cleanup_hook_instruction() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("hook_removed", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	author_cleanup(func, type_table=type_table)
	assert not _has_cleanup_hook(func), (
		"CleanupHook must be removed by authoring — downstream "
		"passes (drop_flags, string_arc) only understand canonical "
		"MoveOut/DropValue sequences, not the marker."
	)


# -- Carrier 8: no CleanupHook → no-op ----------------------------------


def test_authoring_noop_when_no_hook() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("no_hook", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	original_count = len(entry.instructions)
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0
	assert len(entry.instructions) == original_count


# -- Carrier 9: no ledger attached → no-op (hook stays) -----------------


def test_authoring_emits_observe_parity_record_per_candidate(capfd) -> None:
	"""Patch 2 observe parity: every per-candidate decision is
	emitted as a `[drift:ownership_ledger]` line when observe mode
	is on.  Granularity must match legacy site-1 records (one per
	local), reason tags must be from the legacy site-1 set
	(`needs_drop` / `not_drop_needing` / `moved_unconditional`) plus
	the `path_dependent_non_variant_skip` tripwire."""
	import os
	from lang.driftc import debug as drift_debug
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func(
		"parity_emit",
		params=[],
		locals_=["live", "uninit"],
		types={"live": dty, "uninit": dty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="live", value="t_init_live"))
	entry.instructions.append(
		M.CleanupHook(scope_id=0, candidates=[("live", dty), ("uninit", dty)])
	)
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	old_env = os.environ.get("DRIFT_COMPILER_DEBUG")
	os.environ["DRIFT_COMPILER_DEBUG"] = '{"ownership_ledger":true}'
	drift_debug._cached_flags = None
	try:
		author_cleanup(func, type_table=type_table)
	finally:
		drift_debug._cached_flags = None
		if old_env is None:
			os.environ.pop("DRIFT_COMPILER_DEBUG", None)
		else:
			os.environ["DRIFT_COMPILER_DEBUG"] = old_env
	captured = capfd.readouterr()
	# Two parity records expected (one per candidate).
	lines = [
		ln for ln in captured.err.splitlines()
		if ln.startswith("[drift:ownership_ledger]")
	]
	assert len(lines) == 2, (
		f"observe parity regressed: expected 2 per-candidate records "
		f"(one for `live`, one for `uninit`), got {len(lines)}: {lines}"
	)
	import json
	records = [json.loads(ln[len("[drift:ownership_ledger] "):]) for ln in lines]
	by_local = {r["local"]: r for r in records}
	# `live` → MUST_DROP / needs_drop, classification agree.
	assert by_local["live"]["site_verdict"] == "must_drop"
	assert by_local["live"]["site_reason"] == "needs_drop"
	assert by_local["live"]["classification"] == "agree"
	assert by_local["live"]["site"] == "scope_drop"
	# `uninit` → MUST_NOT_DROP / not_drop_needing (legacy site-1 set).
	assert by_local["uninit"]["site_verdict"] == "must_not_drop"
	assert by_local["uninit"]["site_reason"] == "not_drop_needing"
	assert by_local["uninit"]["classification"] == "agree"


def test_authoring_emits_no_observe_records_when_flag_off(capfd) -> None:
	"""Production builds (observe flag off) must emit zero
	`[drift:ownership_ledger]` lines from cleanup_authoring."""
	import os
	from lang.driftc import debug as drift_debug
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("no_observe", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	# Force observe flag OFF.
	old_env = os.environ.get("DRIFT_COMPILER_DEBUG")
	os.environ.pop("DRIFT_COMPILER_DEBUG", None)
	drift_debug._cached_flags = None
	try:
		author_cleanup(func, type_table=type_table)
	finally:
		drift_debug._cached_flags = None
		if old_env is not None:
			os.environ["DRIFT_COMPILER_DEBUG"] = old_env
	captured = capfd.readouterr()
	assert "[drift:ownership_ledger]" not in captured.err, (
		"cleanup_authoring leaked observe records into a non-observe "
		"build — telemetry must be gated on `drift_debug.enabled`."
	)


def test_authoring_temp_names_do_not_collide_with_existing_locals() -> None:
	"""K-found correctness pin (medium): MIR shares one string
	namespace across locals and SSA value-ids.  The authoring pass
	synthesises temps named `__cleanup_t<N>` for the `MoveOut` dest
	of each emitted drop — the prefix is not a guarantee against
	collision (user code could legally name a local
	`__cleanup_t1`).  `used_temps` must seed from
	`func.locals` / `func.local_types.keys()` / `func.params`, not
	just instruction dests."""
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	# Pre-populate `func.locals` with `__cleanup_t1` (the name the
	# authoring pass would otherwise pick first) to force the
	# collision shape.
	func = _make_func(
		"collision",
		params=[],
		locals_=["x", "__cleanup_t1"],
		types={"x": dty, "__cleanup_t1": dty},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	_attach_ledger(func)
	author_cleanup(func, type_table=type_table)
	# Find the synthesised MoveOut for `x` and verify its dest is
	# NOT `__cleanup_t1` (which would collide with the existing
	# local).
	moveout_dests_for_x = [
		ins.dest
		for blk in func.blocks.values()
		for ins in blk.instructions
		if isinstance(ins, M.MoveOut) and ins.local == "x"
	]
	assert len(moveout_dests_for_x) == 1, "expected one scope-drop MoveOut for x"
	assert moveout_dests_for_x[0] != "__cleanup_t1", (
		"authoring pass collided its synthesised temp name with an "
		"existing local — `used_temps` must include `func.locals` / "
		"`func.local_types.keys()` / `func.params`, not just instr "
		"dests."
	)


def test_authoring_noop_when_ledger_unset() -> None:
	type_table = TypeTable()
	dty = _make_droppable_struct(type_table)
	func = _make_func("no_ledger", params=[], locals_=["x"], types={"x": dty})
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="x", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("x", dty)]))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	# Deliberately do NOT attach the ledger.
	emitted = author_cleanup(func, type_table=type_table)
	assert emitted == 0
	assert _has_cleanup_hook(func), (
		"without an attached ledger the hook must remain — its "
		"presence will surface as an error in downstream passes, "
		"which is the intended fail-loud signal."
	)
