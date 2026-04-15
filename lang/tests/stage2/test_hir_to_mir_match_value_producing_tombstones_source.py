# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
MIR-level contract: value-producing `match` over a non-Copy named-local
scrutinee emits `TombstoneValue(scrut_ty) + StoreLocal(source_local, tomb)`
in the arm that moves out the payload.

This pins the ownership model for the value-producing case so the
scope-drop of the scrutinee at the enclosing block's end reads a
drop-safe tombstone value (routing through the variant's
`__drift_internal_tombstone` tag) rather than the moved-from
zero-tag state whose drop dispatches to the first ctor's payload
destroy — unsafe for droppable payloads without a declared
`@tombstone` ctor.

Companion to:
  - Statement-context anchor: stage2
    `test_match_arm_no_scrutinee_drop_on_move.py` (no arm-level
    scrutinee drop when payload moved).
  - E2E regression: `match_value_producing_non_copy_drop_once/`
    (runtime assertion: sess.drops == 1, no SIGSEGV under plain
    / ASAN / memcheck).
"""
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import (
	HIRToMIR,
	MoveOut,
	StoreLocal,
	TombstoneValue,
	VariantGetFieldAddr,
	make_builder,
)


def test_value_producing_match_emits_tombstone_store_into_source_local() -> None:
	"""
	A value-producing match arm that moves a non-Copy payload out of
	a named-local scrutinee must emit `TombstoneValue(scrut_ty)` and
	`StoreLocal(source_local, tomb)` within that same arm, so the
	enclosing scope-drop of the scrutinee sees a drop-safe tombstone.
	"""
	type_table = TypeTable()
	# Variant with a non-Copy first-ctor payload (String) and a
	# zero-arg second ctor.  No `@tombstone` declared — internal
	# tombstone metadata is auto-injected by `finalize_variants`.
	opt_base = type_table.declare_variant(
		module_id="lang.core",
		name="Optional",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	opt_string_tid = type_table.ensure_instantiated(opt_base, [type_table.ensure_string()])

	opt_string = TypeExpr(name="Optional", args=[TypeExpr(name="String")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_string, member="Some")
	init = H.HCall(fn=ctor, args=[H.HLiteralString("x")], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_string, is_mutable=False, binding_id=None)

	# Value-producing match: `val y = match x { Some(v) => v, _ => "y" }`.
	# The Some arm moves `v: String` (non-Copy) out of the scrutinee's
	# payload slot, which triggers the `_ensure_arm_scrut_ptr(
	# mark_source_moved=False)` path and must emit the tombstone store.
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="x", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["v"],
				block=H.HBlock(statements=[]),
				result=H.HVar(name="v", binding_id=None),
				pattern_arg_form="positional",
				binder_field_indices=[0],
			),
			H.HMatchArm(ctor=None, binders=[], block=H.HBlock(statements=[]), result=H.HLiteralString("y")),
		],
	)

	hir = H.HBlock(
		statements=[
			let_x,
			H.HLet(name="y", value=match, declared_type_expr=TypeExpr(name="String"), is_mutable=False, binding_id=None),
		]
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)

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
				target=CallTarget.constructor(opt_string_tid, stmt.value.fn.member),
				sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=opt_string_tid, can_throw=False),
			)
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id).lower_block(hir)

	# Find the arm block that contains the Some-ctor VariantGetFieldAddr —
	# that is the arm which moves the payload out and therefore must also
	# tombstone the source local.
	some_arm_block = next(
		(
			block
			for block in builder.func.blocks.values()
			if any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in block.instructions)
		),
		None,
	)
	assert some_arm_block is not None, "expected a match arm block with a Some-ctor VariantGetFieldAddr"

	# The arm must emit TombstoneValue(scrut_ty) followed by a
	# StoreLocal into the scrutinee's source local.  Verify the
	# TombstoneValue targets the variant type, and that a StoreLocal
	# with that same SSA dest feeds a local assignment.
	tombstone_instrs = [
		instr for instr in some_arm_block.instructions if isinstance(instr, TombstoneValue)
	]
	assert tombstone_instrs, (
		"value-producing arm moved a non-Copy payload but did not emit "
		"TombstoneValue for the scrutinee type — enclosing scope-drop "
		"will read moved-from (tag=0) storage and re-drop the payload"
	)
	assert any(instr.ty == opt_string_tid for instr in tombstone_instrs), (
		"TombstoneValue.ty must be the variant scrutinee type"
	)

	# The tombstone temp produced by TombstoneValue must flow into a
	# StoreLocal targeting the scrutinee's source local ('x').  The
	# exact chain is TombstoneValue(dest=tomb) → StoreLocal(local='x',
	# value=tomb).
	tomb_dests = {instr.dest for instr in tombstone_instrs}
	store_back = [
		instr
		for instr in some_arm_block.instructions
		if isinstance(instr, StoreLocal) and instr.value in tomb_dests and instr.local == "x"
	]
	assert store_back, (
		"TombstoneValue was emitted but not stored back into the named "
		"scrutinee local 'x' — the tombstone must land in the source "
		"slot so later scope-drop is a no-op on the moved-from path"
	)

	# The tombstone store must follow the MoveOut of the scrutinee local
	# (so the MIR sequence is: MoveOut('x') → StoreLocal(arm_scrut_local)
	# → TombstoneValue → StoreLocal('x', tomb)).
	instrs = list(some_arm_block.instructions)
	move_out_idx = next(
		(i for i, instr in enumerate(instrs) if isinstance(instr, MoveOut) and instr.local == "x"),
		None,
	)
	assert move_out_idx is not None, (
		"expected a MoveOut from the scrutinee source local 'x' in the "
		"value-producing arm"
	)
	tombstone_idx = next(
		(i for i, instr in enumerate(instrs) if isinstance(instr, TombstoneValue) and instr.ty == opt_string_tid),
		None,
	)
	assert tombstone_idx is not None
	assert tombstone_idx > move_out_idx, (
		"TombstoneValue must come after the MoveOut of the scrutinee — "
		"otherwise the zero-store emitted by MoveOut would overwrite "
		"the tombstone"
	)
