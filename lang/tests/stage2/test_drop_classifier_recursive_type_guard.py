# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Stage-2 defense-in-depth: the ownership-normalization classifier
(`DropClassifier.type_needs_drop`) must not blow the Python stack on a
malformed recursive value-type TypeTable.

Background: the normal CLI/package recursive-value validator
(`validate_no_recursive_value_types`) rejects directly-recursive value types
before stage 2. This test deliberately bypasses that front-end gate by
hand-constructing a malformed TypeTable whose variant instance has a by-value
self-loop arm (as malformed/legacy package metadata could), then runs the
stage-2 normalization pass directly. The pass must terminate (the in-progress cycle
guard in `_type_needs_drop`) instead of raising RecursionError, and must still
classify the type as droppable when a `String` participates in the cycle.
"""
from __future__ import annotations

from lang.driftc.checker import FnInfo  # noqa: F401  (kept for parity / fn_infos typing)
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import (
	TypeTable,
	VariantArmSchema,
	VariantInstance,
	VariantArmInstance,
)
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.ownership_normalization import normalize_ownership_mir


def _malformed_recursive_variant(type_table: TypeTable, *, with_string_arm: bool) -> int:
	"""Hand-build a VARIANT whose instance has a BY-VALUE self-loop arm, bypassing
	`_define_variant_instance` (which would itself recurse via has_drop). Returns
	the variant's base TypeId.

	Arms:
	  - (optional) `Str(s: String)` — a droppable leaf in the cycle.
	  - `Rec(inner: Self)` — the malformed by-value self-reference.
	"""
	string_ty = type_table.ensure_string()
	# Declare with payload-free schema arms so the auto-built instance does not
	# itself form a cycle (no recursion at declare time); we overwrite it below.
	base = type_table.declare_variant(
		module_id="test",
		name="Rec",
		type_params=[],
		arms=[VariantArmSchema(name="Str", fields=[]), VariantArmSchema(name="Rec", fields=[])],
	)
	arms: list[VariantArmInstance] = []
	if with_string_arm:
		arms.append(VariantArmInstance(tag=0, name="Str", field_names=["s"], field_types=[string_ty]))
	# The malformed by-value self-edge: the `Rec` arm carries the variant itself.
	arms.append(VariantArmInstance(tag=len(arms), name="Rec", field_names=["inner"], field_types=[base]))
	malformed = VariantInstance(
		base_id=base,
		type_args=[],
		arms=arms,
		arms_by_name={a.name: a for a in arms},
	)
	# Direct insertion bypasses the normal instantiation path's has_drop walk.
	type_table.variant_instances[base] = malformed
	return base


def _func_holding(type_table: TypeTable, tid: int) -> M.MirFunc:
	"""A minimal MIR func with one local of type `tid`, listed as a scope-exit
	cleanup candidate so the normalization pass consults its drop classification."""
	fn_id = FunctionId(module="test", name="recdrop", ordinal=0)
	func = M.MirFunc(
		name="test::recdrop",
		params=[],
		locals=["r"],
		fn_id=fn_id,
		local_types={"r": tid},
	)
	entry = M.BasicBlock(name="entry")
	entry.instructions.append(M.StoreLocal(local="r", value="t_init"))
	entry.instructions.append(M.CleanupHook(scope_id=0, candidates=[("r", tid)]))
	entry.terminator = M.Return(value=None)
	func.blocks = {"entry": entry}
	return func


def _arc_pipeline(func: M.MirFunc, tt: TypeTable) -> M.MirFunc:
	"""B2+C S5 pipeline: attach a fresh ledger, build the frozen plan, run
	string_arc (no longer the site-3 drop emitter), then the unified Return
	authority (`return_cleanup_emitter`) which authors the destructible drop
	tail.  Used where the pin asserts the emitted drop shape."""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.destructible_planner import build_destructible_plan
	from lang.driftc.stage2.return_cleanup_emitter import emit_return_cleanups
	setattr(func, "_ownership_ledger", build_ledger(func, drop_policy=lambda _t: None))
	plan, _census, _c1 = build_destructible_plan(func, type_table=tt)
	normalize_ownership_mir(func, type_table=tt, fn_infos={})
	emit_return_cleanups(func, plan)
	return func


def test_classifier_does_not_recurse_on_self_looping_variant() -> None:
	"""Primary defense-in-depth contract: the pass terminates (no RecursionError)
	on a by-value self-looping variant."""
	tt = TypeTable()
	base = _malformed_recursive_variant(tt, with_string_arm=True)
	func = _func_holding(tt, base)
	# Must not raise RecursionError — the in-progress cycle guard breaks the edge.
	result = normalize_ownership_mir(func, type_table=tt, fn_infos={})
	assert result is not None


def test_classifier_string_in_cycle_remains_droppable() -> None:
	"""The guard must return the correct least-fixpoint: a cycle that contains a
	`String` is still classified droppable (a drop is emitted for the local),
	while a pure self-loop with no droppable leaf is not."""
	# With a String arm -> droppable -> the pipeline emits a drop for `r`.
	tt_drop = TypeTable()
	base_drop = _malformed_recursive_variant(tt_drop, with_string_arm=True)
	func_drop = _arc_pipeline(_func_holding(tt_drop, base_drop), tt_drop)
	assert _emits_drop_for(func_drop, "r"), "String-in-cycle variant must be droppable"

	# Pure self-loop, no droppable leaf -> not droppable -> no drop emitted.
	tt_nodrop = TypeTable()
	base_nodrop = _malformed_recursive_variant(tt_nodrop, with_string_arm=False)
	func_nodrop = _arc_pipeline(_func_holding(tt_nodrop, base_nodrop), tt_nodrop)
	assert not _emits_drop_for(func_nodrop, "r"), "pure self-loop has no droppable leaf"


def _emits_drop_for(func: M.MirFunc, local: str) -> bool:
	"""True if the post-pipeline func contains a DropValue whose value traces to
	a MoveOut/LoadLocal of `local` (the historical drop emission shape)."""
	# Collect temps loaded/moved from `local`, then look for a DropValue on them.
	traced: set[str] = set()
	drops: set[str] = set()
	for blk in func.blocks.values():
		for ins in blk.instructions:
			src = getattr(ins, "local", None)
			dest = getattr(ins, "dest", None)
			if src == local and dest is not None and ins.__class__.__name__ in ("MoveOut", "LoadLocal"):
				traced.add(dest)
			if ins.__class__.__name__ == "DropValue":
				v = getattr(ins, "value", None) or getattr(ins, "local", None)
				if v is not None:
					drops.add(v)
	return bool(traced & drops) or (local in drops)
