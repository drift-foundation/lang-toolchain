# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage1 import assign_callsite_ids, assign_node_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget
from lang.driftc.stage2 import CopyValue, DropValue, HIRToMIR, MoveOut, VariantGetFieldAddr, make_builder


def test_match_by_value_noncopy_binder_moves_payload_and_zeros_source() -> None:
	# Non-Copy payload exemplar is Array<Int>: since String Scope A
	# (0.33.75 era; doc/history.md) String is structurally Copy even in
	# an isolated TypeTable, so a String payload no longer takes the MOVE
	# branch — see test_match_by_value_string_binder_copies_payload below
	# for the String contract.
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
	arr_int_tid = type_table.new_array(type_table.ensure_int())
	opt_string_tid = type_table.ensure_instantiated(opt_base, [arr_int_tid])

	arr_int_te = TypeExpr(name="Array", args=[TypeExpr(name="Int")])
	opt_string = TypeExpr(name="Optional", args=[arr_int_te], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_string, member="Some")
	init = H.HCall(fn=ctor, args=[H.HArrayLiteral(elements=[H.HLiteralInt(value=1)])], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_string, is_mutable=False, binding_id=None)

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
			H.HMatchArm(ctor=None, binders=[], block=H.HBlock(statements=[]), result=H.HArrayLiteral(elements=[])),
		],
	)

	hir = H.HBlock(
		statements=[
			let_x,
			H.HLet(name="y", value=match, declared_type_expr=arr_int_te, is_mutable=False, binding_id=None),
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
	all_instrs = [instr for block in builder.func.blocks.values() for instr in block.instructions]
	assert any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in all_instrs)
	assert any(isinstance(instr, MoveOut) and instr.ty == arr_int_tid for instr in all_instrs)
	arm_blocks = [block for block in builder.func.blocks.values() if any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in block.instructions)]
	assert arm_blocks
	for block in arm_blocks:
		assert not any(isinstance(instr, DropValue) and instr.ty == opt_string_tid for instr in block.instructions)


def test_match_by_value_string_binder_copies_payload() -> None:
	"""String Scope A contract: an `Optional<String>` by-value match binder
	COPIES the payload (retain via CopyValue), never MoveOut — String is
	structurally Copy in every mode, isolated TypeTables included.
	Pre-Scope-A this shape moved+zeroed in isolated stage2 (the divergence
	the noncopy test above used to pin via a String payload)."""
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
	string_tid = type_table.ensure_string()
	opt_string_tid = type_table.ensure_instantiated(opt_base, [string_tid])

	opt_string = TypeExpr(name="Optional", args=[TypeExpr(name="String")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_string, member="Some")
	init = H.HCall(fn=ctor, args=[H.HLiteralString("x")], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_string, is_mutable=False, binding_id=None)

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
	all_instrs = [instr for block in builder.func.blocks.values() for instr in block.instructions]
	assert any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in all_instrs)
	assert not any(isinstance(instr, MoveOut) and instr.ty == string_tid for instr in all_instrs)
	assert any(isinstance(instr, CopyValue) and instr.ty == string_tid for instr in all_instrs)
