import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_module_alias_exported_type_alias_struct_ctor_resolves(tmp_path: Path) -> None:
	other_src = tmp_path / "m_other.drift"
	api_src = tmp_path / "m_api.drift"
	main_src = tmp_path / "main.drift"
	other_src.write_text(textwrap.dedent(
		"""
		module m_other;
		pub struct Y {
			pub v: Int
		}
		export { Y };
		"""
	))
	api_src.write_text(textwrap.dedent(
		"""
		module m_api;
		import m_other as other;
		pub type X = other.Y;
		export { X };
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main;
		import m_api as api;
		pub fn main() nothrow -> Int {
			val x = api.X(v = 7);
			val _ = x;
			return 0;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[other_src, api_src, main_src],
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
