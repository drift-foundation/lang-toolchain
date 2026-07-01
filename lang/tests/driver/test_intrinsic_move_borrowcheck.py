from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_replace_consumes_noncopy_arg_and_rejects_later_borrow(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module m;

import std.mem as mem;

struct Box {
	x: String
}

pub fn main() nothrow -> Int {
	var a = Box(x = "1");
	var b = Box(x = "2");
	val _ = mem.replace<type Box>(&mut a, b);
	val _r = &b;
	return 0;
}
"""
	)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any(d.phase == "borrowcheck" for d in errors), errors
	assert any("cannot borrow from moved or uninitialized 'b'" in d.message for d in errors), errors
