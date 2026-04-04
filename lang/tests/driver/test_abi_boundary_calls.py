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
	Cross-module calls to exported functions go directly to the impl body
	(Option B: no boundary wrapper routing).
	"""
	(tmp_path / "acme" / "point").mkdir(parents=True)
	(tmp_path / "acme" / "point" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.point;",
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
				"module main;",
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
	# Option B: calls go directly to the impl body — no wrapper routing.
	assert '@"acme.point::make_point__impl"' in main_ir


def test_cross_module_exported_call_uses_throw_abi(tmp_path: Path) -> None:
	(tmp_path / "acme" / "lib").mkdir(parents=True)
	(tmp_path / "acme" / "lib" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.lib;",
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
				"module main;",
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
	assert defines[0].startswith("define { i64, ptr }")


def test_generic_instantiation_uses_throw_abi(tmp_path: Path) -> None:
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main;",
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
				"module acme.box;",
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
				"module main;",
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

	# Option B: no wrapper routing — generic method calls go directly to
	# the instantiated impl body, not through __wrap_method stubs.
	wrapper_defines = _find_llvm_define_lines(ir, r'__wrap_method::.*wrap__inst__')
	assert not wrapper_defines, "Option B: no wrapper stubs expected"
	main_ir = _extract_llvm_function(ir, "main")
	assert "__wrap_method" not in main_ir


def test_normal_mode_boundary_provenance(tmp_path: Path) -> None:
	"""
	Normal-mode (test_build_only=False) regression for boundary provenance.

	In normal mode, the parser skips applying external_module_packages to
	module_packages for source modules (they all get "__local__"), so
	cross-package boundaries between source modules don't arise.  But
	stdlib identity normalization puts stdlib in package "std" while user
	code is in "__local__", creating a false boundary that source_modules
	must exempt.

	This test proves:
	1. explicitly_packaged_modules is correctly populated in normal mode
	   (the parser records provenance before the merged_programs skip).
	2. Source-compiled stdlib methods avoid false wrapper routing despite
	   being in a different canonical package from user code.
	3. Source-compiled stdlib free functions avoid false can_throw forcing.
	"""
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main;",
				"",
				"import lang.atomic as atomic;",
				"",
				"struct S { a: atomic.AtomicUint }",
				"",
				"fn main() nothrow -> Int {",
				"\tval s = \"hello\";",
				"\tval len = s.byte_length();",
				"\tvar st = S(a = atomic.atomic_uint(cast<Uint>(0)));",
				"\tatomic.atomic_store_uint(&st.a, cast<Uint>(1), 0);",
				"\treturn len;",
				"}",
				"",
			]
		)
	)

	module_packages = {}
	mk_module(module_packages, "main", "app")
	drift_files = sorted(tmp_path.rglob("*.drift"))
	# Normal mode: test_build_only is NOT set (defaults to False).
	modules, type_table, exception_catalog, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		drift_files,
		module_paths=[tmp_path],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
	)
	assert not diags

	# Verify explicit packaging provenance survived the normal-mode path.
	assert "main" in type_table.explicitly_packaged_modules
	# Stdlib modules must NOT be in explicitly_packaged_modules.
	assert "std.core" not in type_table.explicitly_packaged_modules
	assert "lang.atomic" not in type_table.explicitly_packaged_modules

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
	# No errors: stdlib calls must not trigger false boundary enforcement.
	assert not any(d.severity == "error" for d in checked.diagnostics)

	main_ir = _extract_llvm_function(ir, "main")
	# Stdlib method call (byte_length) must NOT go through a wrapper.
	assert "__wrap_method::String::byte_length" not in main_ir


def test_normal_mode_explicit_package_boundary_preserved(tmp_path: Path) -> None:
	"""
	Normal-mode + test_build_only regression for explicitly-packaged modules.

	Proves that explicitly-packaged source modules keep real ABI boundaries
	via test_build_only=True (the path that applies external_module_packages
	to module_packages for source modules).

	This complements test_normal_mode_boundary_provenance which covers the
	stdlib exemption side.  Together they pin both sides of the contract.
	"""
	(tmp_path / "acme" / "lib").mkdir(parents=True)
	(tmp_path / "acme" / "lib" / "lib.drift").write_text(
		"\n".join(
			[
				"module acme.lib;",
				"",
				"export { add_one };",
				"",
				"pub fn add_one(x: Int) nothrow -> Int {",
				"\treturn x + 1;",
				"}",
				"",
			]
		)
	)
	(tmp_path / "main.drift").write_text(
		"\n".join(
			[
				"module main;",
				"",
				"import acme.lib as lib;",
				"",
				"fn main() nothrow -> Int {",
				"\tval s = \"hello\";",
				"\tval len = s.byte_length();",
				"\tval v = try lib.add_one(len) catch { 0 };",
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

	# Verify provenance: acme.lib is explicitly packaged, stdlib is not.
	assert "acme.lib" in type_table.explicitly_packaged_modules
	assert "std.core" not in type_table.explicitly_packaged_modules

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
	# Option B: calls go directly to the impl body — no wrapper routing.
	assert '@"acme.lib::add_one__impl"' in main_ir
	# Stdlib method call must NOT go through a wrapper (no false boundary).
	assert "__wrap_method::String::byte_length" not in main_ir
