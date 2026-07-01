import textwrap
from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def test_array_push_cross_module_variant_arg_does_not_require_unknown_param(tmp_path: Path) -> None:
	rpc_src = tmp_path / "rpc.drift"
	main_src = tmp_path / "main.drift"
	rpc_src.write_text(textwrap.dedent(
		"""
		module mariadb.rpc;
		pub variant RpcArg { Int(value: Int) }
		pub fn arg_int(v: Int) nothrow -> RpcArg { return RpcArg::Int(v); }
		pub fn new_args() nothrow -> Array<RpcArg> { var args: Array<RpcArg> = []; return move args; }
		export { RpcArg, arg_int, new_args };
		"""
	))
	main_src.write_text(textwrap.dedent(
		"""
		module main;
		import mariadb.rpc as rpc;
		pub fn main() nothrow -> Int {
			var args: Array<rpc.RpcArg> = [];
			args.push(rpc.arg_int(1));
			args.push(rpc.arg_int(2));
			return 0;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[rpc_src, main_src],
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
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert not errors, errors
	assert "define i64 @main(" in ir
