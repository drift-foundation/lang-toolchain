# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
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
	return table.ensure_instantiated(base, [inner])


def test_fnresult_is_not_copy() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()
	fnres = table.ensure_fnresult(int_ty, err_ty)
	assert table.is_copy(fnres) is False


def test_optional_copy_matches_inner() -> None:
	table = TypeTable()
	int_ty = table.ensure_int()
	opt_int = _ensure_optional(table, int_ty)
	assert table.is_copy(opt_int) is True

	arr_ty = table.new_array(int_ty)
	opt_arr = _ensure_optional(table, arr_ty)
	assert table.is_copy(opt_arr) is False
