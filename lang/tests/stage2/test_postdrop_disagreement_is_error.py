# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: has_drop() disagreement between lowering and post-pass must
surface as a compiler error through compile_stubbed_funcs, not as silent
__postdrop_* injection.

This exercises the full integrated path.  A stale _needs_drop_cache entry
causes has_drop to return False during lowering but True at post-pass time.
The compiler must:
  - produce an error diagnostic (not a warning)
  - NOT inject any __postdrop_* instructions
  - include param name, type, and drop status in the diagnostic
"""
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeTable
from lang.driftc.checker import FnSignature
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.stage1 import HBlock, HReturn, HLiteralInt
from lang.driftc.stage2 import mir_nodes as M


def test_disagreement_surfaces_as_error_through_compile_stubbed_funcs() -> None:
	"""Integrated: stale has_drop cache → lowering says no_drop → post-pass
	detects has_drop=True → compiler emits error diagnostic, no injection."""
	table = TypeTable()
	int_tid = table.ensure_int()

	# Arc struct with destructor — has_drop(arc) = True.
	arc_tid = table.declare_struct(module_id="std.concurrent", name="Arc", field_names=["inner"])
	table.define_struct_fields(arc_tid, field_types=[int_tid])
	destroy_fn = FunctionId(module="std.concurrent", name="Arc::destroy", ordinal=0)
	table.destructor_fns = {arc_tid: destroy_fn}

	# Mark Arc and Handle as non-Copy.
	_non_copy: set[int] = {arc_tid}
	table._copy_query = lambda tid: False if tid in _non_copy else None  # type: ignore[attr-defined]

	# Handle struct with Arc field.
	handle_tid = table.declare_struct(module_id="mymod", name="Handle", field_names=["flag", "value"])
	table.define_struct_fields(handle_tid, field_types=[arc_tid, int_tid])
	_non_copy.add(handle_tid)

	# Use a cache subclass that injects a stale False for handle_tid
	# immediately after the pre-lowering cache clear.  This simulates code
	# between K39 and lowering that queries has_drop with incomplete state.
	class _StaleCacheOnClear(dict):
		"""Dict that injects stale cache entry on the pre-lowering clear.

		compile_stubbed_funcs calls _needs_drop_cache.clear() multiple times.
		The pre-lowering clear (line 5725) is the one that matters — stale
		entries must survive past it into HIRToMIR.__init__.  We inject on
		the second clear (which is the pre-lowering one in this test shape).
		"""
		_clear_count: int = 0
		def clear(self) -> None:
			super().clear()
			self._clear_count += 1
			if self._clear_count == 2:
				# Inject stale False — simulates a has_drop query that
				# returned False between K39 and lowering.
				self[handle_tid] = False

	table._needs_drop_cache = _StaleCacheOnClear(table._needs_drop_cache)

	fn_id = FunctionId(module="mymod", name="serve", ordinal=0)
	sig = FnSignature(
		name="mymod::serve",
		param_names=["handle"],
		param_type_ids=[handle_tid],
		return_type_id=int_tid,
	)
	hir = HBlock(statements=[HReturn(value=HLiteralInt(value=0))])

	result = compile_stubbed_funcs(
		func_hirs={fn_id: hir},
		signatures={fn_id: sig},
		type_table=table,
		return_checked=True,
		entry_module="mymod",
		entry_name="serve",
	)
	mir_funcs, checked = result  # type: ignore[misc]

	# 1. An error diagnostic must have been emitted for the disagreement.
	postdrop_diags = [
		d for d in checked.diagnostics
		if getattr(d, "phase", None) == "postdrop"
	]
	assert len(postdrop_diags) >= 1, (
		f"expected at least 1 postdrop error diagnostic, got {len(postdrop_diags)}. "
		f"All diags: {[(d.severity, getattr(d, 'phase', '?'), d.message[:80]) for d in checked.diagnostics]}"
	)
	d = postdrop_diags[0]
	assert d.severity == "error", f"diagnostic must be error severity, got {d.severity}"
	assert "handle" in d.message, f"diagnostic must mention param name, got: {d.message}"
	assert "has_drop" in d.message or "no_drop" in d.message, \
		f"diagnostic must explain the disagreement, got: {d.message}"

	# 2. No __postdrop_* instructions injected anywhere.
	serve_func = mir_funcs.get(fn_id)
	if serve_func is not None:
		for block in serve_func.blocks.values():
			for instr in block.instructions:
				dest = getattr(instr, "dest", None)
				if dest is not None and "__postdrop" in str(dest):
					raise AssertionError(
						f"__postdrop instruction found in MIR: {instr}. "
						f"The post-pass must not silently inject drops."
					)

	# 3. param_drop_status recorded "no_drop" at lowering time
	#    (because the stale cache said has_drop=False).
	if serve_func is not None:
		assert serve_func.param_drop_status.get("handle") == "no_drop", (
			f"lowering should have recorded 'no_drop' due to stale cache, "
			f"got {serve_func.param_drop_status.get('handle')!r}"
		)
