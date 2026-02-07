from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.driftc import compile_to_llvm_ir_for_tests


def _extract_llvm_function(ir: str, name: str) -> str:
	start = ir.find(f"define ")
	lines = ir.splitlines()
	out: list[str] = []
	in_fn = False
	for line in lines:
		if line.startswith("define ") and f"@\"{name}\"" in line or line.startswith(f"define ") and f"@{name}" in line:
			in_fn = True
		if in_fn:
			out.append(line)
			if line.strip() == "}":
				break
	return "\n".join(out)


def test_array_dup_string_uses_retain(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main
import std.console as console;

fn main() nothrow -> Int {
	val xs: Array<String> = ["a", "b"];
	val ys = xs.dup();
	console.println(ys[0]);
	return 0;
}
"""
	)
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics
	main_ir = _extract_llvm_function(ir, "main")
	assert "drift_string_retain" in ir
	assert "drift_string_from_utf8_bytes" not in main_ir
	assert "llvm.memcpy" not in main_ir


def test_array_literal_reuses_string_lvalue_retains(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main

fn main() nothrow -> Int {
	val s: String = "hi";
	val xs: Array<String> = [s, s];
	return 0;
}
"""
	)
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics
	main_ir = _extract_llvm_function(ir, "main")
	assert main_ir.count("drift_string_retain") >= 2


def test_array_index_negative_literal_bounds_check_ir(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main

fn main() nothrow -> Int {
	val xs: Array<Int> = [1, 2];
	val v = xs[-1];
	return 0;
}
"""
	)
	modules, type_table, exc_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exc_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics
	main_ir = _extract_llvm_function(ir, "main")
	assert "drift_bounds_check" not in main_ir
	assert "drift_error_new" in main_ir
	assert "drift_error_add_attr_dv" in main_ir
	assert "-1" in main_ir
