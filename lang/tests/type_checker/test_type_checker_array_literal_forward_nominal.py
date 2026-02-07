#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _check_fn(src: str, fn_name: str, tmp_path: Path) -> list:
	src_path = tmp_path / "array_literal_forward_nominal.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get(fn_name) or []
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


def test_array_literal_forward_nominal_copy_allowed(tmp_path: Path) -> None:
	diags = _check_fn(
		"""
fn main() -> Int {
	val pairs = [Pair(a = 1, b = 2), Pair(a = 3, b = 4)];
	val points = [Point(x = 1, y = 2), Point(x = 3, y = 4)];
	val variants = [Choice::PointVal(Point(x = 5, y = 6)), Choice::PairVal(Pair(a = 7, b = 8))];
	val total = pairs.len + points.len + variants.len;
	return total;
}

struct Pair { a: Int, b: Int }
struct Point { x: Int, y: Int }
variant Choice {
	PointVal(p: Point),
	PairVal(p: Pair),
}
""",
		"main",
		tmp_path,
	)
	assert diags == []
