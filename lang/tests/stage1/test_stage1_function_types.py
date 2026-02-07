# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.types_core import TypeKind
from lang.driftc.parser import parse_drift_to_hir


def test_stage1_parse_function_type_literals(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
fn takes(f: Fn(Int) -> Int, g: Fn(Int) nothrow -> Int) -> Void {
	return;
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get("main::takes") or module.fn_ids_by_name.get("takes") or []
	assert len(fn_ids) == 1
	sig = module.signatures_by_id[fn_ids[0]]
	assert sig.param_type_ids is not None
	assert len(sig.param_type_ids) == 2

	first = type_table.get(sig.param_type_ids[0])
	second = type_table.get(sig.param_type_ids[1])
	assert first.kind is TypeKind.FUNCTION
	assert second.kind is TypeKind.FUNCTION
	assert first.fn_throws is True
	assert second.fn_throws is False
	assert {fid.name for fid in module.func_hirs} == {"takes"}
