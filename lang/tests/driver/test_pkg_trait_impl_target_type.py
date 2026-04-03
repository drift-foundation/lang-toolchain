# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: trait impl method on generic struct instantiation must resolve
through the package-consumer path.

The impl_target_type_id on trait impl method signatures must be the
concrete instantiation (ArrayRange<Int>), not the generic base (ArrayRange).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _build_and_compile_pkg_consumer(tmp_path: Path, source: str) -> tuple[int, str]:
	"""Build signed stdlib, compile consumer. Returns (returncode, stderr)."""
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
		return res.returncode, res.stderr
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=10)
	return run.returncode, res.stderr


def test_array_range_len(tmp_path: Path) -> None:
	"""ArrayRange<Int>::len must resolve through package-consumer path."""
	source = """\
module consumer;
import std.iter as iter;
pub fn main() nothrow -> Int {
\tvar arr: Array<Int> = [3, 1, 4, 1, 5];
\tvar r = arr.range();
\tval n = try iter.RandomAccessReadable::len(&r) catch { 99 };
\treturn n - 5;
}
"""
	rc, stderr = _build_and_compile_pkg_consumer(tmp_path, source)
	assert "no matching method" not in stderr, (
		f"impl_target_type_id missing type args: {stderr[:300]}"
	)
	assert rc == 0, f"exit {rc}: {stderr[:300]}"
