# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema


def test_variant_instantiation_allows_droppable_payloads_without_tombstone() -> None:
	table = TypeTable()
	base = table.declare_variant(
		"m",
		"Maybe",
		["T"],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	inst = table.ensure_instantiated(base, [table.ensure_string()])
	inst_obj = table.get_variant_instance(inst)
	assert inst_obj is not None
	assert inst_obj.internal_tombstone_ctor == "__drift_internal_tombstone"
	assert inst_obj.internal_tombstone_tag == 2


def test_non_generic_droppable_variant_gets_internal_tombstone() -> None:
	table = TypeTable()
	choice = table.declare_variant(
		"m",
		"Choice",
		[],
		[
			VariantArmSchema(name="None", fields=[]),
			VariantArmSchema(
				name="TextVal",
				fields=[VariantFieldSchema(name="s", type_expr=GenericTypeExpr.named("String"))],
			),
		],
	)
	table.finalize_variants()
	inst_obj = table.get_variant_instance(choice)
	assert inst_obj is not None
	assert inst_obj.internal_tombstone_ctor == "__drift_internal_tombstone"
	assert inst_obj.internal_tombstone_tag == 2


def test_explicit_tombstone_is_preserved_in_variant_instance() -> None:
	table = TypeTable()
	base = table.declare_variant(
		"m",
		"Maybe",
		["T"],
		[
			VariantArmSchema(name="Tombstone", fields=[]),
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
		],
		tombstone_ctor="Tombstone",
	)
	inst = table.ensure_instantiated(base, [table.ensure_string()])
	inst_obj = table.get_variant_instance(inst)
	assert inst_obj is not None
	assert inst_obj.internal_tombstone_ctor == "Tombstone"
	assert inst_obj.internal_tombstone_tag == 0
