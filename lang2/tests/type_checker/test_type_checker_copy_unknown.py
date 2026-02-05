#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang2.driftc.parser import parse_drift_to_hir
from lang2.driftc.type_checker import TypeChecker


def _check_fn(src: str, fn_name: str, tmp_path: Path) -> list:
	src_path = tmp_path / "copy_unknown.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get(fn_name) or []
	if not fn_ids:
		qualified = [name for name in module.fn_ids_by_name.keys() if name.endswith(f"::{fn_name}")]
		if len(qualified) == 1:
			fn_ids = module.fn_ids_by_name.get(qualified[0]) or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	fn_block = module.func_hirs[fn_id]
	fn_sig = module.signatures_by_id.get(fn_id)
	tc = TypeChecker(type_table=type_table)
	param_types = None
	if fn_sig is not None and fn_sig.param_names and fn_sig.param_type_ids:
		param_types = dict(zip(fn_sig.param_names, fn_sig.param_type_ids))
	result = tc.check_function(
		fn_id,
		fn_block,
		param_types=param_types,
		return_type=fn_sig.return_type_id if fn_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=None,
	)
	return result.diagnostics


def test_copy_unknown_in_generic_explicit_copy(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
module m_main

fn mk<T>(x: T) -> Int {
	val y = copy x;
	return 0;
}
""",
		"mk",
		tmp_path,
	)
	assert any(d.code == "E-COPY-UNKNOWN" for d in diags)


def test_copy_unknown_in_array_dup_generic(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
module m_main

fn mk<T>(xs: Array<T>) -> Int {
	val ys = xs.dup();
	return ys.len;
}
""",
		"mk",
		tmp_path,
	)
	assert any(d.code == "E-COPY-UNKNOWN" for d in diags)
