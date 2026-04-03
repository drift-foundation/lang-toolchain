# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: generic wrapper instantiation in lambda callback bodies.

Lambda callbacks that call generic methods (Cell<T>::get, Arc<T>::borrow)
through boundary wrappers must have the wrapper MIR body synthesized.
This is a mainline bug in compile_stubbed_funcs — the generic drain
creates the wrapper signature but not the body.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _build_and_run_pkg_consumer(tmp_path: Path, source: str) -> tuple[int, str, str]:
	"""Build signed stdlib, compile consumer, run binary.

	Returns (exit_code, compile_stderr, run_stderr).
	"""
	from lang.tests.driver.test_pkg_map_literal_string_leak import _build_signed_stdlib, STD_VERSION
	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(source)
	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", f"std@{STD_VERSION}",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=180,
	)
	if res.returncode != 0:
		return res.returncode, res.stderr, ""
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	return run.returncode, res.stderr, run.stderr


def test_cell_get_in_lambda(tmp_path: Path) -> None:
	"""Cell<T>::get/set called from a lambda callback must compile and run."""
	source = """\
module consumer;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar count = core.cell(0);
\t(| | captures(count) => {
\t\tcount.set(count.get() + 1);
\t\treturn 0;
\t})();
\t(| | captures(count) => {
\t\tcount.set(count.get() + 1);
\t\treturn 0;
\t})();
\treturn count.get() - 2;
}
"""
	rc, compile_stderr, run_stderr = _build_and_run_pkg_consumer(tmp_path, source)
	assert "unknown call target" not in compile_stderr, (
		f"wrapper MIR body missing: {compile_stderr[:500]}"
	)
	assert rc == 0, f"exit {rc}, expected 0. compile: {compile_stderr[:300]}"


def test_cell_in_nested_lambda(tmp_path: Path) -> None:
	"""Cell<T> used in a nested lambda callback chain.

	Covers the nested-callback shape where wrapper instantiation must
	propagate through multiple lambda compilation rounds.
	"""
	source = """\
module consumer;
import std.core as core;
pub fn main() nothrow -> Int {
\tvar count = core.cell(0);
\tval outer = core.callback0(| | captures(count) nothrow => {
\t\tcount.set(count.get() + 10);
\t\treturn count.get();
\t});
\tval r = outer.call();
\treturn r - 10;
}
"""
	rc, compile_stderr, run_stderr = _build_and_run_pkg_consumer(tmp_path, source)
	assert "unknown call target" not in compile_stderr, (
		f"wrapper MIR body missing: {compile_stderr[:500]}"
	)
	assert rc == 0, f"exit {rc}, expected 0. compile: {compile_stderr[:300]}"
