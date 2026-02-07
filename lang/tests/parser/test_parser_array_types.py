# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Ensure parsed Array<T> annotations flow into signatures with real TypeIds.
"""

from pathlib import Path

from lang.driftc.core.types_core import TypeKind
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.test_helpers import assert_module_lowered_consistent


def test_parse_array_types_in_signatures(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn takes(xs: Array<Int>) -> Int {
    return xs[0];
}

	fn returns_array() -> Array<Int> {
    return [1, 2, 3];
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	assert_module_lowered_consistent(module)
	takes_ids = module.fn_ids_by_name.get("main::takes") or module.fn_ids_by_name.get("takes") or []
	returns_ids = module.fn_ids_by_name.get("main::returns_array") or module.fn_ids_by_name.get("returns_array") or []
	assert len(takes_ids) == 1
	assert len(returns_ids) == 1

	int_ty = type_table.new_scalar("Int")  # will get a fresh id; compare by name instead
	array_param_ty = module.signatures_by_id[takes_ids[0]].param_type_ids[0]
	array_ret_ty = module.signatures_by_id[returns_ids[0]].return_type_id

	array_def = type_table.get(array_param_ty)
	assert array_def.kind is TypeKind.ARRAY
	elem_ty = array_def.param_types[0]
	assert type_table.get(elem_ty).name == "Int"

	array_def_ret = type_table.get(array_ret_ty)
	assert array_def_ret.kind is TypeKind.ARRAY
	elem_ty_ret = array_def_ret.param_types[0]
	assert type_table.get(elem_ty_ret).name == "Int"

	# HIR is still produced for completeness.
	assert {fid.name for fid in module.func_hirs} == {"takes", "returns_array"}
