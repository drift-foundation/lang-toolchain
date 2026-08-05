# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Pending-lambda alias matrix probes (review-2026-08-04T21-46-28Z items 1-6).

Work-only characterization: full driver compiles recording exit status and
the COMPLETE ordered diagnostic stream.  Loose assertions by design; the
in-tree red/green contracts are proposed separately in PROGRESS.md.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[2]

CASES = {
	# 1. Captureless inferable alias, full compile/run.
	"captureless_inferable_alias": """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val g = f;
	return g() - 7;
}
""",
	# 2. Contextual alias: v1 has NO bare fn-type local annotation; the
	#    nearest contextual container is core.Callback1.  Record behavior.
	"contextual_callback_alias": """
module repro;
import std.core as core;
pub fn main() nothrow -> Int {
	val f = | x: Int | => x;
	val g: core.Callback1<Int, Int> = f;
	return g.call(3) - 3;
}
""",
	# 3. Unconstrained alias of an unannotated-param lambda.
	"unconstrained_alias": """
module repro;
pub fn main() nothrow -> Int {
	val f = | x | => x;
	val g = f;
	return 0;
}
""",
	# 4. Resolve-after-alias / stale state.
	"resolve_after_alias": """
module repro;
pub fn main() nothrow -> Int {
	val f = || => { 7 };
	val g = f;
	val a = f();
	return a + g() - 14;
}
""",
	# 5. Ordinary non-lambda causal producer.
	"nonlambda_causal_producer": """
module repro;
pub fn main() nothrow -> Int {
	val bad = missing_name;
	bad();
	return 0;
}
""",
	# 6. Explicit-capture alias companion.
	"explicit_capture_alias": """
module repro;
pub fn main() nothrow -> Int {
	val x = 1;
	val f = | | captures(copy x) nothrow => { x };
	val alias = f;
	return 0;
}
""",
}


def _run_case(tmp: Path, name: str, source: str) -> None:
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
		if "error" in line or "warning" in line:
			print(f"  {line}")
	if r.returncode == 0 and out.exists():
		run = subprocess.run([str(out)], capture_output=True, text=True, timeout=60)
		print(f"  run exit={run.returncode}")


def test_probe_pending_alias_matrix(tmp_path: Path) -> None:
	for name, source in CASES.items():
		_run_case(tmp_path, name, source)
