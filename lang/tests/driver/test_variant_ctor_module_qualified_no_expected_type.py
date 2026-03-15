import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_module_qualified_zero_field_ctor_no_expected_type(tmp_path: Path) -> None:
	main_src = tmp_path / "main.drift"
	main_src.write_text(textwrap.dedent(
		"""
		module main;
		pub fn main() nothrow -> Int {
			val _op = std.err.IteratorOpId::Next();
			return 0;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[main_src],
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
