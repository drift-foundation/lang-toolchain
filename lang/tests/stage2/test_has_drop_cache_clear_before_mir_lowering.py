# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: stale _needs_drop_cache at MIR-lowering time must not cause
_param_drop_locals to omit droppable parameters.

Proven discriminator:
  - has_drop(ServerHandle) queried BEFORE destructor_fns has the generic
    Arc<AtomicBool> destroy → caches False
  - K39 registers Arc<AtomicBool>::destroy in destructor_fns
  - MIR lowering runs WITHOUT clearing the cache → _param_drop_locals
    misses 'handle' → _emit_scope_drops omits drop → post-pass injects
    __postdrop_handle → double-drop
  - Fix: clear _needs_drop_cache immediately before MIR lowering

This test exercises the exact boundary:
  1. Build a TypeTable with a struct whose has_drop depends on a transitive
     destructor (Arc field)
  2. Query has_drop BEFORE the destructor is installed → caches False
  3. Install the destructor (simulating K39)
  4. WITHOUT cache clear: HIRToMIR._param_drop_locals misses the param
  5. WITH cache clear: HIRToMIR._param_drop_locals includes the param
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.hir_to_mir import HIRToMIR, make_builder
from lang.driftc.stage1 import hir_nodes as H


def _make_table_with_destructor_and_stale_cache() -> tuple[TypeTable, int, int, int]:
	"""Build a TypeTable where has_drop(handle_tid) is stale-cached as False
	because the Arc destructor was registered AFTER the initial has_drop query.

	Simulates the real pipeline:
	  1. Arc struct and Handle struct fully declared with field types
	  2. has_drop(handle_tid) queried BEFORE Arc destructor is installed
	     → struct instance available, all fields non-droppable → caches False
	  3. K39 registers Arc destructor (destructor_fns[arc_tid] = ...)
	     via in-place dict mutation (no __setattr__, no cache clear)
	  4. handle_tid's cached False is now stale — Arc IS droppable

	Returns (table, arc_tid, handle_tid, int_tid).
	"""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc struct — fully declared, but NO destructor yet (pre-K39 state).
	# Mark Arc as non-Copy (Destructible types are never Copy).
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])
	# Mark Arc and Handle as non-Copy.  In the real pipeline the trait prover
	# returns False for Destructible types and structs containing them.
	_non_copy: set[int] = set()
	_non_copy.add(arc_tid)
	table._copy_query = lambda tid: False if tid in _non_copy else None  # type: ignore[attr-defined]

	# Handle struct with Arc field — fully declared, non-Copy (contains non-Copy Arc).
	handle_tid = table.declare_struct(module_id="mymod", name="Handle", field_names=["flag", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])
	_non_copy.add(handle_tid)

	# Query has_drop — struct instance IS available, but Arc has no destructor
	# yet, so has_drop recurses into fields and finds no droppable field.
	# Caches False at line 1484-1485 of types_core.py.
	assert table.has_drop(handle_tid) is False, \
		"precondition: has_drop must be False before Arc destructor is installed"
	assert table._needs_drop_cache.get(handle_tid) is False, \
		"precondition: False must be cached for handle_tid"

	# Simulate K39 adding Arc destructor via in-place dict mutation.
	# In the real pipeline, K39 does: destructor_fns[inst_id] = handle.fn_id
	# This mutates the dict in place — __setattr__ is NOT triggered, cache
	# is NOT cleared.
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	dfns = getattr(table, "destructor_fns", None)
	if dfns is None:
		dfns = {}
		table.destructor_fns = dfns  # triggers __setattr__, clears cache
		# Re-poison the cache after the clear.
		_ = table.has_drop(handle_tid)  # caches False again (Arc not yet in dfns)
	dfns[arc_tid] = destroy_fn  # in-place mutation — NO cache clear

	# Cache is stale: handle_tid cached as False even though Arc now has destructor.
	assert table._needs_drop_cache.get(handle_tid) is False, \
		"precondition: stale cache still has False for handle_tid"
	# But querying Arc directly would find the destructor.
	table._needs_drop_cache.pop(arc_tid, None)  # clear Arc's own cache to prove
	assert table.has_drop(arc_tid) is True, \
		"precondition: Arc itself IS droppable (destructor installed)"

	return table, arc_tid, handle_tid, int_tid


def _get_param_drop_locals(table: TypeTable, handle_tid: int, int_tid: int) -> list[str]:
	"""Create a minimal HIRToMIR and return its _param_drop_locals.

	This exercises the exact decision path at lines 316-326 of hir_to_mir.py.
	"""
	fn_id = FunctionId(module="mymod", name="serve", ordinal=0)
	builder = make_builder(fn_id)
	lower = HIRToMIR(
		builder,
		type_table=table,
		param_types={"handle": handle_tid, "count": int_tid},
		current_fn_id=fn_id,
	)
	return list(lower._param_drop_locals)


def test_stale_cache_omits_param_drop_locals() -> None:
	"""Without cache clear, _param_drop_locals misses the droppable parameter
	because has_drop returns stale False."""
	table, arc_tid, handle_tid, int_tid = _make_table_with_destructor_and_stale_cache()

	# Cache has stale False — _param_drop_locals will miss 'handle'.
	locals = _get_param_drop_locals(table, handle_tid, int_tid)
	assert "handle" not in locals, \
		"stale cache: handle should be MISSING from _param_drop_locals"


def test_cache_clear_restores_param_drop_locals() -> None:
	"""After cache clear, _param_drop_locals correctly includes the param."""
	table, arc_tid, handle_tid, int_tid = _make_table_with_destructor_and_stale_cache()

	# Clear cache (the fix) — has_drop now returns True.
	table._needs_drop_cache.clear()
	assert table.has_drop(handle_tid) is True, \
		"after cache clear: has_drop must return True (Arc field with destructor)"

	# _param_drop_locals includes 'handle'.
	locals = _get_param_drop_locals(table, handle_tid, int_tid)
	assert "handle" in locals, (
		f"after cache clear: handle must be in _param_drop_locals, "
		f"got {locals}"
	)


def test_scope_exit_drop_emitted_after_cache_clear() -> None:
	"""After cache clear, the cleanup-authoring path emits
	MoveOut+DropValue for the droppable parameter — the compiler no
	longer relies on post-pass repair.

	Patch 6c (2026-04-24) retired the legacy `_emit_scope_drops` helper
	this test originally exercised.  Rewritten against the production
	path: HIR→MIR emits `M.CleanupHook` via `_emit_scope_cleanup_hook`,
	then `cleanup_authoring.author_cleanup` queries `verdict_at` per
	candidate (against a fresh ledger build) and emits the canonical
	chain.
	"""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.cleanup_authoring import author_cleanup

	table, arc_tid, handle_tid, int_tid = _make_table_with_destructor_and_stale_cache()
	table._needs_drop_cache.clear()

	fn_id = FunctionId(module="mymod", name="serve", ordinal=0)
	builder = make_builder(fn_id)
	builder.func.params = ["handle", "count"]
	lower = HIRToMIR(
		builder,
		type_table=table,
		param_types={"handle": handle_tid, "count": int_tid},
		current_fn_id=fn_id,
	)

	# Simulate function body: push scope with params, emit cleanup
	# hook (covers `handle` via the param-drop registration), return.
	lower._push_scope(include_params=True)
	lower._emit_scope_cleanup_hook(scope_index=0)
	lower.b.set_terminator(M.Return(value=None))

	# Author the cleanup hook through the production pipeline.
	ledger = build_ledger(builder.func, drop_policy=lambda _t: None)
	setattr(builder.func, "_ownership_ledger", ledger)
	author_cleanup(builder.func, type_table=table)

	func = builder.func
	# Verify MoveOut + DropValue for handle was emitted.
	drops_for_handle = []
	for block in func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.MoveOut) and instr.local == "handle":
				drops_for_handle.append(("MoveOut", instr.dest))
			if isinstance(instr, M.DropValue) and instr.ty == handle_tid:
				drops_for_handle.append(("DropValue", instr.value))

	assert len(drops_for_handle) >= 2, (
		f"scope-exit must emit MoveOut+DropValue for handle after cache clear, "
		f"got {drops_for_handle}"
	)
	assert drops_for_handle[0][0] == "MoveOut", \
		"first instruction must be MoveOut"
	assert drops_for_handle[1][0] == "DropValue", \
		"second instruction must be DropValue"
	# The MoveOut dest must feed the DropValue value.
	assert drops_for_handle[0][1] == drops_for_handle[1][1], \
		"MoveOut dest must match DropValue value"
