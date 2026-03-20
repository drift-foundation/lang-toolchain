# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: pub fn with a parameter named 'entry' must not collide
with the nothrow wrapper's entry block label.

Bug: the wrapper emitted %entry as both a parameter name and the
entry block label.  LLVM rejects this because parameters and block
labels share the same value namespace within a function.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

STDLIB_ROOT = Path("stdlib")


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


class TestWrapperEntryParamCollision:

	def test_param_named_entry_compiles(self, tmp_path: Path) -> None:
		"""A pub fn with a parameter named 'entry' must produce valid IR."""
		mod_root = tmp_path / "mods"
		_write_file(mod_root / "main" / "main.drift", """
module main;

pub struct Item {
	pub value: Int
}

pub fn put(entry: Item) nothrow -> Int {
	return entry.value;
}

fn main() nothrow -> Int {
	val item = Item(value = 42);
	return put(item) - 42;
}
""".lstrip())
		ir_path = tmp_path / "out.ll"
		paths = sorted(mod_root.rglob("*.drift"))
		argv = [
			"-M", str(mod_root),
			*map(str, paths),
			"--stdlib-root", str(STDLIB_ROOT),
			"--emit-ir", str(ir_path),
		]
		rc = driftc_main(argv)
		assert rc == 0, f"driftc failed with exit code {rc}"

		ir = ir_path.read_text(encoding="utf-8")

		# The wrapper must have %entry as a param and a non-colliding block label.
		assert "%entry" in ir, "parameter %entry should appear in wrapper"

		# Verify llvm-as accepts the IR.
		llvm_as = shutil.which("llvm-as-20") or shutil.which("llvm-as")
		if llvm_as is None:
			pytest.skip("llvm-as not available")
		result = subprocess.run(
			[llvm_as, str(ir_path), "-o", "/dev/null"],
			capture_output=True, text=True,
		)
		assert result.returncode == 0, (
			f"llvm-as rejected IR with param named 'entry':\n{result.stderr}"
		)

	def test_param_named_entry_in_throwing_fn(self, tmp_path: Path) -> None:
		"""A throwing pub fn with 'entry' param must also produce valid IR."""
		self._compile_and_verify(tmp_path, """
module main;

pub fn process(entry: String) -> Int {
	return entry.byte_length();
}

fn main() nothrow -> Int {
	val r = try process("hello") catch { -1 };
	return r - 5;
}
""".lstrip())

	def test_params_named_like_internal_labels(self, tmp_path: Path) -> None:
		"""Params named like plausible internal labels must not collide."""
		self._compile_and_verify(tmp_path, """
module main;

pub struct Pair {
	pub a: Int,
	pub b: Int
}

pub fn compute(entry: Int, ok: Int, trap: Int, then: Int) nothrow -> Int {
	return entry + ok + trap + then;
}

pub fn process(loop: Pair, body: Int) nothrow -> Int {
	return loop.a + loop.b + body;
}

fn main() nothrow -> Int {
	val r1 = compute(1, 2, 3, 4);
	val p = Pair(a = 5, b = 6);
	val r2 = process(p, 7);
	return r1 + r2 - 28;
}
""".lstrip())

	def test_throwing_wrapper_with_internal_label_params(self, tmp_path: Path) -> None:
		"""Throwing pub fn wrapper with params named like internal labels."""
		self._compile_and_verify(tmp_path, """
module main;

pub fn danger(entry: String, ok: Int) -> Int {
	return entry.byte_length() + ok;
}

fn main() nothrow -> Int {
	val r = try danger("hi", 10) catch { -1 };
	return r - 12;
}
""".lstrip())

	def _compile_and_verify(self, tmp_path: Path, source: str) -> None:
		mod_root = tmp_path / "mods"
		_write_file(mod_root / "main" / "main.drift", source)
		ir_path = tmp_path / "out.ll"
		paths = sorted(mod_root.rglob("*.drift"))
		argv = [
			"-M", str(mod_root),
			*map(str, paths),
			"--stdlib-root", str(STDLIB_ROOT),
			"--emit-ir", str(ir_path),
		]
		rc = driftc_main(argv)
		assert rc == 0, f"driftc failed with exit code {rc}"

		llvm_as = shutil.which("llvm-as-20") or shutil.which("llvm-as")
		if llvm_as is None:
			pytest.skip("llvm-as not available")
		result = subprocess.run(
			[llvm_as, str(ir_path), "-o", "/dev/null"],
			capture_output=True, text=True,
		)
		assert result.returncode == 0, (
			f"llvm-as rejected IR:\n{result.stderr}"
		)
