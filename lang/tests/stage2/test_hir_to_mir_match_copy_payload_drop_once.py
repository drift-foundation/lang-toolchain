# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import CopyValue, DropValue, HIRToMIR, VariantGetFieldAddr, make_builder


def _collect_ctor_callinfo(hir: H.HBlock, type_table: TypeTable, var_tid: TypeId) -> dict[int, CallInfo]:
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
			info = CallInfo(target=CallTarget.constructor(var_tid, stmt.value.fn.member), sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=var_tid, can_throw=False))
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info
	return call_info_by_callsite_id


def test_match_copy_payload_emits_copyvalue_and_has_single_scrutinee_drop_across_cfg() -> None:
	"""Phase 4 site-1 patch 3 update: `lower_block`'s end-of-block
	scope drop now emits a `M.CleanupHook` marker rather than an
	inline `DropValue`; the materialised drop is authored by
	`cleanup_authoring.author_cleanup` after the ledger build.
	The semantic invariant pinned here — exactly one `DropValue`
	of the variant type across the post-match CFG, never in
	`match_dispatch` — is unchanged.  Same update pattern as
	`test_scope_drop_swap.test_emission_definite_live_string_...`.
	"""
	from lang.driftc.stage2.ownership_ledger import build_ledger
	from lang.driftc.stage2.cleanup_authoring import author_cleanup
	type_table = TypeTable()
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(name="Some", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0)), VariantFieldSchema(name="msg", type_expr=GenericTypeExpr.named("String"))]),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	int_ty = type_table.ensure_int()
	var_tid = type_table.ensure_instantiated(var_base, [int_ty])
	var_te = TypeExpr(name="V", args=[TypeExpr(name="Int")], module_id="main")
	ctor = H.HQualifiedMember(base_type_expr=var_te, member="Some")
	let_x = H.HLet(name="x", value=H.HCall(fn=ctor, args=[H.HLiteralInt(value=7), H.HLiteralString("msg")], kwargs=[]), declared_type_expr=var_te, is_mutable=False, binding_id=None)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="x", binding_id=None),
		arms=[
			H.HMatchArm(ctor="Some", binders=["v", "m"], block=H.HBlock(statements=[]), result=H.HVar(name="v", binding_id=None), pattern_arg_form="positional", binder_field_indices=[0, 1]),
			H.HMatchArm(ctor=None, binders=[], block=H.HBlock(statements=[]), result=H.HLiteralInt(value=0)),
		],
	)
	hir = H.HBlock(statements=[let_x, H.HLet(name="y", value=match, declared_type_expr=TypeExpr(name="Int"), is_mutable=False, binding_id=None)])
	assign_node_ids(hir)
	assign_callsite_ids(hir)
	call_info_by_callsite_id = _collect_ctor_callinfo(hir, type_table, var_tid)
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id).lower_block(hir)
	ledger = build_ledger(builder.func, drop_policy=lambda _t: None)
	setattr(builder.func, "_ownership_ledger", ledger)
	author_cleanup(builder.func, type_table=type_table)
	arm_blocks = [block for block in builder.func.blocks.values() if block.name.startswith("match_arm_")]
	assert arm_blocks
	some_arm = next((block for block in arm_blocks if any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in block.instructions)), None)
	assert some_arm is not None
	assert any(isinstance(instr, CopyValue) and instr.ty == int_ty for instr in some_arm.instructions)
	total_variant_drops = 0
	for block in builder.func.blocks.values():
		drops = [instr for instr in block.instructions if isinstance(instr, DropValue) and instr.ty == var_tid]
		total_variant_drops += len(drops)
		if block.name.startswith("match_arm_"):
			assert len(drops) <= 1
		if block.name == "match_dispatch":
			assert not drops
	assert total_variant_drops == 1
