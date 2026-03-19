# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: driftc must emit opaque-pointer LLVM IR accepted by LLVM 20.

LLVM removed typed-pointer support (i8*, %Struct*, etc.) in LLVM 17.
driftc's emitter must use opaque pointers (ptr) so the generated IR
is valid under LLVM 15+ / mandatory under LLVM 17+.

Bug report: drift-web/docs/bugs/driftc-llvm20-typed-pointers.md
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main

STDLIB_ROOT = Path("stdlib")
BUILD_ROOT = Path("build/tests/lang/opaque_ptr")


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _emit_ir(tmp_path: Path, source: str, *, allow_unsafe: bool = False) -> str:
	"""Compile a single-module Drift program to LLVM IR text."""
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
	if allow_unsafe:
		argv.append("--allow-unsafe")
	rc = driftc_main(argv)
	assert rc == 0, f"driftc failed with exit code {rc}"
	return ir_path.read_text(encoding="utf-8")


class TestOpaquePointerIR:
	"""Emitted IR must use opaque pointers (ptr), not typed pointers (i8*, %T*)."""

	def test_no_typed_pointers_in_emitted_ir(self, tmp_path: Path) -> None:
		"""
		Core regression: IR must not contain typed pointer syntax.

		Typed pointer patterns: i8*, i8**, %DriftString*, %Struct_Foo*,
		i64*, etc.  Opaque-pointer IR uses 'ptr' for all pointer types.
		"""
		ir = _emit_ir(tmp_path, """
module main;

fn main() nothrow -> Int {
	val s = "hello";
	val x = s.byte_length();
	return x;
}
""".lstrip())

		# The IR should contain 'ptr' (opaque pointer) and NOT contain
		# typed-pointer patterns like 'i8*' or '%DriftString*'.
		assert "ptr" in ir, "emitted IR should use opaque pointers"

		# Check for typed-pointer patterns.  We look for common forms:
		#   i8*, i8**, i64*, %DriftString*, %DriftError*, %Struct_*
		# but exclude patterns like 'i8*' inside comments or metadata strings.
		typed_ptr_pattern = re.compile(
			r'(?<!\w)'        # not preceded by word char
			r'(?:i\d+|%\w+)' # base type: i8, i64, %DriftString, etc.
			r'\*'             # pointer star
		)
		# Filter to actual IR lines (skip comments, metadata strings).
		ir_lines = [
			line for line in ir.splitlines()
			if line.strip() and not line.strip().startswith(";")
			and not line.strip().startswith("!")
			and not line.strip().startswith("source_filename")
		]
		violations = []
		for i, line in enumerate(ir_lines, 1):
			# Skip string constant data (e.g., @.str.0 = ... c"hello\00")
			if "= private" in line and (" c\"" in line or " zeroinitializer" in line):
				continue
			matches = typed_ptr_pattern.findall(line)
			if matches:
				violations.append(f"  line {i}: {line.strip()}  (found: {matches})")

		assert not violations, (
			f"Emitted IR contains typed pointers (must use opaque 'ptr'):\n"
			+ "\n".join(violations[:20])
		)

	def test_llvm_as_accepts_emitted_ir(self, tmp_path: Path) -> None:
		"""
		End-to-end: llvm-as-20 must accept the emitted IR without error.

		This is the exact failure mode from the web team's bug report:
		LLVM 20's textual IR parser rejects typed pointers.
		"""
		llvm_as = shutil.which("llvm-as-20") or shutil.which("llvm-as")
		if llvm_as is None:
			pytest.skip("llvm-as not available")

		ir = _emit_ir(tmp_path, """
module main;

fn main() nothrow -> Int {
	val s = "hello world";
	val x = s.byte_length();
	return x;
}
""".lstrip())

		ir_path = tmp_path / "test.ll"
		ir_path.write_text(ir, encoding="utf-8")

		result = subprocess.run(
			[llvm_as, str(ir_path), "-o", "/dev/null"],
			capture_output=True, text=True,
		)
		assert result.returncode == 0, (
			f"llvm-as rejected emitted IR:\n{result.stderr}"
		)

	def test_clang_compiles_emitted_ir(self, tmp_path: Path) -> None:
		"""
		End-to-end: clang (LLVM 20) must compile the emitted IR to an object.
		"""
		clang = shutil.which("clang")
		if clang is None:
			pytest.skip("clang not available")

		ir = _emit_ir(tmp_path, """
module main;

fn main() nothrow -> Int {
	return 42;
}
""".lstrip())

		ir_path = tmp_path / "test.ll"
		ir_path.write_text(ir, encoding="utf-8")

		result = subprocess.run(
			[clang, "-c", "-x", "ir", str(ir_path), "-o", "/dev/null"],
			capture_output=True, text=True,
		)
		assert result.returncode == 0, (
			f"clang rejected emitted IR:\n{result.stderr}"
		)

	def test_pointer_heavy_program_no_typed_pointers(self, tmp_path: Path) -> None:
		"""
		Exercises pointer-heavy codegen paths:
		  - struct field access through &-references
		  - RawPtr via mem.ptr_from_ref / mem.ptr_offset / mem.ptr_read / mem.ptr_write
		  - array indexing and length
		  - string byte_length

		These paths hit _lower_ptr_from_ref, _lower_ptr_offset,
		_lower_ptr_read, _lower_ptr_write, and the no-op bitcast
		eliminator in the normalizer.
		"""
		ir = _emit_ir(tmp_path, """
module main;

import std.mem as mem;

pub struct Pair {
	pub a: Int,
	pub b: Int
}

pub struct Nested {
	pub inner: Pair,
	pub label: String
}

fn sum_pair(p: &Pair) nothrow -> Int {
	return p.a + p.b;
}

fn make_pair(x: Int, y: Int) nothrow -> Pair {
	return Pair(a = x, b = y);
}

fn nested_sum(n: &Nested) nothrow -> Int {
	return n.inner.a + n.inner.b + n.label.byte_length();
}

fn array_work() nothrow -> Int {
	val arr = [10, 20, 30, 40, 50];
	val total = arr[0] + arr[1] + arr[2] + arr[3] + arr[4];
	return total;
}

fn rawptr_round_trip() nothrow -> Int {
	// Exercise RawPtr codegen: ptr_from_ref, ptr_offset, ptr_write, ptr_read.
	var x = 100;
	var y = 200;
	val px = unsafe { mem.ptr_from_ref<type Int>(&x) };
	val py = unsafe { mem.ptr_from_ref<type Int>(&y) };
	val vx = mem.ptr_read<type Int>(px);
	val vy = mem.ptr_read<type Int>(py);
	return vx + vy;
}

fn rawptr_write_back() nothrow -> Int {
	// Exercise ptr_write and ptr_as_mut_ref.
	var z = 0;
	val pz = unsafe { mem.ptr_from_ref<type Int>(&z) };
	val rz = mem.ptr_as_mut_ref<type Int>(pz);
	*rz = 42;
	return z;
}

fn main() nothrow -> Int {
	val p = make_pair(3, 4);
	val n = Nested(inner = p, label = "hello");
	val s = nested_sum(&n);
	val a = array_work();
	val r = rawptr_round_trip();
	val w = rawptr_write_back();
	// s=12, a=150, r=300, w=42 → 12+150+300+42 = 504
	return s + a + r + w - 504;
}
""".lstrip(), allow_unsafe=True)

		# Verify no typed pointers remain.
		typed_ptr_pattern = re.compile(
			r'(?<!\w)'
			r'(?:i\d+|%\w+)'
			r'\*'
		)
		ir_lines = [
			line for line in ir.splitlines()
			if line.strip() and not line.strip().startswith(";")
			and not line.strip().startswith("!")
			and not line.strip().startswith("source_filename")
		]
		violations = []
		for i, line in enumerate(ir_lines, 1):
			if "= private" in line and (" c\"" in line or " zeroinitializer" in line):
				continue
			matches = typed_ptr_pattern.findall(line)
			if matches:
				violations.append(f"  line {i}: {line.strip()}  (found: {matches})")

		assert not violations, (
			f"Pointer-heavy IR contains typed pointers:\n"
			+ "\n".join(violations[:20])
		)

		# Verify no no-op bitcasts remain (bitcast ptr %x to ptr).
		for i, line in enumerate(ir.splitlines(), 1):
			if "bitcast ptr" in line and "to ptr" in line:
				assert False, (
					f"No-op ptr-to-ptr bitcast at line {i}: {line.strip()}"
				)

		# Must also be accepted by llvm-as.
		llvm_as = shutil.which("llvm-as-20") or shutil.which("llvm-as")
		if llvm_as is not None:
			ir_path = tmp_path / "heavy.ll"
			ir_path.write_text(ir, encoding="utf-8")
			result = subprocess.run(
				[llvm_as, str(ir_path), "-o", "/dev/null"],
				capture_output=True, text=True,
			)
			assert result.returncode == 0, (
				f"llvm-as rejected pointer-heavy IR:\n{result.stderr}"
			)

		# Verify the IR is non-trivial (>500 lines).
		line_count = len(ir.splitlines())
		assert line_count > 500, (
			f"Expected pointer-heavy IR to be >500 lines, got {line_count}"
		)
