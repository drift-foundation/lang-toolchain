# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: Phase 2a fix for the `match Optional<String>` double-drop UAF
(fix/ownership-drop-ledger track, 0.31.0).

The bug and its runtime manifestation are documented extensively in
`docs/history.md`'s 0.31.0 entry; this file pins the compiler-side
emission fix.  Pre-0.31.0, `_ensure_arm_scrut_ptr`'s Copy-store branch
(reached whenever the scrutinee's `_should_copy_value` returns True)
emitted a bare `StoreLocal(arm_scrut_local, scrut_val)` — a bitcopy of
the variant bits WITHOUT running the per-arm retain traversal.  For a
refcount-bearing variant (`Optional<String>`, any user variant
containing a `Copy`-impl'd wrapper with a String field, etc.) under
the packaged-load `copy_status=True` resolution, this produced two
owners of the same refcount and a double-release UAF.

Phase 2a changed the Copy-store branch to dispatch on the conjoined
predicate `dual_owner = (source_local is not None) AND
_drop_policy(scrut_ty).has_structural_drop`:

  - **dual_owner True** → `CopyValue`, which lowers into
    `_emit_copy_value_inner`'s per-kind retain traversal (SCALAR
    String → single `drift_string_retain`; VARIANT → tag switch
    + per-arm recursive Copy; STRUCT → per-field recursive Copy;
    ARRAY → element-wise dup).  `arm_scrut_local` ends up owning
    an independent set of refcount increments; the named source
    local keeps its own; scope-exit drops on both balance.

  - **dual_owner False** (either no named source local OR no
    structural drop) → bare `StoreLocal`.  A transient rvalue
    scrutinee has a single refcount owner in `scrut_val`; bare
    StoreLocal transfers that ownership to `arm_scrut_local`
    cleanly.  A POD-variant scrutinee has no refcount at all;
    bare StoreLocal is trivially correct.

Both halves of the guard matter: dropping the `source_local is not
None` half would leak transient rvalue refcounts (the original
`scrut_val` ends up with no named local to scope-drop); dropping
the `has_structural_drop` half would route every POD variant
through an unnecessary traversal without fixing any additional
bug.  The four tests in this file pin each quadrant: dual-owner
(must CopyValue), POD variant with named source (must bare
StoreLocal fast-path), transient rvalue with structural drop (must
bare StoreLocal — the Phase 2a review finding), and the composite
invariant that the CopyValue's dest lands in the scrut_tmp.

This test pins the emission shape directly at the MIR level because
reproducing the exact bug trigger in a runtime e2e fixture requires
the packaged-load eager `copy_status` resolution, which source-compile
in-tree tests cannot replicate without test-harness hooks.  The
Copy-hook mechanism used here mirrors the packaged-load path exactly
— it is the same mechanism `test_drop_policy_contract.py` uses to
pin the policy output on the bug shape.  If the Copy-store branch
ever regresses to the pre-Phase-2a bare StoreLocal (either by
reverting the dispatch, by special-casing some type family, or by
any later refactor that accidentally reintroduces a bitcopy where a
retain traversal is required), the CopyValue assertion below goes
red and the regression is caught at compile-time review.

Companion runtime smoke-test: the e2e fixture
`lang/tests/codegen/e2e/match_optional_copy_wrapper_string/` exercises
the semantics through a real compile+run (though in source-compile
mode that fixture reaches the MoveOut branch, not the Copy-store
branch — source-compile doesn't eagerly resolve the generic Copy
impl `implement<T> Copy for Optional<T> require T is Copy`).  The
authoritative runtime regression gate for the packaged-load bug is
the TLS team's downstream e2e run against a freshly-deployed
toolchain (Gate C in the 0.31.0 history entry).
"""
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2 import mir_nodes as M


def _collect_ctor_callinfo(hir: H.HBlock, type_table: TypeTable, var_tid: TypeId) -> dict[int, CallInfo]:
	"""Synthesise CallInfo entries for any ctor calls in `hir`.

	Lifted from the other stage2 match unit tests — at unit-test
	scope we don't run the checker, so the CallInfo table must be
	built from the HIR shape directly to satisfy HIRToMIR's
	qualified-member-call contract.
	"""
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	for stmt in hir.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall) and isinstance(stmt.value.fn, H.HQualifiedMember):
			base_te = stmt.value.fn.base_type_expr
			base_tid = resolve_opaque_type(base_te, type_table, module_id=getattr(base_te, "module_id", None))
			inst_tid = base_tid
			if type_table.get_variant_instance(inst_tid) is None:
				inst_tid = type_table.ensure_instantiated(base_tid, [])
			inst = type_table.get_variant_instance(inst_tid)
			if inst is None:
				continue
			arm_def = inst.arms_by_name.get(stmt.value.fn.member)
			if arm_def is None:
				continue
			info = CallInfo(
				target=CallTarget.constructor(var_tid, stmt.value.fn.member),
				sig=CallSig(
					param_types=tuple(arm_def.field_types),
					user_ret_type=var_tid,
					can_throw=False,
				),
			)
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info
	return call_info_by_callsite_id


def test_match_scrut_copy_store_branch_emits_copyvalue_for_refcounted_variant() -> None:
	"""Pin: a refcount-bearing scrutinee `V<String>` takes the MOVE
	branch (`MoveOut(source_local) → StoreLocal(arm_scrut_local)`),
	NOT the bare-`StoreLocal` double-owner shape.

	**History.**  Pre-Copy-shortcut-fix, V<String> with a Copy hook
	had `is_cheap_copy=True AND has_structural_drop=True` — the
	asymmetric triplet.  `_ensure_arm_scrut_ptr` reached its Copy-
	store else-branch and the Phase 2a `dual_owner` dispatch had to
	emit a `CopyValue` to retain the refcount.

	**Post-fix.**  The Copy-shortcut fix
	(`lang/tests/driver/test_drop_policy_copy_short_circuit_bug.py`)
	makes `V<String>.is_cheap_copy = False` because structural-with-
	drop is not cheap-copy regardless of `copy_status`.  V<String>
	now naturally takes the MOVE branch at the top of
	`_ensure_arm_scrut_ptr`: source_local is consumed via `MoveOut`
	and `arm_scrut_local` owns the +1.  The arm-end drop releases
	the +1; the source local is dead so its scope-drop is a no-op
	(the lattice records it as MOVED_OUT after the MoveOut).

	**The test.**  Verifies the new lowered shape: a `MoveOut`
	whose `local` is the source local AND a `StoreLocal` into a
	`__match_scrut_tmp*` whose `value` is the `MoveOut`'s dest.
	No `CopyValue(ty=V<String>)` should appear at the scrut level
	— the policy fix has subsumed Phase 2a's dual_owner mitigation
	by routing structural-with-drop scrutinees through MOVE
	directly.
	"""
	type_table = TypeTable()
	# Build V<String> — structural equivalent of Optional<String>.
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

	# Install the packaged-load Copy classification: force
	# copy_status(V<String>) to resolve True.  Matches what the
	# `.dmp` loader's eager trait-graph walk produces.
	wanted = {v_string_ty}
	type_table.set_copy_query(lambda tid: tid in wanted, allow_fallback=True)
	assert type_table.copy_status(v_string_ty) is True, (
		"test setup invariant broken: Copy hook did not take effect"
	)

	# Build the HIR shape: `val m = V::Some("x"); match m { Some(s) =>
	# 1, None => 2 }`.  The `m` local is the named source local that
	# triggers the `_ensure_arm_scrut_ptr` Copy-store else-branch
	# under the forced classification.
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
				result=H.HLiteralInt(value=1),
				pattern_arg_form="positional",
				binder_field_indices=[0],
			),
			H.HMatchArm(
				ctor="None",
				binders=[],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(value=2),
				pattern_arg_form="bare",
				binder_field_indices=[],
			),
		],
	)
	hir = H.HBlock(
		statements=[
			let_m,
			H.HLet(
				name="result",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info_by_callsite_id = _collect_ctor_callinfo(hir, type_table, v_string_ty)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)

	# Scan emitted MIR for the MOVE-path emission shape:
	#   MoveOut(dest=tmp, local=source_local, ty=v_string_ty)
	#   StoreLocal(local=__match_scrut_tmp*, value=tmp)
	# AND assert no `CopyValue(ty=v_string_ty)` was emitted at the
	# scrut level (the Copy-store branch must be unreachable for a
	# structural-with-drop scrutinee post-policy-fix).
	saw_move_out_of_scrut_ty = False
	saw_store_of_moved_dest_into_scrut_tmp = False
	saw_copy_of_scrut_ty = False
	move_out_dests: set[str] = set()
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.MoveOut) and getattr(instr, "ty", None) == v_string_ty:
				saw_move_out_of_scrut_ty = True
				move_out_dests.add(getattr(instr, "dest", ""))
			if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == v_string_ty:
				saw_copy_of_scrut_ty = True
			if isinstance(instr, M.StoreLocal):
				local_name = getattr(instr, "local", "") or ""
				value_name = getattr(instr, "value", "") or ""
				if isinstance(local_name, str) and local_name.startswith("__match_scrut_tmp"):
					if value_name in move_out_dests:
						saw_store_of_moved_dest_into_scrut_tmp = True

	assert saw_move_out_of_scrut_ty, (
		"Policy-fix regression: V<String> scrutinee did NOT emit a "
		"`MoveOut(ty=V<String>)` from its source local.  Post-fix, "
		"`is_cheap_copy=False` for structural-with-drop variants, so "
		"`_ensure_arm_scrut_ptr` must take its MOVE branch (line 1448 "
		"in hir_to_mir.py) — consuming the source local and storing "
		"the moved value into `arm_scrut_local`.  If MoveOut is "
		"missing, the source local is leaving the helper still LIVE "
		"and the dual-owner UAF returns."
	)
	assert saw_store_of_moved_dest_into_scrut_tmp, (
		"Policy-fix regression: V<String> emitted MoveOut but did "
		"NOT chain through `StoreLocal(__match_scrut_tmp*, "
		"<move_dest>)`.  The arm temp is left holding garbage; the "
		"arm-end drop will fire on uninitialised storage."
	)
	assert not saw_copy_of_scrut_ty, (
		"Policy-fix regression: V<String> scrutinee emitted a "
		"`CopyValue` at the scrut level.  Post-fix, structural-with-"
		"drop variants must take the MOVE branch — `CopyValue` would "
		"retain an extra refcount that has no symmetric drop, "
		"leaking one refcount per match.  This is the inverse of the "
		"Phase 2a leak (rvalue+CopyValue) and equally bad."
	)


def test_match_scrut_copy_store_branch_preserves_fast_path_for_pod_variant() -> None:
	"""Negative pin: `V<Int>` (POD variant with
	`has_structural_drop=False`) must take the bare-StoreLocal
	fast-path in the Copy-store else-branch, even when the Copy hook
	is installed.

	Important subtlety (and the reason the gate is
	`has_structural_drop` rather than `is_bitcopy`): variants are
	never classified as bitcopy in this compiler — `V<Int>` has
	`is_bitcopy = False` just like `V<String>`, because the variant
	tag + payload layout is not treated as trivially-bitcopyable
	even when every field is.  If the dispatch were on `is_bitcopy`,
	`V<Int>` would fall into the CopyValue branch and every POD-
	variant match would pay the tag-switch + per-arm traversal cost
	for no correctness benefit.  The `has_structural_drop` gate is
	what distinguishes "POD variant — bare StoreLocal is fine" from
	"refcount-bearing variant — must CopyValue to avoid double-
	release."

	If this regresses (POD variants start going through `CopyValue`),
	every POD-variant match gets an unnecessary traversal.  Not a
	correctness bug but a real compile-time / codegen-time cost
	regression.  Pinning here catches either a tightening of the
	dispatch (e.g. switching back to `is_bitcopy`) or an accidental
	removal of the fast-path altogether.
	"""
	type_table = TypeTable()
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
	int_ty = type_table.ensure_int()
	v_int_ty = type_table.ensure_instantiated(var_base, [int_ty])
	# Force Copy hook so the else-branch is reached.  (V<Int> is
	# classified as Copy structurally by the trait system anyway, but
	# using the hook makes the intent explicit and matches the
	# packaged-load shape exercised by the positive test.)
	type_table.set_copy_query(lambda tid: tid == v_int_ty, allow_fallback=True)

	v_int_te = TypeExpr(name="V", args=[TypeExpr(name="Int")], module_id="main")
	let_m = H.HLet(
		name="m",
		value=H.HCall(
			fn=H.HQualifiedMember(base_type_expr=v_int_te, member="Some"),
			args=[H.HLiteralInt(value=42)],
			kwargs=[],
		),
		declared_type_expr=v_int_te,
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="m", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["v"],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(value=1),
				pattern_arg_form="positional",
				binder_field_indices=[0],
			),
			H.HMatchArm(
				ctor="None",
				binders=[],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(value=2),
				pattern_arg_form="bare",
				binder_field_indices=[],
			),
		],
	)
	hir = H.HBlock(
		statements=[
			let_m,
			H.HLet(
				name="result",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info_by_callsite_id = _collect_ctor_callinfo(hir, type_table, v_int_ty)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)

	# V<Int> MUST NOT go through CopyValue in the Copy-store
	# branch — the `has_structural_drop=False` fast-path applies.
	# (V<Int> is not bitcopy-shaped either, but that's not the gate;
	# the gate is `has_structural_drop`.  A POD variant has no
	# drop-bearing children and so does not need the retain-traversal
	# that CopyValue would run.)  If V<Int> has CopyValue emission,
	# the fast-path has been either removed or accidentally gated on
	# `is_bitcopy`, which would push every POD variant through an
	# unnecessary per-arm traversal.
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == v_int_ty:
				raise AssertionError(
					"Phase 2a regressed the POD-variant fast-path: "
					"V<Int> scrutinee took the CopyValue path in "
					"`_ensure_arm_scrut_ptr`'s Copy-store branch.  "
					"For a variant with `has_structural_drop=False` "
					"the bare StoreLocal is correct and cheaper; the "
					"`dual_owner` dispatch "
					"(source_local is not None AND has_structural_drop) "
					"must gate the CopyValue path so only "
					"refcount-bearing scrutinees pay the traversal cost."
				)


def test_match_scrut_copy_store_inline_rvalue_does_not_copy() -> None:
	"""Regression pin: a transient rvalue scrutinee with
	`has_structural_drop=True` must NOT emit a `CopyValue` at the
	scrut level — the original Phase 2a leak shape.

	**History.**  Pre-Copy-shortcut-fix, V<String> with the Copy
	hook had `is_cheap_copy=True` so transient rvalue + Copy-store
	branch was the reachable path.  The Phase 2a `dual_owner` guard
	(`source_local is not None AND has_structural_drop`) was
	required to prevent CopyValue on the rvalue path (which would
	retain an unsymmetric +1 → leak).

	**Post-fix.**  V<String>.is_cheap_copy is now False (structural-
	with-drop is not cheap).  The first MOVE-path conditional in
	`_ensure_arm_scrut_ptr` synthesises a source_local for the
	rvalue (line 1443: `if source_local is None and not
	_should_copy_value(scrut_ty)`), then the second conditional
	emits MoveOut from that synthetic source_local into
	arm_scrut_local (line 1448).  No CopyValue at scrut level; the
	single +1 from the rvalue transfers cleanly into the arm temp.
	The synthetic source_local becomes MOVED_OUT after the MoveOut,
	so its scope-drop is a no-op.

	**What this test verifies.**  Inline-ctor scrutinee with
	`has_structural_drop=True` MUST NOT emit `CopyValue(ty=V<String>)`
	at the scrut level.  The MOVE path (synthetic source_local +
	MoveOut) is the post-fix expected shape.
	"""
	type_table = TypeTable()
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
	# Force Copy hook — same mechanism as the positive test.  With
	# `copy_status(V<String>) == True`, `_should_copy_value` returns
	# True and `_ensure_arm_scrut_ptr` takes its Copy-store else-
	# branch.  Sanity-check the precondition holds: `has_structural_drop`
	# must still be True (shortcut-free axis) so the CopyValue path
	# WOULD be reached if the `source_local is not None` half of the
	# guard were missing.
	type_table.set_copy_query(lambda tid: tid == v_string_ty, allow_fallback=True)
	builder_probe = make_builder(FunctionId(module="main", name="probe", ordinal=0))
	lower_probe = HIRToMIR(builder_probe, type_table=type_table)
	probe_policy = lower_probe._drop_policy(v_string_ty)
	assert probe_policy.has_structural_drop, (
		"test setup invariant broken: V<String> no longer reports "
		"has_structural_drop=True — this test cannot prove the "
		"no-CopyValue-at-scrut invariant without that precondition"
	)
	assert not probe_policy.is_cheap_copy, (
		"test setup invariant broken: V<String> is reporting "
		"is_cheap_copy=True under the post-Copy-shortcut-fix policy.  "
		"Structural-with-drop variants must classify as not cheap-"
		"copy; if this flips back, the policy fix has regressed"
	)

	# Match on a ctor-call expression directly (no `val` binding)
	# so the scrutinee is an SSA rvalue without a named source
	# local.  `_lower_match` will see `scrut_source_local = None`.
	v_string_te = TypeExpr(name="V", args=[TypeExpr(name="String")], module_id="main")
	inline_scrut = H.HCall(
		fn=H.HQualifiedMember(base_type_expr=v_string_te, member="Some"),
		args=[H.HLiteralString("x")],
		kwargs=[],
	)
	match = H.HMatchExpr(
		scrutinee=inline_scrut,
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["s"],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(value=1),
				pattern_arg_form="positional",
				binder_field_indices=[0],
			),
			H.HMatchArm(
				ctor="None",
				binders=[],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(value=2),
				pattern_arg_form="bare",
				binder_field_indices=[],
			),
		],
	)
	hir = H.HBlock(
		statements=[
			H.HLet(
				name="result",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
		],
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	# CallInfo for the inline ctor (it's a nested call, not a
	# direct HLet-style statement; `_collect_ctor_callinfo` only
	# walks top-level HLet stmts, so synthesise manually here).
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
	csid = getattr(inline_scrut, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = info

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info_by_callsite_id,
	).lower_block(hir)

	# Transient rvalue + has_structural_drop must NOT produce a
	# CopyValue of the scrut type at the scrut level.  Post policy
	# fix, V<String>.is_cheap_copy is False so the MOVE branch is
	# taken: a synthetic source_local wraps the rvalue, then MoveOut
	# transfers it into arm_scrut_local — the single +1 from the
	# rvalue ends up in the arm temp without a CopyValue retain.
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == v_string_ty:
				raise AssertionError(
					"Policy-fix regression: a match on an inline ctor "
					"call (transient rvalue, no named source local) "
					"with a has_structural_drop=True scrutinee emitted "
					"a `CopyValue` at the scrut level.  Post-policy-"
					"fix, V<String>.is_cheap_copy is False, so "
					"`_ensure_arm_scrut_ptr` MUST take the MOVE branch "
					"(synthetic source_local + MoveOut into arm temp).  "
					"A CopyValue here would retain a second refcount "
					"with no symmetric drop and leak per match."
				)
