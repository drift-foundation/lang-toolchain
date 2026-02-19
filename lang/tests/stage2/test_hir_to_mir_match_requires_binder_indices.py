# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Stage2 match lowering: typed checker provides binder→field mapping.

Stage2 lowering treats match pattern normalization as a typed-checker
responsibility. Any constructor pattern that binds payload fields must carry a
normalized `binder_field_indices` list by the time it reaches MIR lowering.
"""

from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.parser.ast import TypeExpr
from lang.driftc.stage2 import HIRToMIR, make_builder, VariantGetField, VariantGetFieldAddr
from lang.driftc.stage1 import assign_node_ids, assign_callsite_ids
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget


def test_match_missing_binder_field_indices_is_a_checker_bug() -> None:
	"""Stage2 asserts when binder_field_indices are missing for binding patterns."""
	type_table = TypeTable()

	# Declare `lang.core:Optional<T>` with `Some(value: T)` / `None()`.
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
	type_table.ensure_instantiated(opt_base, [type_table.ensure_int()])

	# x: Optional<Int> = Optional<Int>::Some(1)
	opt_int = TypeExpr(name="Optional", args=[TypeExpr(name="Int")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_int, member="Some")
	init = H.HCall(fn=ctor, args=[H.HLiteralInt(1)], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_int, is_mutable=False, binding_id=None)

	# match x { Some(v) => { v } default => { 0 } }  (value position)
	match = H.HMatchExpr(
		scrutinee=H.HVar(name="x", binding_id=None),
		arms=[
			H.HMatchArm(
				ctor="Some",
				binders=["v"],
				block=H.HBlock(statements=[]),
				result=H.HVar(name="v", binding_id=None),
				pattern_arg_form="positional",
				# Intentionally leave binder_field_indices empty to simulate missing typecheck.
				binder_field_indices=[],
			),
			H.HMatchArm(
				ctor=None,
				binders=[],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(0),
			),
		],
	)

	hir = H.HBlock(
		statements=[
			let_x,
			H.HLet(
				name="y",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
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
			if inst is not None:
				arm_def = inst.arms_by_name.get(stmt.value.fn.member)
				if arm_def is not None:
					info = CallInfo(
						target=CallTarget.constructor(inst_tid, stmt.value.fn.member),
						sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=inst_tid, can_throw=False),
					)
					csid = getattr(stmt.value, "callsite_id", None)
					if isinstance(csid, int):
						call_info_by_callsite_id[csid] = info
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id)
	try:
		lower.lower_block(hir)
		raise AssertionError("expected MIR lowering to reject missing binder_field_indices")
	except AssertionError as e:
		assert "binder field-index mapping missing" in str(e)


def test_match_with_positional_binders_lowers_with_indices() -> None:
	"""Stage2 lowers positional binders when binder_field_indices are present."""
	type_table = TypeTable()

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
	type_table.ensure_instantiated(opt_base, [type_table.ensure_int()])

	opt_int = TypeExpr(name="Optional", args=[TypeExpr(name="Int")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_int, member="Some")
	init = H.HCall(fn=ctor, args=[H.HLiteralInt(1)], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_int, is_mutable=False, binding_id=None)

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
			H.HMatchArm(
				ctor=None,
				binders=[],
				block=H.HBlock(statements=[]),
				result=H.HLiteralInt(0),
			),
		],
	)

	hir = H.HBlock(
		statements=[
			let_x,
			H.HLet(
				name="y",
				value=match,
				declared_type_expr=TypeExpr(name="Int"),
				is_mutable=False,
				binding_id=None,
			),
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
			if inst is not None:
				arm_def = inst.arms_by_name.get(stmt.value.fn.member)
				if arm_def is not None:
					info = CallInfo(
						target=CallTarget.constructor(inst_tid, stmt.value.fn.member),
						sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=inst_tid, can_throw=False),
					)
					csid = getattr(stmt.value, "callsite_id", None)
					if isinstance(csid, int):
						call_info_by_callsite_id[csid] = info
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id).lower_block(hir)


def test_match_by_value_copy_binder_extracts_via_value_path() -> None:
	"""Stage2 may extract by-value Copy binders directly via VariantGetField."""
	type_table = TypeTable()

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
	opt_int_tid = type_table.ensure_instantiated(opt_base, [type_table.ensure_int()])

	opt_int = TypeExpr(name="Optional", args=[TypeExpr(name="Int")], module_id="lang.core")
	ctor = H.HQualifiedMember(base_type_expr=opt_int, member="Some")
	init = H.HCall(fn=ctor, args=[H.HLiteralInt(7)], kwargs=[])
	let_x = H.HLet(name="x", value=init, declared_type_expr=opt_int, is_mutable=False, binding_id=None)

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
			H.HMatchArm(ctor=None, binders=[], block=H.HBlock(statements=[]), result=H.HLiteralInt(0)),
		],
	)

	hir = H.HBlock(
		statements=[
			let_x,
			H.HLet(name="y", value=match, declared_type_expr=TypeExpr(name="Int"), is_mutable=False, binding_id=None),
		]
	)
	assign_node_ids(hir)
	assign_callsite_ids(hir)

	call_info_by_callsite_id: dict[int, CallInfo] = {}
	for stmt in hir.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall) and isinstance(stmt.value.fn, H.HQualifiedMember):
			inst = type_table.get_variant_instance(opt_int_tid)
			assert inst is not None
			arm_def = inst.arms_by_name.get(stmt.value.fn.member)
			assert arm_def is not None
			info = CallInfo(
				target=CallTarget.constructor(opt_int_tid, stmt.value.fn.member),
				sig=CallSig(param_types=tuple(arm_def.field_types), user_ret_type=opt_int_tid, can_throw=False),
			)
			csid = getattr(stmt.value, "callsite_id", None)
			if isinstance(csid, int):
				call_info_by_callsite_id[csid] = info

	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	HIRToMIR(builder, type_table=type_table, call_info_by_callsite_id=call_info_by_callsite_id).lower_block(hir)
	all_instrs = [instr for block in builder.func.blocks.values() for instr in block.instructions]
	assert any(isinstance(instr, VariantGetField) and instr.ctor == "Some" for instr in all_instrs)
	assert not any(isinstance(instr, VariantGetFieldAddr) and instr.ctor == "Some" for instr in all_instrs)
