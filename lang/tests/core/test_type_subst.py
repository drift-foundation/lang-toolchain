# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.type_subst import Subst, apply_subst
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	TypeKind,
	TypeParamId,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)


def _ensure_optional(table: TypeTable, inner: int) -> int:
	base = table.get_variant_base(module_id="lang.core", name="Optional")
	if base is None:
		base = table.declare_variant(
			"lang.core",
			"Optional",
			["T"],
			[
				VariantArmSchema(name="None", fields=[]),
				VariantArmSchema(
					name="Some",
					fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
				),
			],
		)
	if table.has_typevar(inner):
		return table.ensure_variant_template(base, [inner])
	return table.ensure_variant_instantiated(base, [inner])


def test_subst_owner_isolated() -> None:
	table = TypeTable()
	fid_a = FunctionId(module="main", name="f", ordinal=0)
	fid_b = FunctionId(module="main", name="g", ordinal=0)
	tp_a = TypeParamId(owner=fid_a, index=0)
	tp_b = TypeParamId(owner=fid_b, index=0)
	tv_a = table.ensure_typevar(tp_a, name="T")
	tv_b = table.ensure_typevar(tp_b, name="T")
	subst = Subst(owner=fid_a, args=[table.ensure_int()])
	assert apply_subst(tv_a, subst, table) == table.ensure_int()
	assert apply_subst(tv_b, subst, table) == tv_b


def test_subst_nested_types() -> None:
	table = TypeTable()
	fid = FunctionId(module="main", name="wrap", ordinal=0)
	tp = TypeParamId(owner=fid, index=0)
	tv = table.ensure_typevar(tp, name="T")
	arr = table.new_array(tv)
	opt = _ensure_optional(table, arr)
	subst = Subst(owner=fid, args=[table.ensure_int()])
	res = apply_subst(opt, subst, table)
	td = table.get(res)
	assert td.kind is TypeKind.VARIANT
	inst = table.get_variant_instance(res)
	assert inst is not None
	assert inst.type_args[0] == table.new_array(table.ensure_int())
