import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_alias_return_assigns_into_struct_field_without_mismatch(tmp_path: Path) -> None:
	wire_types_src = tmp_path / "wire_types.drift"
	wire_src = tmp_path / "wire.drift"
	main_src = tmp_path / "main.drift"
	wire_types_src.write_text(textwrap.dedent(
		"""
		module wire.types
		pub struct S { pub x: Int }
		export { S };
		"""
	))
	wire_src.write_text(textwrap.dedent(
		"""
		module wire
		import wire.types as types;
		pub type S = types.S;
		export { S, mk };
		pub fn mk() nothrow -> S { return types.S(x = 1); }
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main
		import wire as wire;
		struct C { pub s: wire.S }
		pub fn main() nothrow -> Int {
			val s = wire.mk();
			val c = C(s = s);
			return c.s.x;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[wire_types_src, wire_src, main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		entry="main",
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics)
	assert "define i64 @main(" in ir


def test_alias_to_missing_nominal_reports_user_diagnostic_not_internal(tmp_path: Path) -> None:
	wire_src = tmp_path / "wire.drift"
	main_src = tmp_path / "main.drift"
	wire_src.write_text(textwrap.dedent(
		"""
		module wire
		pub type S = missing.Nope;
		export { S };
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main
		import wire as wire;
		struct C { pub s: wire.S }
		pub fn main() nothrow -> Int { return 0; }
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[wire_src, main_src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert any(d.severity == "error" for d in diags), diags
	assert not any(d.message.startswith("internal:") for d in diags), diags
