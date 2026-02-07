# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	_write_file(path, src)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked.diagnostics


def test_diagnostic_byref_impl_allows_noncopy(tmp_path: Path) -> None:
	diagnostics = _compile_source(
		"""
module main

import std.core as core;
use trait core.Diagnostic;

struct S { x: Int }

implement core.Diagnostic for S {
	pub fn to_diag(self: &S) nothrow -> DiagnosticValue {
		return DiagnosticValue::Int(self.x);
	}
}

pub fn main() nothrow -> Int {
	val s = S(x = 1);
	val dv = s.to_diag();
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics == []
