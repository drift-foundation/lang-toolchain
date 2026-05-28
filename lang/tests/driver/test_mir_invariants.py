# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.core.function_id import function_symbol
from lang.driftc.stage2 import mir_nodes as M


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_call_noncopy_moves_local(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module main;

struct Box { s: String }

fn take(x: Box) nothrow -> Int {
	return 1;
}

fn main() nothrow -> Int {
	var s = Box(s = "hi");
	var n = take(move s);
	return n;
}
""",
	)
	modules, type_table, _exc, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures_by_id, _fn_ids = flatten_modules(modules)
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures_by_id,
		module_exports=module_exports,
		module_deps=module_deps,
		type_table=type_table,
		prelude_enabled=True,
		return_checked=True,
	)
	assert not any(d.severity == "error" for d in checked.diagnostics)
	main_candidates = [
		fn_id
		for fn_id in mir_funcs.keys()
		if fn_id.name == "main" or function_symbol(fn_id).endswith("::main")
	]
	assert main_candidates, f"missing main in MIR funcs: {[function_symbol(fid) for fid in mir_funcs.keys()]}"
	main_id = main_candidates[0]
	func = mir_funcs[main_id]
	zero_vals: set[str] = set()
	store_zero_to_s = False
	call_seen = False
	for block in func.blocks.values():
		for instr in block.instructions:
			if isinstance(instr, M.ZeroValue):
				zero_vals.add(instr.dest)
			if isinstance(instr, M.StoreLocal) and instr.local.startswith("s"):
				if instr.value in zero_vals:
					store_zero_to_s = True
			if isinstance(instr, M.Call) and instr.fn_id.name == "take":
				call_seen = True
				break
		if call_seen:
			break
	assert store_zero_to_s
