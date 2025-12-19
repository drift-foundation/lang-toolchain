from __future__ import annotations

from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.generic_type_expr import GenericTypeExpr
from lang2.driftc.core.types_core import (
	TypeKind,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang2.driftc.parser.ast import TypeExpr


def test_resolve_builtin_void_and_error_are_canonical():
	table = TypeTable()

	void_ty = resolve_opaque_type("Void", table)
	err_ty = resolve_opaque_type("Error", table)

	assert table.get(void_ty).kind is TypeKind.VOID
	assert void_ty == table.ensure_void()

	assert table.get(err_ty).kind is TypeKind.ERROR
	assert err_ty == table.ensure_error()


def test_resolve_fnresult_and_array_typeexpr():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()

	fnres_expr = TypeExpr(name="FnResult", args=[TypeExpr(name="Int"), TypeExpr(name="Error")])
	fnres_ty = resolve_opaque_type(fnres_expr, table)
	fnres_def = table.get(fnres_ty)

	assert fnres_def.kind is TypeKind.FNRESULT
	assert fnres_def.param_types == [int_ty, err_ty]

	arr_ty = resolve_opaque_type("Array<Int>", table)
	arr_def = table.get(arr_ty)

	assert arr_def.kind is TypeKind.ARRAY
	assert arr_def.param_types == [int_ty]


def test_resolve_tuple_fnresult():
	table = TypeTable()
	int_ty = table.ensure_int()
	err_ty = table.ensure_error()

	fnres_ty = resolve_opaque_type((int_ty, err_ty), table)

	fnres_def = table.get(fnres_ty)
	assert fnres_def.kind is TypeKind.FNRESULT
	assert fnres_def.param_types == [int_ty, err_ty]


def test_resolve_optional_and_diagnostic_value():
	table = TypeTable()
	# In the language, `Optional<T>` is a generic variant declared in `lang.core`,
	# not a special-case builtin mapped by name.
	table.declare_variant(
		"lang.core",
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
	opt_expr = TypeExpr(name="Optional", args=[TypeExpr(name="Int")])
	opt_ty = resolve_opaque_type(opt_expr, table)
	opt_def = table.get(opt_ty)
	assert opt_def.kind is TypeKind.VARIANT
	assert opt_def.param_types == [table.ensure_int()]

	dv_ty = resolve_opaque_type("DiagnosticValue", table)
	assert table.get(dv_ty).kind is TypeKind.DIAGNOSTICVALUE
