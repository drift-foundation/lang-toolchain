from __future__ import annotations

import re
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.tests.support.module_packages import mk_module


def _extract_llvm_function(ir: str, fn: str) -> str:
	"""
	Extract the textual LLVM IR for a single function definition.

	This is intentionally simple and stable for tests: locate `define ... @<fn>`
	and return everything up to (but not including) the next `define` line.
	"""
	lines = ir.splitlines()
	start = None
	for i, line in enumerate(lines):
		if line.startswith("define ") and re.search(rf"@{re.escape(fn)}\b", line):
			start = i
			break
	if start is None:
		raise AssertionError(f"missing LLVM function definition for {fn}")
	end = len(lines)
	for i in range(start + 1, len(lines)):
		if lines[i].startswith("define "):
			end = i
			break
	return "\n".join(lines[start:end])


def _find_llvm_define_lines(ir: str, pattern: str) -> list[str]:
	lines = []
	for line in ir.splitlines():
		if line.startswith("define ") and re.search(pattern, line):
			lines.append(line)
	return lines


def test_cross_module_exported_call_uses_wrapper_not_impl(tmp_path: Path) -> None:
	"""
	Cross-module calls to exported entrypoints must call the public wrapper symbol,
	not the private `__impl` body.
	"""
	(tmp_path / "acme" / "point").mkdir(parents=True)
	(tmp_path / "acme" / "point" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.point",
				"",
				"export { Point, make_point };",
				"",
				"pub struct Point { pub x: Int, pub y: Int }",
				"",
				"pub fn make_point() -> Point {",
				"\treturn Point(x = 1, y = 2);",
				"}",
				"",
			]
		)
	)
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main",
				"",
				"import acme.point as ap;",
				"",
				"fn main() nothrow -> Int {",
				"\tval p: ap.Point = try ap.make_point() catch { ap.Point(x = 0, y = 0) };",
				"\treturn p.x + p.y;",
				"}",
				"",
			]
		)
	)

	module_packages = {}
	mk_module(module_packages, "main", "app")
	mk_module(module_packages, "acme.point", "acme")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics

	main_ir = _extract_llvm_function(ir, "main")
	assert '@"acme.point::make_point"' in main_ir
	assert "__impl" not in main_ir


def test_cross_module_exported_call_uses_throw_abi(tmp_path: Path) -> None:
	(tmp_path / "acme" / "lib").mkdir(parents=True)
	(tmp_path / "acme" / "lib" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.lib",
				"",
				"export { get_value };",
				"",
				"pub fn get_value() nothrow -> Int {",
				"\treturn 42;",
				"}",
				"",
			]
		)
	)
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main",
				"",
				"import acme.lib as lib;",
				"",
				"fn main() nothrow -> Int {",
				"\tval v = try lib.get_value() catch { 0 };",
				"\treturn v;",
				"}",
				"",
			]
		)
	)

	module_packages = {}
	mk_module(module_packages, "main", "app")
	mk_module(module_packages, "acme.lib", "acme")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics

	defines = _find_llvm_define_lines(ir, r'@"acme\.lib::get_value"')
	assert defines
	assert defines[0].startswith("define { i64, %DriftError* }")


def test_generic_instantiation_uses_throw_abi(tmp_path: Path) -> None:
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main",
				"",
				"fn id<T>(x: T) -> T {",
				"\treturn x;",
				"}",
				"",
				"fn main() nothrow -> Int {",
				"\tval v = try id(7) catch { 0 };",
				"\treturn v;",
				"}",
				"",
			]
		)
	)

	module_packages = {}
	mk_module(module_packages, "main", "app")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics

	inst_defines = _find_llvm_define_lines(ir, r'@id__inst__')
	assert inst_defines
	assert "%FnResult_Int_Error" in inst_defines[0]
	main_ir = _extract_llvm_function(ir, "main")
	assert "call %FnResult_Int_Error @id__inst__" in main_ir


def test_cross_module_generic_method_uses_wrapper_throw_abi(tmp_path: Path) -> None:
	(tmp_path / "acme" / "box").mkdir(parents=True)
	(tmp_path / "acme" / "box" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.box",
				"",
				"export { Box, make };",
				"",
				"pub struct Box<T> { pub value: T }",
				"",
				"pub fn make<T>(v: T) nothrow -> Box<T> {",
				"\treturn Box<type T>(value = v);",
				"}",
				"",
				"implement<T> Box<T> {",
				"\tpub fn wrap<U>(self: Box<T>, v: U) nothrow -> U {",
				"\t\treturn v;",
				"\t}",
				"}",
				"",
			]
		)
	)
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main",
				"",
				"import acme.box as box;",
				"",
				"fn main() nothrow -> Int {",
				"\tval b = try box.make(1) catch { box.Box<type Int>(value = 0) };",
				"\tval out = try b.wrap(7) catch { 0 };",
				"\treturn out;",
				"}",
				"",
			]
		)
	)

	module_packages = {}
	mk_module(module_packages, "main", "app")
	mk_module(module_packages, "acme.box", "acme")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert not diags
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)

	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		entry="main",
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not checked.diagnostics

	wrapper_defines = _find_llvm_define_lines(ir, r'__wrap_method::.*wrap__inst__')
	assert wrapper_defines
	assert "%FnResult_Int_Error" in wrapper_defines[0]
	main_ir = _extract_llvm_function(ir, "main")
	assert "call %FnResult_Int_Error @\"acme.box::__wrap_method::" in main_ir
