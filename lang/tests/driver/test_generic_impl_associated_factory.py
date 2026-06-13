# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression for generic impl associated factory calls.

LANGUAGE_BUG (2026-05-24): inside `implement<T> Array<T>`, calling the
sibling associated factory `Array<T>::with_capacity(...)` inferred a
fresh `Array<T>` whose type variable owner did not match the current impl
body's `T`, so assignment to `var out: Array<T>` failed with
`E-AUTO-af256fe5`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


def test_array_generic_impl_can_call_sibling_associated_factory(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"""
module main;

import std.core as core;

implement<T> Array<T> {
	pub fn clone_via_factory(self: &Array<T>) nothrow -> Array<T> require T is core.Copy {
		var out: Array<T> = Array<T>::with_capacity(self.len);
		var i = 0;
		while i < self.len {
			out.push(self[i]);
			i = i + 1;
		}
		return move out;
	}
}

pub fn main() nothrow -> Int {
	var src: Array<Int> = [];
	src.push(10);
	src.push(32);
	val dup = src.clone_via_factory();
	if dup.len != 2 { return 1; }
	if dup[0] != 10 { return 2; }
	if dup[1] != 32 { return 3; }
	return 0;
}
""".lstrip(),
		encoding="utf-8",
	)
	out_bin = tmp_path / "out"
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
	assert build.returncode == 0, (
		"generic impl associated factory call should type-check and lower:\n"
		f"--- stdout ---\n{build.stdout}\n--- stderr ---\n{build.stderr}"
	)
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))
	assert run.returncode == 0, (
		f"program exit {run.returncode} indicates clone_via_factory semantic failure:\n"
		f"--- stdout ---\n{run.stdout}\n--- stderr ---\n{run.stderr}"
	)
