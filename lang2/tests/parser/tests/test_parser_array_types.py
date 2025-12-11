# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Ensure parsed Array<T> annotations flow into signatures with real TypeIds.
"""

from pathlib import Path

from lang2.core.types_core import TypeKind
from lang2.parser import parse_drift_to_hir


def test_parse_array_types_in_signatures(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn takes(xs: Array<Int>) returns Int {
    return xs[0];
}

	fn returns_array() returns Array<Int> {
    return [1, 2, 3];
}
"""
	)
	func_hirs, sigs, type_table, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert "takes" in sigs and "returns_array" in sigs

	int_ty = type_table.new_scalar("Int")  # will get a fresh id; compare by name instead
	array_param_ty = sigs["takes"].param_type_ids[0]
	array_ret_ty = sigs["returns_array"].return_type_id

	array_def = type_table.get(array_param_ty)
	assert array_def.kind is TypeKind.ARRAY
	elem_ty = array_def.param_types[0]
	assert type_table.get(elem_ty).name == "Int"

	array_def_ret = type_table.get(array_ret_ty)
	assert array_def_ret.kind is TypeKind.ARRAY
	elem_ty_ret = array_def_ret.param_types[0]
	assert type_table.get(elem_ty_ret).name == "Int"

	# HIR is still produced for completeness.
	assert set(func_hirs.keys()) == {"takes", "returns_array"}
