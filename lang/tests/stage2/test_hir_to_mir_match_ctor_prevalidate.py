# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import HIRToMIR, make_builder


def test_match_unknown_ctor_prevalidated_before_arm_blocks() -> None:
	type_table = TypeTable()
	opt_base = type_table.declare_variant(
		module_id="lang.core",
		name="Optional",
		type_params=["T"],
		arms=[
			VariantArmSchema(name="Some", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	opt_int_tid = type_table.ensure_instantiated(opt_base, [type_table.ensure_int()])
	opt_int = TypeExpr(name="Optional", args=[TypeExpr(name="Int")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_int, member="Some")
	let_x = H.HLet(name="x", value=H.HCall(fn=ctor, args=[H.HLiteralInt(value=1)], kwargs=[]), declared_type_expr=opt_int, is_mutable=False, binding_id=None)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="x", binding_id=None),
		arms=[
			H.HMatchArm(ctor="Bogus", binders=[], block=H.HBlock(statements=[]), result=H.HLiteralInt(value=1), pattern_arg_form="positional", binder_field_indices=[]),
			H.HMatchArm(ctor=None, binders=[], block=H.HBlock(statements=[]), result=H.HLiteralInt(value=0)),
		],
	)
	hir = H.HBlock(statements=[let_x, H.HLet(name="y", value=match, declared_type_expr=TypeExpr(name="Int"), is_mutable=False, binding_id=None)])
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
			info = CallInfo(target=CallTarget.constructor(opt_int_tid, stmt.value.fn.member), sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=opt_int_tid, can_throw=False))
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lowering = HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id)
	with pytest.raises(AssertionError, match="unknown constructor in match reached MIR lowering"):
		lowering.lower_block(hir)
	assert not any(name.startswith("match_arm_") for name in builder.func.blocks.keys())
