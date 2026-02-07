from __future__ import annotations

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	TypeKind,
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


def test_type_table_registers_basic_kinds():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	bool_ty = table.new_scalar("Bool")
	err_ty = table.new_error("Error")
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	fn_ty = table.new_function([int_ty], bool_ty)
	unknown_ty = table.new_unknown()

	assert table.get(int_ty).kind is TypeKind.SCALAR
	assert table.get(int_ty).name == "Int"

	assert table.get(err_ty).kind is TypeKind.ERROR

	fnres_def = table.get(fnres_ty)
	assert fnres_def.kind is TypeKind.FNRESULT
	assert fnres_def.param_types == [int_ty, err_ty]

	fn_def = table.get(fn_ty)
	assert fn_def.kind is TypeKind.FUNCTION
	assert fn_def.param_types == [int_ty, bool_ty]

	assert table.get(unknown_ty).kind is TypeKind.UNKNOWN


def test_type_table_seeds_void():
	table = TypeTable()

	void_ty = table.ensure_void()

	# Canonical Void is a dedicated kind and id should be stable.
	assert table.get(void_ty).kind is TypeKind.VOID
	assert void_ty == table.ensure_void()


def test_type_table_seeds_error_canonically():
	table = TypeTable()

	err_ty = table.ensure_error()

	assert table.get(err_ty).kind is TypeKind.ERROR
	assert table.get(err_ty).name == "Error"
	assert err_ty == table.ensure_error()


def test_type_table_seeds_diagnostic_value_and_optional():
	table = TypeTable()
	dv_ty = table.ensure_diagnostic_value()
	opt_int = _ensure_optional(table, table.ensure_int())
	opt_int_again = _ensure_optional(table, table.ensure_int())

	assert table.get(dv_ty).kind is TypeKind.DIAGNOSTICVALUE
	assert dv_ty == table.ensure_diagnostic_value()
	assert table.get(opt_int).kind is TypeKind.VARIANT
	inst = table.get_variant_instance(opt_int)
	assert inst is not None
	assert inst.type_args == [table.ensure_int()]
	# Optional cache should reuse the same id for the same inner.
	assert opt_int == opt_int_again


def test_variant_instantiation_cache_is_stable() -> None:
	table = TypeTable()
	base = table.declare_variant(
		"m",
		"Result",
		["T"],
		[
			VariantArmSchema(name="Ok", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
			VariantArmSchema(name="Err", fields=[]),
		],
	)
	int_ty = table.ensure_int()
	inst_a = table.ensure_instantiated(base, [int_ty])
	inst_b = table.ensure_instantiated(base, [int_ty])
	assert inst_a == inst_b
	inst = table.get_variant_instance(inst_a)
	assert inst is not None
	assert [arm.name for arm in inst.arms] == ["Ok", "Err"]
