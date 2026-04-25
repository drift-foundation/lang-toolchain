# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Site-2 candidate-set authority pins (post-walker-fix migration).

After the per-field walker became chain-aware (`MoveFromRef` +
`_apply_field_state`'s tightened MovedOut detection, 0.31.12), HIR's
in-loop pre-filters on the per-field cleanup candidate set became
structurally redundant:

  - Filter A (`cleanup_fidx in moved_field_indices`) — the walker
    already marks moved fields MOVED_OUT, so authoring's
    `field_verdict_at` returns MUST_NOT_DROP without HIR's help.
  - Filter B (`not _needs_runtime_drop(cleanup_fty)`) — pure pruning;
    `compute_drop_policy(field_ty).needs_drop=False` collapses the
    verdict to MUST_NOT_DROP regardless of state, so authoring
    correctly skips POD fields too.

The candidate-set migration retires both filters and the
`moved_field_indices` set: HIR proposes the FULL `arm_def.field_types`
list as candidates; `match_cleanup_authoring` + the ledger decide
emit-vs-skip per candidate.

These two pins make the new authority boundary visible at unit scope:

  1. **Full field set in the hook** — `MatchCleanupHook.candidates`
     contains one entry for EVERY ctor field (including fields the
     binder loop moved out and fields whose type doesn't need drop),
     not the pre-migration filtered subset.

  2. **Non-drop candidate skipped at authoring** — when a POD
     candidate (e.g. `Int`) is present in the hook, authoring
     queries `field_verdict_at` and emits NO chain for it (verdict
     is MUST_NOT_DROP via the `needs_drop=False` collapse), but the
     hook is still removed and other candidates are still authored
     correctly.

If either pin fails, the candidate-set migration has regressed —
either HIR re-introduced a filter, or authoring stopped treating the
ledger as the sole emit-vs-skip authority.
"""
from __future__ import annotations

from typing import List

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2 import mir_nodes as M
from lang.driftc.stage2.match_cleanup_authoring import author_match_cleanup
from lang.driftc.stage2.ownership_ledger import build_ledger


def _build_two_field_string_string_match_hir(type_table: TypeTable):
	"""Construct the HIR for a statement-context match on a two-field
	String/String variant `Pair(a: String, b: String)`:

	    val a0: String = "a";
	    val a1: String = "b";
	    val p: Pair = Pair::Pair(a = a0, b = a1);
	    match p {
	        Pair(x, _y) => { ... }
	    }

	The Some-equivalent arm binds `x` (real binder) and `_y` (discard),
	both String.  In statement-context match (per
	`_visit_expr_HMatchExpr`, line 1546), `_ensure_arm_scrut_ptr`
	fires unconditionally and arm_scrut_local is set.  Without a Copy
	hook installed, `_should_copy_value(String)=False` so binders take
	the MOVE branch — `arm_scrut_payload_moved=True`, both field
	indices land in `moved_field_indices` (or did, pre-migration).
	"""
	field_ty = type_table.ensure_string()
	pair_base = type_table.declare_variant(
		module_id="main",
		name="Pair",
		type_params=[],
		arms=[
			VariantArmSchema(
				name="Pair",
				fields=[
					VariantFieldSchema(name="a", type_expr=GenericTypeExpr.named("String")),
					VariantFieldSchema(name="b", type_expr=GenericTypeExpr.named("String")),
				],
			),
		],
	)
	pair_ty = type_table.ensure_instantiated(pair_base, [])

	pair_te = TypeExpr(name="Pair", module_id="main")
	let_a0 = H.HLet(
		name="a0",
		value=H.HLiteralString("a"),
		declared_type_expr=TypeExpr(name="String"),
		is_mutable=False,
		binding_id=None,
	)
	let_a1 = H.HLet(
		name="a1",
		value=H.HLiteralString("b"),
		declared_type_expr=TypeExpr(name="String"),
		is_mutable=False,
		binding_id=None,
	)
	let_p = H.HLet(
		name="p",
		value=H.HCall(
			fn=H.HQualifiedMember(base_type_expr=pair_te, member="Pair"),
			args=[H.HVar(name="a0", binding_id=None), H.HVar(name="a1", binding_id=None)],
			kwargs=[],
		),
		declared_type_expr=pair_te,
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="p", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Pair",
				binders=["x", "_y"],
				block=H.HBlock(statements=[]),
				result=None,
				pattern_arg_form="positional",
				binder_field_indices=[0, 1],
			),
		],
	)
	hir = H.HBlock(statements=[let_a0, let_a1, let_p, H.HExprStmt(expr=match)])
	assign_node_ids(hir)
	assign_callsite_ids(hir)

	# Synthesise CallInfo for the ctor call.
	inst = type_table.get_variant_instance(pair_ty)
	assert inst is not None
	arm_def = inst.arms_by_name["Pair"]
	info = CallInfo(
		target=CallTarget.constructor(pair_ty, "Pair"),
		sig=CallSig(
			param_types=tuple(arm_def.field_types),
			user_ret_type=pair_ty,
			can_throw=False,
		),
	)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	csid = getattr(let_p.value, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = info
	return hir, pair_ty, field_ty, call_info_by_callsite_id


def _build_string_int_match_hir(type_table: TypeTable):
	"""Construct the HIR for a statement-context match on a
	String/Int-mixed variant `Pair2(s: String, n: Int)`:

	    val s0: String = "x";
	    val n0: Int = 7;
	    val p: Pair2 = Pair2::Pair2(s = s0, n = n0);
	    match p {
	        Pair2(s_b, n_b) => { ... }
	    }

	The String binder triggers MOVE (non-Copy in unit scope) →
	`arm_scrut_payload_moved=True`.  The Int binder is a POD/Copy —
	pre-migration, Filter B would skip the Int field entirely from
	the candidate set.  Post-migration, the Int field appears as a
	candidate and authoring skips it via MUST_NOT_DROP.
	"""
	string_ty = type_table.ensure_string()
	int_ty = type_table.ensure_int()
	pair_base = type_table.declare_variant(
		module_id="main",
		name="Pair2",
		type_params=[],
		arms=[
			VariantArmSchema(
				name="Pair2",
				fields=[
					VariantFieldSchema(name="s", type_expr=GenericTypeExpr.named("String")),
					VariantFieldSchema(name="n", type_expr=GenericTypeExpr.named("Int")),
				],
			),
		],
	)
	pair_ty = type_table.ensure_instantiated(pair_base, [])

	pair_te = TypeExpr(name="Pair2", module_id="main")
	let_s0 = H.HLet(
		name="s0",
		value=H.HLiteralString("x"),
		declared_type_expr=TypeExpr(name="String"),
		is_mutable=False,
		binding_id=None,
	)
	let_n0 = H.HLet(
		name="n0",
		value=H.HLiteralInt(value=7),
		declared_type_expr=TypeExpr(name="Int"),
		is_mutable=False,
		binding_id=None,
	)
	let_p = H.HLet(
		name="p",
		value=H.HCall(
			fn=H.HQualifiedMember(base_type_expr=pair_te, member="Pair2"),
			args=[H.HVar(name="s0", binding_id=None), H.HVar(name="n0", binding_id=None)],
			kwargs=[],
		),
		declared_type_expr=pair_te,
		is_mutable=False,
		binding_id=None,
	)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="p", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Pair2",
				binders=["s_b", "n_b"],
				block=H.HBlock(statements=[]),
				result=None,
				pattern_arg_form="positional",
				binder_field_indices=[0, 1],
			),
		],
	)
	hir = H.HBlock(statements=[let_s0, let_n0, let_p, H.HExprStmt(expr=match)])
	assign_node_ids(hir)
	assign_callsite_ids(hir)

	inst = type_table.get_variant_instance(pair_ty)
	assert inst is not None
	arm_def = inst.arms_by_name["Pair2"]
	info = CallInfo(
		target=CallTarget.constructor(pair_ty, "Pair2"),
		sig=CallSig(
			param_types=tuple(arm_def.field_types),
			user_ret_type=pair_ty,
			can_throw=False,
		),
	)
	call_info_by_callsite_id: dict[int, CallInfo] = {}
	csid = getattr(let_p.value, "callsite_id", None)
	if isinstance(csid, int):
		call_info_by_callsite_id[csid] = info
	return hir, pair_ty, string_ty, int_ty, call_info_by_callsite_id


def _collect_match_cleanup_hooks(builder) -> List[M.MatchCleanupHook]:
	out: List[M.MatchCleanupHook] = []
	for blk in builder.func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, M.MatchCleanupHook):
				out.append(ins)
	return out


def test_match_cleanup_hook_carries_full_field_set_including_moved() -> None:
	"""**Pin: candidate-set migration** — `MatchCleanupHook.candidates`
	contains one entry for EVERY ctor field, including fields the
	binder loop moved out.  HIR's `Filter A` (skip if in
	`moved_field_indices`) has been retired; the chain-aware ledger
	walker is the authority on Move-vs-Live per-field state.

	Carrier: a 2-field String/String variant where both binders take
	the MOVE branch.  Pre-migration, the hook would have ZERO
	candidates (both fields filtered by Filter A).  Post-migration,
	the hook has BOTH fields as candidates; authoring then queries
	`field_verdict_at` per candidate and skips both via MUST_NOT_DROP
	(the chain-aware walker correctly marked them MOVED_OUT).

	If this regresses to fewer candidates, HIR has re-introduced
	Filter A or an equivalent — the chain-aware walker is no longer
	the sole MovedOut authority.
	"""
	type_table = TypeTable()
	hir, pair_ty, _string_ty, call_info = _build_two_field_string_string_match_hir(type_table)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info,
	).lower_block(hir)

	hooks = _collect_match_cleanup_hooks(builder)
	assert len(hooks) == 1, (
		f"expected exactly one MatchCleanupHook for the partial-move arm; "
		f"got {len(hooks)}"
	)
	hook = hooks[0]

	field_indices = sorted(int(c[1]) for c in hook.candidates)
	assert field_indices == [0, 1], (
		f"candidate-set migration regressed: MatchCleanupHook.candidates "
		f"must contain field indices [0, 1] (the FULL ctor field list, "
		f"including moved fields).  Got field indices {field_indices!r}.  "
		f"HIR appears to have re-introduced Filter A "
		f"(`cleanup_fidx in moved_field_indices`) or an equivalent — the "
		f"chain-aware ledger walker (`_apply_field_state`) is the sole "
		f"MovedOut authority post-0.31.12; HIR must propose the full "
		f"field set."
	)


def test_match_cleanup_hook_includes_pod_field_and_authoring_skips() -> None:
	"""**Pin: Filter B retirement** — a POD/non-drop field (`Int`)
	appears as a candidate in `MatchCleanupHook.candidates`; authoring
	then queries `field_verdict_at` and emits NO drop chain for it
	(verdict is MUST_NOT_DROP via the `compute_drop_policy.needs_drop=
	False` collapse).

	Pre-migration, Filter B (`not _needs_runtime_drop(cleanup_fty)`)
	excluded POD fields from the candidate list.  Post-migration, HIR
	proposes the full set; the ledger / drop-policy is the sole
	authority on emit-vs-skip.

	The pin verifies BOTH halves: the Int candidate IS in the hook,
	AND no DropValue is emitted for `Int` after authoring runs.
	"""
	type_table = TypeTable()
	hir, pair_ty, string_ty, int_ty, call_info = _build_string_int_match_hir(type_table)

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(
		builder,
		type_table=type_table,
		call_info_by_callsite_id=call_info,
	).lower_block(hir)

	hooks = _collect_match_cleanup_hooks(builder)
	assert len(hooks) == 1, (
		f"expected exactly one MatchCleanupHook; got {len(hooks)}"
	)
	hook = hooks[0]

	# Half 1: the Int field is in the candidate list.
	int_candidates = [c for c in hook.candidates if int(c[1]) == 1 and c[2] == int_ty]
	assert int_candidates, (
		f"candidate-set migration regressed (Filter B): the Int field "
		f"is missing from MatchCleanupHook.candidates.  HIR appears to "
		f"have re-introduced Filter B (`not _needs_runtime_drop(...)`) "
		f"or an equivalent.  All candidates: {hook.candidates!r}"
	)

	# Half 2: run authoring, assert NO DropValue(ty=Int) is emitted.
	# (DropValue(ty=String) would still be emitted iff the String
	# field's chain-aware verdict resolves to MUST_DROP — that's a
	# separate carrier; here we focus on the Int-skip contract.)
	func = builder.func
	ledger = build_ledger(func, drop_policy=lambda _t: None)
	setattr(func, "_ownership_ledger", ledger)
	emitted = author_match_cleanup(func, type_table=type_table)
	# After authoring, scan for any DropValue(ty=Int) — there must be none.
	int_drops = []
	for blk in func.blocks.values():
		for ins in blk.instructions:
			if isinstance(ins, M.DropValue) and getattr(ins, "ty", None) == int_ty:
				int_drops.append(ins)
	assert not int_drops, (
		f"Filter B retirement regressed: authoring emitted "
		f"{len(int_drops)} `DropValue(ty=Int)` for the POD Int field.  "
		f"`compute_drop_policy(Int).needs_drop=False` should collapse "
		f"the verdict to MUST_NOT_DROP regardless of state, and authoring "
		f"must skip emission.  Drop instructions: {int_drops!r}"
	)
	# (Sanity: authoring did at least one MatchCleanupHook removal —
	# the hook is gone after the pass.)
	assert not _collect_match_cleanup_hooks(builder), (
		"authoring did not remove the MatchCleanupHook; the post-pass "
		"MIR must contain no MatchCleanupHook instructions."
	)
