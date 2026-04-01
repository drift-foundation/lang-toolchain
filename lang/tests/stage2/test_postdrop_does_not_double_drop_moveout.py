# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: param drop obligations use explicit param_drop_status instead
of MIR pattern scanning.

Tests that:
  - params with status "scope_exit_drop" are not flagged
  - params with status "forwarded_to_callee" are not flagged
  - params with status "no_drop" where has_drop()=True produce a diagnostic
  - the diagnostic contains full context (not silent injection)
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import _postdrop_check_param_drops, Diagnostic
from lang.driftc.stage2 import mir_nodes as M


def _make_type_table_with_droppable_struct() -> tuple[TypeTable, int]:
	"""Build a TypeTable with a struct that has_drop (via destructor_fns).

	Returns (table, handle_tid).
	"""
	table = TypeTable()
	int_tid = table.ensure_int()

	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# Mark Arc as non-Copy so _needs_runtime_drop returns True.
	_non_copy: set[int] = {arc_tid}
	table._copy_query = lambda tid: False if tid in _non_copy else None  # type: ignore[attr-defined]

	handle_tid = table.declare_struct(module_id="mymod", name="Handle", field_names=["flag", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])
	_non_copy.add(handle_tid)

	return table, handle_tid


def _make_func(handle_tid: int, status: str | None) -> M.MirFunc:
	"""Build a minimal MirFunc with one droppable param and given status."""
	fn_id = FunctionId(module="mymod", name="serve", ordinal=0)
	func = M.MirFunc(
		name="mymod::serve",
		params=["handle"],
		locals=["handle"],
		fn_id=fn_id,
		local_types={"handle": handle_tid},
	)
	if status is not None:
		func.param_drop_status["handle"] = status
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	return func


def _count_drop_values_for_type(func: M.MirFunc, ty: int) -> int:
	count = 0
	for block in func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.DropValue) and instr.ty == ty:
				count += 1
	return count


def test_scope_exit_drop_status_produces_no_diagnostic() -> None:
	"""Param with status 'scope_exit_drop' and has_drop=True → no diagnostic."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func(handle_tid, "scope_exit_drop")
	diags: list[Diagnostic] = []

	_postdrop_check_param_drops(func, table, diagnostics=diags)

	assert len(diags) == 0, f"should produce no diagnostic, got {len(diags)}"
	assert _count_drop_values_for_type(func, handle_tid) == 0, \
		"should not inject any drops"


def test_forwarded_to_callee_produces_no_diagnostic() -> None:
	"""Param with status 'forwarded_to_callee' → no diagnostic."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func(handle_tid, "forwarded_to_callee")
	diags: list[Diagnostic] = []

	_postdrop_check_param_drops(func, table, diagnostics=diags)

	assert len(diags) == 0
	assert _count_drop_values_for_type(func, handle_tid) == 0


def test_no_drop_with_has_drop_true_produces_diagnostic() -> None:
	"""Param with status 'no_drop' but has_drop()=True → diagnostic emitted.

	This is the exact disagreement that caused the 0.27.132–0.27.135 bugs:
	lowering decided no drop needed, but post-pass sees has_drop=True.
	The new behavior emits a diagnostic instead of silently injecting drops.
	"""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func(handle_tid, "no_drop")
	diags: list[Diagnostic] = []

	_postdrop_check_param_drops(func, table, diagnostics=diags)

	assert len(diags) == 1, f"expected 1 diagnostic, got {len(diags)}"
	d = diags[0]
	assert d.severity == "error"
	assert "handle" in d.message
	assert "no_drop" in d.message
	assert "has_drop" in d.message
	# No silent injection.
	assert _count_drop_values_for_type(func, handle_tid) == 0, \
		"must NOT silently inject drops — diagnostic only"


def test_missing_status_with_has_drop_true_produces_diagnostic() -> None:
	"""Param with no recorded status but has_drop()=True → diagnostic."""
	table, handle_tid = _make_type_table_with_droppable_struct()
	func = _make_func(handle_tid, None)  # no status recorded
	diags: list[Diagnostic] = []

	_postdrop_check_param_drops(func, table, diagnostics=diags)

	assert len(diags) == 1
	assert "not recorded" in diags[0].message


def test_no_drop_with_has_drop_false_produces_no_diagnostic() -> None:
	"""Param with status 'no_drop' and has_drop()=False → no diagnostic."""
	table = TypeTable()
	int_tid = table.ensure_int()
	fn_id = FunctionId(module="mymod", name="add", ordinal=0)
	func = M.MirFunc(
		name="mymod::add",
		params=["x"],
		locals=["x"],
		fn_id=fn_id,
		local_types={"x": int_tid},
	)
	func.param_drop_status["x"] = "no_drop"
	entry = M.BasicBlock(name="entry")
	entry.terminator = M.Return(value=None)
	func.blocks["entry"] = entry
	diags: list[Diagnostic] = []

	_postdrop_check_param_drops(func, table, diagnostics=diags)

	assert len(diags) == 0
