from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == []
	func_hirs, signatures, _ = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry="m::main",
	)
	return ir, checked


def test_while_true_nested_match_all_paths_return_no_internal(tmp_path: Path) -> None:
	ir, checked = _compile(
		tmp_path,
		"""
module m;

fn pick(flag: Int) nothrow -> Int {
	while true {
		if flag == 1 {
			return 1;
		}
		return 2;
	}
}

pub fn main() nothrow -> Int {
	return pick(1);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == []
	assert 'define i64 @"m::pick"' in ir
