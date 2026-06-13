# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Compile + run regression for `Optional<T>::unwrap_or`.

Background: the method was added in 2026-05-06 to land the
`e.params.get(k).as_int().unwrap_or(-1)` fluent-chain ergonomic
over `JsonCursor`.  The first user of the method
(`test_params_cursor_access_over_pub_error`) only exercises the
`Some(v)` branch: the cursor returns `Some(offset_int)` when the
key is present and the type matches, so `unwrap_or(-1)`'s fallback
arm is never hit.

This test exercises BOTH branches end-to-end:

  Optional<Int>::Some(41).unwrap_or(1) + Optional<Int>::None().unwrap_or(1) == 42

A `Some` arm yielding 41 plus a `None` arm yielding the fallback 1
sums to 42, which becomes the binary's exit code.  Pre-fix the
method didn't exist; post-fix the `None` arm has to actually
consume the `fallback` parameter and return it (and drop the
moved-but-unmatched `Some(v)` slot symmetrically).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


def _build_run(tmp_path: Path, source: str) -> tuple[int, str, str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			str(src),
			"--entry", "main::main",
			"-o", str(out_bin),
		],
		cwd=ROOT,
		capture_output=True,
		text=True,
		timeout=sanitizer_timeout(120),
		env=env,
	)
	if build.returncode != 0:
		return (build.returncode, build.stdout, build.stderr)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	return (run.returncode, run.stdout, run.stderr)


def test_optional_unwrap_or_covers_both_branches(tmp_path: Path) -> None:
	"""`Some(41).unwrap_or(1) + None().unwrap_or(1) == 42` —
	exit code 42 proves the Some(v) arm returns v AND the None arm
	returns the fallback parameter."""
	source = """
module main;

fn main() nothrow -> Int {
\tval s: Optional<Int> = Optional::Some(41);
\tval n: Optional<Int> = Optional::None();
\treturn s.unwrap_or(1) + n.unwrap_or(1);
}
"""
	rc, stdout, stderr = _build_run(tmp_path, source)
	assert rc == 42, (
		f"expected exit 42 (Some(41).unwrap_or(1) + None().unwrap_or(1)); "
		f"got rc={rc}\nstdout: {stdout!r}\nstderr: {stderr!r}"
	)
