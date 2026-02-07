import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_entrypoint_falls_back_to_unique_main_module(tmp_path: Path) -> None:
	src = tmp_path / "foo.drift"
	src.write_text(textwrap.dedent(
		"""
		module foo
		pub fn main(argv: Array<String>) nothrow -> Int {
			return 0;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, _diags = parse_drift_workspace_to_hir(
		paths=[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
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
	assert "define i32 @main(" in ir
