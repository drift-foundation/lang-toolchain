import textwrap
from pathlib import Path

from lang2.driftc.driftc import compile_stubbed_funcs, compile_to_llvm_ir_for_tests
from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _extract_lambda_ir(ir: str, marker: str) -> str:
	start = ir.find(marker)
	assert start != -1, f"missing lambda marker: {marker}"
	end = ir.find("\ndefine ", start + 1)
	if end == -1:
		end = len(ir)
	return ir[start:end]


def test_hidden_lambda_capture_locals_do_not_collide(tmp_path: Path) -> None:
	source = tmp_path / "main.drift"
	source.write_text(textwrap.dedent(
		"""
		module main

		import std.concurrent as conc;
		import std.core as core;

		pub fn main() nothrow -> Int {
			val port = 7;
			val _t = conc.spawn_cb(core.callback0(| | captures(copy port) => {
				if port == 7 {
					var buf = 1;
					return port + buf;
				} else {
					var buf2 = 2;
					return port + buf2;
				}
				return port;
			}));
			return 0;
		}
		"""
	))
	modules, table, excs, module_exports, module_deps, diags = parse_drift_workspace_to_hir(
		paths=[source],
		stdlib_root=stdlib_root(),
	)
	assert not diags
	func_hirs, signatures, _fn_ids = flatten_modules(modules)
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=excs,
		type_table=table,
		module_exports=module_exports,
		module_deps=module_deps,
		build_ssa=False,
		return_checked=True,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics)
	lambda_fn = None
	for fn_id, func in mir_funcs.items():
		if getattr(fn_id, "name", "") == "__lambda_cb_main_0_0":
			lambda_fn = func
			break
	assert lambda_fn is not None
	store_locals = []
	for block in lambda_fn.blocks.values():
		for instr in block.instructions:
			if instr.__class__.__name__ == "StoreLocal":
				store_locals.append(instr.local)
	assert store_locals.count("port") == 1
	assert any(local != "port" for local in store_locals)
