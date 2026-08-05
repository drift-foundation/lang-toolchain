# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Addendum probes (review-2026-08-04T21-48-05Z): transaction boundary +
value-position inventory for pending lambdas.

Work-only characterization.  The transaction probe compiles a source whose
generic-call argument is a call THROUGH a pending stored lambda and reads
`call_resolver._DEFER_PROBE_STATS` deltas in-process to establish whether a
`CheckerStateTxn` probe opens around the pending pre-resolution.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[2]

GENERIC_ARG_PENDING_CALL = """
module repro;

fn id<T>(x: T) nothrow -> T { return x; }

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val r = id(f());
	return r - 7;
}
"""

RETURN_PENDING = """
module repro;

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	return f;
}
"""

ARG_PENDING = """
module repro;

fn sink(x: Int) nothrow -> Int { return x; }

pub fn main() nothrow -> Int {
	val f = || => { 7 };
	return sink(f);
}
"""


def _driver(tmp: Path, name: str, source: str) -> None:
	d = tmp / name
	d.mkdir()
	src = d / "main.drift"
	src.write_text(source, encoding="utf-8")
	out = d / "out"
	cmd = [sys.executable, "-m", "lang.driftc.driftc", str(src), "--entry", "repro::main", "--target-word-bits", "64", "-o", str(out)]
	sr = stdlib_root()
	if sr is not None:
		cmd.extend(["--stdlib-root", str(sr)])
	r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
	print(f"\n=== {name}: build exit={r.returncode} ===")
	for line in (r.stdout + r.stderr).splitlines():
		if "error" in line:
			print(f"  {line}")
	if r.returncode == 0 and out.exists():
		run = subprocess.run([str(out)], capture_output=True, text=True, timeout=60)
		print(f"  run exit={run.returncode}")


def test_probe_value_position_inventory(tmp_path: Path) -> None:
	_driver(tmp_path, "return_pending", RETURN_PENDING)
	_driver(tmp_path, "arg_pending", ARG_PENDING)


def test_probe_txn_generic_arg_pending_call(tmp_path: Path) -> None:
	# In-process compile so _DEFER_PROBE_STATS deltas are observable.
	import lang.driftc.checker.call_resolver as CR
	from lang.driftc.driftc import compile_stubbed_funcs
	from lang.driftc.module_lowered import flatten_modules
	from lang.driftc.parser import parse_drift_workspace_to_hir

	src = tmp_path / "main.drift"
	src.write_text(GENERIC_ARG_PENDING_CALL, encoding="utf-8")
	modules, type_table, _exc, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		[src], module_paths=[tmp_path], stdlib_root=stdlib_root()
	)
	assert diagnostics == []
	func_hirs, signatures, _by_name = flatten_modules(modules)
	before = dict(CR._DEFER_PROBE_STATS)
	_mir, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	after = dict(CR._DEFER_PROBE_STATS)
	delta = {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}
	errs = [d.message for d in checked.diagnostics if d.severity == "error"]
	print("\n=== TXN PROBE (id(f()) with pending f) ===")
	print(f"  stats delta: {delta}")
	print(f"  errors: {errs}")
