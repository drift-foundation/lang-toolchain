# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import StructFieldSchema, TypeParamId, TypeTable, VariantArmSchema, VariantFieldSchema


def test_struct_template_is_cached() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base = table.declare_struct("m", "Box", ["value"], type_params=["T"])
	table.define_struct_schema_fields(
		base,
		[StructFieldSchema(name="value", type_expr=GenericTypeExpr(param_index=0))],
	)
	param_id = TypeParamId(owner=FunctionId(module="m", name="Box", ordinal=0), index=0)
	tvar = table.ensure_typevar(param_id, name="T")
	inst_a = table.ensure_struct_template(base, [tvar])
	inst_b = table.ensure_struct_template(base, [tvar])
	assert inst_a == inst_b


def test_variant_template_is_cached() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base = table.declare_variant(
		"m",
		"Opt",
		["T"],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr(param_index=0))],
			)
		],
	)
	param_id = TypeParamId(owner=FunctionId(module="m", name="Opt", ordinal=0), index=0)
	tvar = table.ensure_typevar(param_id, name="T")
	inst_a = table.ensure_variant_template(base, [tvar])
	inst_b = table.ensure_variant_template(base, [tvar])
	assert inst_a == inst_b
