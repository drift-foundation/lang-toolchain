# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: helper-path recursion-limit headroom (matrix row #4).

The row #4 fix has two halves:
1. stage1 iterative left-spine flattener (covered by
   `lang/tests/stage1/test_long_binary_chain.py`)
2. compile-pipeline recursion-limit bump in driftc.py — applied via the
   `_with_compile_recursion_headroom` decorator at every public compile entry
   point (`main`, `compile_stubbed_funcs`, `compile_to_llvm_ir_for_tests`)

The driver-level pipeline regression
(`lang/tests/driver/test_long_add_chain_pipeline.py`) only exercises the CLI
path through `main()`. This file pins the **library/helper path** by calling
`compile_stubbed_funcs` directly under `sys.setrecursionlimit(1000)` with a
deeply-chained binary expression — the helper's decorator must bump the
limit internally and restore it on exit, otherwise stage2's
`_visit_expr_HBinary` recursion will overflow.
"""
from __future__ import annotations

import sys
from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_long_add_chain_module(root: Path, n: int) -> None:
	"""Build `module main; pub fn main() nothrow -> Int { return 1+1+...+1; }`."""
	expr = "1" + "+1" * n
	src = root / "main.drift"
	src.parent.mkdir(parents=True, exist_ok=True)
	src.write_text(
		f"module main;\npub fn main() nothrow -> Int {{\n\treturn {expr};\n}}\n"
	)


def test_compile_stubbed_funcs_handles_deep_binary_chain_under_default_recursion_limit(tmp_path: Path) -> None:
	"""Library path: compile_stubbed_funcs must work with a deep HBinary tree.

	If the `_with_compile_recursion_headroom` decorator regresses (e.g.
	someone removes it from `compile_stubbed_funcs`), stage2's
	`_visit_expr_HBinary` will overflow Python's recursion stack on the
	resulting deeply-left-leaning tree under the default 1000-frame limit.

	Pinned with `sys.setrecursionlimit(1000)` so the test does not silently
	rely on a parent process having already bumped the limit.
	"""
	mod_root = tmp_path / "mods"
	# 700 chain elements: well past stage2's recursion ceiling (~250 source
	# levels at default 1000 frames) but well below the parser's
	# expression-nesting limit (256 nested parens). Long add chains are
	# left-leaning, not paren-nested, so the parser limit doesn't apply.
	_write_long_add_chain_module(mod_root, 700)

	# Parsing must happen *before* we drop the recursion limit because
	# `parse_program` itself bumps the limit internally for the duration of
	# parsing. After parsing returns, the limit is restored to whatever the
	# caller set, so we can reset it to a tight 1000 here and have the
	# subsequent `compile_stubbed_funcs` call exercise its own decorator.
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == [], f"unexpected parse diagnostics: {diagnostics}"
	func_hirs, sigs, _fn_ids_by_name = flatten_modules(modules)

	prev_limit = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		# This is the call under test. The decorator must bump the limit
		# inside the helper and restore it before returning.
		_mir_funcs, checked = compile_stubbed_funcs(
			func_hirs=func_hirs,
			signatures=sigs,
			exc_env=exc_catalog,
			type_table=type_table,
			module_exports=module_exports,
			module_deps=module_deps,
			return_checked=True,
		)
		# After the helper returns, the limit must be back to what we set.
		assert sys.getrecursionlimit() == 1000, (
			f"compile_stubbed_funcs leaked a recursion-limit change: "
			f"got {sys.getrecursionlimit()}, expected 1000"
		)
		# And the compile must have succeeded (no diagnostics for a simple
		# integer-add chain).
		assert checked.diagnostics == [], (
			f"unexpected diagnostics: {checked.diagnostics}"
		)
	finally:
		sys.setrecursionlimit(prev_limit)
