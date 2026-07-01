# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_interface_method_call_on_impl_type(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main;

interface I {
	fn call(self: &Self, v: Int) nothrow -> Int;
}

struct Box {
	value: Int
}

implement I for Box {
	fn call(self: &Box, v: Int) nothrow -> Int {
		return self.value + v;
	}
}

pub fn main() nothrow -> Int {
	var b = Box(value = 40);
	return b.call(2);
}
""",
	)
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
	assert checked.diagnostics == []
