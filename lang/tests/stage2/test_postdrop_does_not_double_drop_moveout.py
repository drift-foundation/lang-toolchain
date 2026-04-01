# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: post-pass drop injection must not re-drop parameters already
dropped by _emit_scope_drops via MoveOut.

Proven discriminator:
  - _emit_scope_drops emits MoveOut(dest=tmp, local=param) + DropValue(tmp)
  - the post-pass existing-drop check only looked for LoadLocal, not MoveOut
  - when external package loading changes post-MIR has_drop() results,
    has_drop returns True for a type that was already correctly dropped
  - the post-pass injects a duplicate LoadLocal + DropValue → double-drop

The double-drop causes Arc refcount corruption:
  - refcount decremented twice (2 → 0) instead of once (2 → 1)
  - freed memory accessed by the caller → heap-use-after-free

This test constructs a MirFunc with a MoveOut-based drop (as _emit_scope_drops
produces) and verifies that the production post-pass does NOT inject a
duplicate drop.
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import _postdrop_inject_missing_param_drops
from lang.driftc.stage2 import mir_nodes as M


def _make_type_table_with_droppable_struct() -> tuple[TypeTable, int]:
	"""Build a TypeTable with a struct that has_drop (via destructor_fns).

	Returns (table, handle_tid).
	"""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc-like struct with destructor.
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# Handle struct with Arc field — needs drop transitively.
	handle_tid = table.declare_struct(module_id="mymod", name="Handle", field_names=["flag", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])

	return table, handle_tid


def _make_func_with_moveout_drop(handle_tid: int) -> M.MirFunc:
	"""Build a MirFunc that mirrors _emit_scope_drops output:
	a parameter 'handle' is dropped via MoveOut + DropValue before Return.
	"""
	fn_id = FunctionId(module="mymod", name="serve_impl", ordinal=0)
	func = M.MirFunc(
		name="mymod::serve_impl",
		params=["handle"],
		locals=["handle", "__moveout_0"],
		fn_id=fn_id,
		local_types={"handle": handle_tid, "__moveout_0": handle_tid},
	)

	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.MoveOut(dest="__moveout_0", local="handle", ty=handle_tid))
	entry.instructions.append(M.DropValue(value="__moveout_0", ty=handle_tid))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry

	return func


def _make_func_without_drop(handle_tid: int) -> M.MirFunc:
	"""Build a MirFunc with no drop instructions for the parameter.

	Simulates the case where _emit_scope_drops missed this parameter because
	has_drop was False during initial MIR lowering.
	"""
	fn_id = FunctionId(module="mymod", name="serve_impl", ordinal=0)
	func = M.MirFunc(
		name="mymod::serve_impl",
		params=["handle"],
		locals=["handle"],
		fn_id=fn_id,
		local_types={"handle": handle_tid},
	)
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func


def _make_func_with_loadlocal_drop(handle_tid: int) -> M.MirFunc:
	"""Build a MirFunc with a LoadLocal-based drop (as prior post-pass would emit)."""
	fn_id = FunctionId(module="mymod", name="serve_impl", ordinal=0)
	func = M.MirFunc(
		name="mymod::serve_impl",
		params=["handle"],
		locals=["handle", "__load_0"],
		fn_id=fn_id,
		local_types={"handle": handle_tid, "__load_0": handle_tid},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.LoadLocal(dest="__load_0", local="handle"))
	entry.instructions.append(M.DropValue(value="__load_0", ty=handle_tid))
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func


def _count_drop_values_for_type(func: M.MirFunc, ty: int) -> int:
	"""Count all DropValue instructions targeting a specific type."""
	count = 0
	for block in func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.DropValue) and instr.ty == ty:
				count += 1
	return count


def _run_postdrop_production(func: M.MirFunc, table: TypeTable) -> None:
	"""Run the production post-pass from driftc.py."""
	table._needs_drop_cache.clear()
	_postdrop_inject_missing_param_drops(func, table)


def test_postdrop_does_not_duplicate_moveout_drop() -> None:
	"""Production post-pass must detect MoveOut-based drops and NOT inject a duplicate."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func_with_moveout_drop(handle_tid)

	assert _count_drop_values_for_type(func, handle_tid) == 1, \
		"precondition: exactly one scope-exit DropValue before post-pass"
	assert table.has_drop(handle_tid), "Handle with Arc field must need drop"

	_run_postdrop_production(func, table)

	drop_count = _count_drop_values_for_type(func, handle_tid)
	assert drop_count == 1, (
		f"post-pass injected a duplicate drop: found {drop_count} DropValues "
		f"for handle type (expected 1). The post-pass must detect MoveOut-based "
		f"drops, not just LoadLocal-based drops."
	)


def test_postdrop_injects_when_no_existing_drop() -> None:
	"""Production post-pass must inject drops when no existing drop is present."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func_without_drop(handle_tid)

	assert _count_drop_values_for_type(func, handle_tid) == 0

	_run_postdrop_production(func, table)

	drop_count = _count_drop_values_for_type(func, handle_tid)
	assert drop_count == 1, (
		f"post-pass must inject a drop when no existing drop is present, "
		f"found {drop_count} DropValues (expected 1)"
	)


def test_postdrop_detects_loadlocal_based_drop() -> None:
	"""Production post-pass must also detect LoadLocal-based drops."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func_with_loadlocal_drop(handle_tid)

	assert _count_drop_values_for_type(func, handle_tid) == 1

	_run_postdrop_production(func, table)

	drop_count = _count_drop_values_for_type(func, handle_tid)
	assert drop_count == 1, (
		f"post-pass should not duplicate LoadLocal-based drops either, "
		f"found {drop_count} DropValues (expected 1)"
	)
