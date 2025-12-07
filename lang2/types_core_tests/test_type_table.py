from __future__ import annotations

from lang2.core.types_core import TypeTable, TypeKind


def test_type_table_registers_basic_kinds():
	table = TypeTable()
	int_ty = table.new_scalar("Int")
	bool_ty = table.new_scalar("Bool")
	err_ty = table.new_error("Error")
	fnres_ty = table.new_fnresult(int_ty, err_ty)
	fn_ty = table.new_function("foo", [int_ty], bool_ty)
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
