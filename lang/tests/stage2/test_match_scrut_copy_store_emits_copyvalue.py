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
	"""Pin: the Copy-store branch emits `CopyValue` for a non-bitcopy
	refcount-bearing scrutinee, NOT the pre-Phase-2a bare `StoreLocal`.

	Setup: `V<String>` variant (Optional<String>-shaped), Copy hook
	installed to force `copy_status(V<String>) == True` at match-
	lowering time — mirroring the packaged-`.dmp`-load classification
	that caused the original TLS UAF.  The `_ensure_arm_scrut_ptr`
	helper then takes its Copy-store else-branch for the scrutinee.

	Expected emission (post-Phase-2a):
	  1. A `CopyValue` instruction whose `ty` is the scrutinee
	     variant type.  This triggers `_emit_copy_value_inner`'s
	     per-arm retain traversal at LLVM-codegen time, producing a
	     fully independent copy in `arm_scrut_local`.
	  2. A `StoreLocal` whose `value` is the `CopyValue`'s dest
	     (NOT the raw scrut SSA value), placing the owned copy into
	     the per-arm scrut_tmp.

	Pre-Phase-2a shape: `StoreLocal(arm_scrut_local, scrut_val)`
	with NO preceding CopyValue.  If the test regresses, the
	assertion on the CopyValue presence fires AND the message
	identifies the exact emission pattern that reintroduced the
	bug — so the triage path is "search for this pattern in the
	Copy-store branch."
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

	# Scan emitted MIR for a CopyValue whose `ty` is the scrutinee
	# variant type — that is the Phase 2a emission.  Also verify
	# that the immediately-following StoreLocal into a
	# `__match_scrut_tmp*` local uses the CopyValue's dest, not the
	# raw scrut SSA value — the Pre-Phase-2a bug shape stored the
	# raw SSA, which is what we are regressing against.
	saw_copy_of_scrut_ty = False
	saw_store_of_copy_dest_into_scrut_tmp = False
	copy_value_dests: set[str] = set()
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == v_string_ty:
				saw_copy_of_scrut_ty = True
				copy_value_dests.add(getattr(instr, "dest", ""))
			if isinstance(instr, M.StoreLocal):
				local_name = getattr(instr, "local", "") or ""
				value_name = getattr(instr, "value", "") or ""
				if isinstance(local_name, str) and local_name.startswith("__match_scrut_tmp"):
					if value_name in copy_value_dests:
						saw_store_of_copy_dest_into_scrut_tmp = True

	assert saw_copy_of_scrut_ty, (
		"Phase 2a regressed: the Copy-store branch of "
		"`_ensure_arm_scrut_ptr` did NOT emit a `CopyValue` for the "
		"refcount-bearing scrutinee `V<String>`.  Pre-Phase-2a, this "
		"branch emitted a bare `StoreLocal(arm_scrut_local, scrut_val)` "
		"which bitcopies the variant without running the per-arm "
		"retain traversal — both the source local and the arm_scrut_local "
		"then claim ownership of the same refcount, and scope-exit "
		"drops on both fire a double-release UAF (the TLS team's "
		"original bug).  Re-audit the else-branch in "
		"`_ensure_arm_scrut_ptr` — the `dual_owner` dispatch "
		"(source_local is not None AND has_structural_drop) must stay "
		"or the UAF returns."
	)
	assert saw_store_of_copy_dest_into_scrut_tmp, (
		"Phase 2a regressed: the Copy-store branch emitted a "
		"`CopyValue` but did NOT route its dest through the "
		"`StoreLocal(__match_scrut_tmp*, ...)`.  That means the owned "
		"copy isn't landing in the arm's scrut_tmp — the per-arm "
		"cleanup operates on garbage.  Re-audit the else-branch in "
		"`_ensure_arm_scrut_ptr`: the emission order must be "
		"`CopyValue(dest=copy_dest, value=scrut_val, ty=scrut_ty)` "
		"followed by `StoreLocal(local=arm_scrut_local, value=copy_dest)`."
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
	"""Regression pin for the Phase 2a review-found leak: a transient
	rvalue scrutinee with `has_structural_drop=True` must NOT go
	through `CopyValue`.

	**The leak.**  The initial Phase 2a dispatch was
	`if has_structural_drop: CopyValue else: bare StoreLocal`.  For
	a match like `match make_opt_string() { Some(s) => ..., None
	=> ... }` the scrutinee is a transient owning SSA value —
	`scrut_val` holds the function-call return with refcount 1 on
	the inner String, and there is NO named source local to
	scope-drop.  Under that initial dispatch, `CopyValue` retained
	the refcount (+1), `StoreLocal(arm_scrut_local, copy_dest)`
	stored the retained copy into the arm temp, the arm-end drop
	released once (refcount → 1), and the original `scrut_val` had
	no owner to scope-drop — leaking one refcount for the function
	lifetime.

	**The fix.**  The dispatch is guarded by
	`dual_owner = (source_local is not None) AND has_structural_drop`.
	For transient rvalues (source_local is None), bare
	`StoreLocal(arm_scrut_local, scrut_val)` transfers the single
	refcount owner into the arm temp; drops stay balanced.  The
	CopyValue path activates only when there's a named source local
	that ALSO needs to be dropped later — the true dual-owner case
	the TLS UAF created.

	**What this test verifies.**  Construct a match whose
	scrutinee is an inline ctor call (no `val` binding → no named
	source local), with a forced `has_structural_drop=True`
	classification.  Assert the emitted MIR does NOT contain a
	CopyValue of the scrutinee type.  If it does, the leak is back.
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
		"transient-rvalue fast-path without that precondition"
	)
	assert probe_policy.is_cheap_copy, (
		"test setup invariant broken: V<String> no longer takes "
		"the Copy-store else-branch under the Copy hook"
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
	# CopyValue of the scrut type.  If it does, the Phase 2a review
	# finding has regressed and the original refcount leaks for
	# every such match.
	for block in builder.func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.CopyValue) and getattr(instr, "ty", None) == v_string_ty:
				raise AssertionError(
					"Phase 2a regressed the transient-rvalue "
					"fast-path: a match on an inline ctor call "
					"(no named source local) with a "
					"has_structural_drop=True scrutinee emitted a "
					"`CopyValue`.  Pre-review Phase 2a had this same "
					"shape and leaked the original refcount (the "
					"rvalue `scrut_val` has no named local to "
					"scope-drop; CopyValue retains an extra refcount "
					"for the arm temp; only the arm temp drops).  "
					"The `dual_owner` guard in "
					"`_ensure_arm_scrut_ptr`'s Copy-store branch "
					"must require BOTH `source_local is not None` "
					"AND `has_structural_drop`.  Re-audit the guard."
				)
