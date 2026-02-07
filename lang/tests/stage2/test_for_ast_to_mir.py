# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Integration test: AST for-loop → HIR → MIR CFG sanity.

Checks that the iterator-based for desugaring produces a well-formed MIR CFG
with proper terminators on all blocks.
"""

from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_for_ast_lowered_to_mir_cfg(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(
		src,
		"""
module m_main

	fn main() nothrow -> Int {
		val xs = [1, 2, 3];
		for x in xs { x; }
		return 0;
	}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, fn_ids_by_name = flatten_modules(modules)
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert checked.diagnostics == []
	main_ids = fn_ids_by_name.get("main") or fn_ids_by_name.get("m_main::main")
	assert main_ids is not None
	fn_id = main_ids[0]
	func = mir_funcs[fn_id]
	assert func.blocks
	assert func.blocks[func.entry].terminator is not None
	assert any(bname.startswith("loop_") for bname in func.blocks)
