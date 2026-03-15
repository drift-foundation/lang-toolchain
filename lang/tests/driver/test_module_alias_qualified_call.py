import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_module_alias_qualified_call_resolves(tmp_path: Path) -> None:
	util_src = tmp_path / "m_util.drift"
	main_src = tmp_path / "main.drift"
	util_src.write_text(textwrap.dedent(
		"""
		module m_util;
		pub fn add(a: Int, b: Int) nothrow -> Int {
			return a + b;
		}
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main;
		import m_util as util;
		pub fn main() nothrow -> Int {
			return util.add(2, 3);
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[util_src, main_src],
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
