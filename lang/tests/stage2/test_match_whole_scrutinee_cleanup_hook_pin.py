# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Behavior-level pin: whole-scrutinee match cleanup is authored through
the production `M.CleanupHook` path, not via inline `MoveOut + DropValue`
in `_visit_expr_HMatchExpr`.

**Why this pin exists.**  The whole-scrutinee migration (post-Copy-
shortcut-fix) replaced the legacy inline emission in the
`elif arm_scrut_local is not None:` branch with a single-candidate
`M.CleanupHook(scope_id=N, candidates=[(arm_scrut_local, scrut_ty)])`.
`cleanup_authoring` (`lang/driftc/stage2/cleanup_authoring.py`) then
queries `verdict_at` and emits the canonical `MoveOut + DropValue`
chain only when the lattice says `MUST_DROP`.

The previous attempt at this migration was reverted because of a
66-byte leak under signed-package stdlib — root-caused to the
`compute_drop_policy.needs_drop` short-circuit on `copy_status=True`,
which cleanup_authoring consults via `verdict_at`'s `needs_drop`
parameter.  Fixing the policy on the destruction axis (commit
`cba71410`, 0.31.11) unblocked the migration.

**What the pin verifies.**  At HIR→MIR lowering time (no driver-
side ledger, no cleanup_authoring), a `match V<String> { Some(_)
=> ..., None => ... }` shape produces:

  1. A `M.CleanupHook` whose `candidates` list contains exactly
     one tuple `(arm_scrut_local, scrut_ty)` for the no-payload-
     move arm.  This is the production hook the migration emits.
  2. NO inline `MoveOut(local=__match_scrut_tmp*) + DropValue(...)`
     pair from the legacy emission.  If the inline emission ever
     comes back (e.g. via a partial revert), this assertion fires.

The companion `test_match_cleanup_authoring.py` tests prove
cleanup_authoring correctly expands `M.CleanupHook` into
`MoveOut + DropValue` when the verdict is `MUST_DROP`.  Together,
the two pins close the loop: the production lowering path emits
the hook, and authoring expands it correctly.

This is a behavior-level pin (HIR shape → MIR shape) rather than a
helper-unit pin: the deleted `_match_scrutinee_drop_verdict` helper
was the helper-unit anchor, but its semantics are now subsumed by
the `verdict_at` query inside `cleanup_authoring`, so the meaningful
boundary to pin is the hook emission itself.
"""
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2 import mir_nodes as M


def _build_v_string_match_hir(type_table: TypeTable):
	"""Construct the HIR for the statement-context shape:

	    val m = V::Some("x");
	    match m {
	        Some(s) => { },   // s is non-Copy → MOVE branch (per-field cleanup)
	        None    => { },   // bare arm → elif branch (whole-scrutinee)
	    }

	**Why statement context (not `val r = match ...`):** in value-
	producing context `_ensure_arm_scrut_ptr` is only called when
	`need_addr_binders` is True (some binder is non-Copy), so the
	None arm — which has no binders — never gets `arm_scrut_local`
	set and the elif branch never fires.  Statement-context match
	calls `_ensure_arm_scrut_ptr(mark_source_moved=True)` once per
	arm UNCONDITIONALLY (line 1546 in hir_to_mir.py), so the bare
	None arm's `arm_scrut_local` is set, no binder loop runs (no
	binders), `arm_scrut_payload_moved` stays False, and the elif
	branch is the production path.

	This is the same shape every real `match opt { Some(s) => ...;
	None => ... }` statement-context match uses in the stdlib /
	user code.
	"""
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	string_ty = type_table.ensure_string()
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])

	v_string_te = TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main")
	let_m = H.HLet(
		name="m",
		value=H.HCall(
			fn=H.HQualifiedMember(base_type_expr=v_string_te, member="Some"),
			args=[H.HLiteralString("x")],
			kwargs=[],
		),
		declared_type_expr=v_string_te,
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="m", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["s"],
				block=H.HBlock(statements=[]),
				result=None,
				pattern_arg_form="positional",
				binder_field_indices=[0],
			),
			H.HMatchArm(
				ctor="None",
				binders=[],
				block=H.HBlock(statements=[]),
				result=None,
				pattern_arg_form="bare",
				binder_field_indices=[],
			),
		],
	)
	hir = H.HBlock(
		statements=[
			let_m,
			H.HExprStmt(expr=match),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)

	# Synthesise CallInfo for the `V::Some("x")` ctor — at unit scope
	# we don't run the checker, so HIRToMIR needs the resolved CallInfo
	# directly.
	inst = type_table.get_variant_instance(v_string_ty)
	assert inst is not None
	arm_def = inst.arms_by_name["Some"]
	info = CallInfo(
		target=CallTarget.constructor(v_string_ty, "Some"),
		sig=CallSig(
			param_types=tuple(arm_def.field_types),
			user_ret_type=v_string_ty,
			can_throw=False,
		),
	)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	csid = getattr(let_m.value, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = info
	return hir, v_string_ty, call_info_by_callsite_id


def test_whole_scrutinee_emits_cleanup_hook_with_arm_scrut_local() -> None:
	"""Production HIR→MIR emits a `M.CleanupHook` whose candidates
	include `(arm_scrut_local, scrut_ty)` for the no-payload-move arm
	of a refcount-bearing variant match.

	Pre-migration shape (before commit-after-`cba71410`): the
	`elif arm_scrut_local is not None:` branch emitted inline
	`MoveOut(arm_scrut_local) + DropValue(scrut_ty)` plus a HIR-side
	`_record_drop_decision(SITE_MATCH_CLEANUP, ...)` telemetry call,
	with the verdict computed by the now-deleted
	`_match_scrutinee_drop_verdict` helper.

	Post-migration: a single `M.CleanupHook` whose candidate list is
	`[(arm_scrut_local, scrut_ty)]`.  Authority shifts to
	`cleanup_authoring`, which queries `verdict_at` per candidate
	and emits the drop chain only when the lattice says `MUST_DROP`.

	If this assertion fires, the production whole-scrutinee path has
	regressed off the CleanupHook authority surface (either back to
	inline emission, or onto a non-CleanupHook author).
	"""
	type_table = TypeTable()
	hir, v_string_ty, call_info_by_callsite_id = _build_v_string_match_hir(type_table)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)

	# Find any M.CleanupHook whose candidates list mentions a
	# __match_scrut_tmp* local with the variant scrut_ty.
	saw_hook_for_arm_scrut = False
	hook_candidates_seen: list[tuple[str, int]] = []
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if not isinstance(instr, M.CleanupHook):
				continue
			for local, ty in instr.candidates:
				hook_candidates_seen.append((local, ty))
				if (
					isinstance(local, str)
					and local.startswith("__match_scrut_tmp")
					and ty == v_string_ty
				):
					saw_hook_for_arm_scrut = True

	assert saw_hook_for_arm_scrut, (
		f"Whole-scrutinee migration regressed: no M.CleanupHook "
		f"emitted with `(arm_scrut_local, scrut_ty=V<String>)` as a "
		f"candidate.  All CleanupHook candidates seen: "
		f"{hook_candidates_seen!r}.  The production path in "
		f"`_visit_expr_HMatchExpr`'s `elif arm_scrut_local is not "
		f"None:` branch must emit a single-candidate CleanupHook so "
		f"`cleanup_authoring` decides emission via `verdict_at` + "
		f"`compute_drop_policy.needs_drop`."
	)


def test_whole_scrutinee_does_not_emit_inline_moveout_dropvalue() -> None:
	"""The legacy inline `MoveOut(arm_scrut_local) + DropValue(...)`
	pair must NOT appear in HIR→MIR output for the no-payload-move arm.

	`cleanup_authoring` is the only site allowed to materialise a drop
	for `arm_scrut_local`.  If HIR→MIR also emits one, we get two
	drops — exactly the over-drop class the policy fix was meant to
	eliminate at the source level.

	The assertion looks for the specific shape:
	    MoveOut(dest=T, local=__match_scrut_tmp*, ty=V<String>)
	    DropValue(value=T, ty=V<String>)
	in the same block, indicating an inline emission survived.
	"""
	type_table = TypeTable()
	hir, v_string_ty, call_info_by_callsite_id = _build_v_string_match_hir(type_table)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)

	for block in builder.func.blocks.values():
		instrs = list(block.instructions)
		for i, ins in enumerate(instrs):
			if not isinstance(ins, M.MoveOut):
				continue
			local = getattr(ins, "local", "") or ""
			if not (isinstance(local, str) and local.startswith("__match_scrut_tmp")):
				continue
			if getattr(ins, "ty", None) != v_string_ty:
				continue
			# Found a MoveOut of the scrut tmp at the variant ty.
			# Look for an immediately-following DropValue on the same
			# dest — the inline emission shape.
			move_dest = getattr(ins, "dest", None)
			for j in range(i + 1, len(instrs)):
				nxt = instrs[j]
				if isinstance(nxt, M.DropValue):
					if getattr(nxt, "value", None) == move_dest and getattr(nxt, "ty", None) == v_string_ty:
						raise AssertionError(
							f"Whole-scrutinee migration regressed: HIR→MIR "
							f"emitted the legacy inline shape "
							f"`MoveOut(local={local!r}, ty=V<String>) + "
							f"DropValue(value={move_dest!r}, ty=V<String>)` "
							f"in block {block.name!r} at index {i}-{j}.  "
							f"Post-migration the elif branch must emit only "
							f"a `M.CleanupHook` and let `cleanup_authoring` "
							f"author the drop.  See "
							f"`lang/driftc/stage2/hir_to_mir.py` "
							f"`_visit_expr_HMatchExpr`."
						)
				# Stop scanning past a terminator-like or block-bound
				# point; legacy emission was always immediately adjacent.
				if isinstance(nxt, (M.MoveOut, M.CleanupHook)):
					break
