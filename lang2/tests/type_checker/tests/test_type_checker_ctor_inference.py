#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang2.driftc.parser import parse_drift_to_hir
from lang2.driftc.type_checker import TypeChecker


def _check_main(src: str, tmp_path: Path) -> list:
	src_path = tmp_path / "ctor_infer.drift"
	src_path.write_text(src)
	func_hirs, sigs, fn_ids_by_name, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = func_hirs[main_id]
	main_sig = sigs.get(main_id)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=None,
	)
	return result.diagnostics


def test_ctor_infers_from_expected_type_in_let(tmp_path: Path) -> None:
	diags = _check_main(
		"""
fn main() returns Int {
	val x: Optional<Int> = Optional::None();
	return 0;
}
""",
		tmp_path,
	)
	assert diags == []


def test_ctor_infers_from_expected_return_type(tmp_path: Path) -> None:
	diags = _check_main(
		"""
fn main() returns Optional<Int> {
	return Optional::None();
}
""",
		tmp_path,
	)
	assert diags == []


def test_ctor_missing_expected_type_is_error(tmp_path: Path) -> None:
	diags = _check_main(
		"""
fn main() returns Int {
	val x = Optional::None();
	return 0;
}
""",
		tmp_path,
	)
	assert any("E-QMEM-CANNOT-INFER" in d.message for d in diags)
