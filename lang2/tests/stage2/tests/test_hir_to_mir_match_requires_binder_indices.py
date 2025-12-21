# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Stage2 match lowering: typed checker provides binder→field mapping.

Stage2 lowering treats match pattern normalization as a typed-checker
responsibility. Any constructor pattern that binds payload fields must carry a
normalized `binder_field_indices` list by the time it reaches MIR lowering.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema
from lang2.driftc.parser.ast import TypeExpr
from lang2.driftc.stage2 import HIRToMIR, MirBuilder


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
	builder = MirBuilder(name="main")
	lower = HIRToMIR(builder, type_table=type_table)
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
	builder = MirBuilder(name="main")
	HIRToMIR(builder, type_table=type_table).lower_block(hir)
