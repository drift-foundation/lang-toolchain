# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Explicitly-run red regressions for the hidden-lambda coercion finding.

This filename intentionally does not match pytest's default ``test_*.py``
pattern.  Run it by path while implementing the fix.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lang.driftc.stage2.mir_nodes as M
from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.core.function_id import function_symbol
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


ROOT = Path(__file__).resolve().parents[2]
REPRO = Path(__file__).with_name("repro_callback0_speaker_tail.drift")


def _parse_and_lower(tmp_path: Path):
	src = tmp_path / "main.drift"
	src.write_text(REPRO.read_text(encoding="utf-8"), encoding="utf-8")
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root()
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	return compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)


def test_block_tail_interface_coercion_reaches_hidden_mir(tmp_path: Path) -> None:
	mir_funcs, checked = _parse_and_lower(tmp_path)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], [d.message for d in errors]
	hidden = [
		func
		for fn_id, func in mir_funcs.items()
		if function_symbol(fn_id).split("::")[-1].startswith("__lambda_cb_")
	]
	assert len(hidden) == 1
	instructions = [instr for block in hidden[0].blocks.values() for instr in block.instructions]
	assert any(isinstance(instr, M.ConstructIfaceValue) for instr in instructions), instructions


def test_block_tail_interface_coercion_compiles_and_runs(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(REPRO.read_text(encoding="utf-8"), encoding="utf-8")
	out = tmp_path / "repro"
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", str(src),
		"--entry", "repro::main", "--target-word-bits", "64", "-o", str(out),
	]
	stdlib = stdlib_root()
	if stdlib is not None:
		cmd.extend(["--stdlib-root", str(stdlib)])
	build = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180))
	assert build.returncode == 0, build.stderr
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, run.stderr
