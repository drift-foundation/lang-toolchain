from __future__ import annotations

from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.types_core import (
	TypeKind,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang2.driftc.parser import ast as parser_ast


def test_variant_instantiation_produces_concrete_instance() -> None:
	table = TypeTable()
	table.ensure_int()
	table.ensure_string()

	# Declare `variant Optional<T> { Some(value: T), None }`.
	opt_base = table.declare_variant(
		"Optional",
		["T"],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)

	opt_int = table.ensure_instantiated(opt_base, [table.ensure_int()])
	assert table.get(opt_int).kind is TypeKind.VARIANT

	inst = table.get_variant_instance(opt_int)
	assert inst is not None
	assert set(inst.arms_by_name.keys()) == {"Some", "None"}
	assert inst.arms_by_name["Some"].tag == 0
	assert inst.arms_by_name["None"].tag == 1
	assert inst.arms_by_name["Some"].field_names == ["value"]
	assert inst.arms_by_name["Some"].field_types == [table.ensure_int()]


def test_resolve_opaque_type_instantiates_variant_when_declared() -> None:
	table = TypeTable()
	opt_base = table.declare_variant(
		"Optional",
		["T"],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	# TypeExpr-like object from the real parser AST.
	raw = parser_ast.TypeExpr(name="Optional", args=[parser_ast.TypeExpr(name="Int")])
	ty = resolve_opaque_type(raw, table)
	assert table.get(ty).kind is TypeKind.VARIANT
	assert ty != opt_base
	inst = table.get_variant_instance(ty)
	assert inst is not None
	assert inst.arms_by_name["Some"].field_types == [table.ensure_int()]

