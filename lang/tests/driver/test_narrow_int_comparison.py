# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Same-width narrow-integer comparisons (`Uint32 == Uint32`, `Int32 == Int32`,
…) must produce `Bool`, not crash codegen (LANGUAGE_BUG).

`Uint32 == Uint32` (and other same-type narrow-int comparisons) raised an
internal Python traceback during codegen:

    NotImplementedError: LLVM codegen v1: integer binop requires matching
    Int/Uint operands (have i32, i32)

The integer-binop lowering only recognised `Int`/`Uint`/`Uint64`/`Byte`
operands, not `i32` (`Int32`/`Uint32`).  Comparisons of two same-width narrow
ints now lower to an `icmp` producing `Bool`, with CORRECT signedness: `Uint32`
ordering emits unsigned `icmp u*` and `Int32` ordering signed `icmp s*`
(equality is signedness-agnostic).  The LLVM `i32` type does not encode
signedness, so `BinaryOpInstr.signed` (set by HIR→MIR from the operand type)
carries it to codegen.  Narrow-int *arithmetic* remains out of scope.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--target-word-bits", "64",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def test_uint32_equality_produces_bool(tmp_path: Path) -> None:
	"""`Uint32 == Uint32` compiles and evaluates to the right `Bool`."""
	src = """\
module main;
fn main() nothrow -> Int {
	val a = cast<Uint32>(5);
	val b = cast<Uint32>(5);
	val c = cast<Uint32>(6);
	var r = 0;
	if a == b { r = r + 1; }
	if a == c { r = r + 10; }
	if a != c { r = r + 100; }
	return r;
}
"""
	# a==b true (+1), a==c false, a!=c true (+100) -> 101
	assert _compile_and_run(tmp_path, src).returncode == 101


def test_int32_equality_produces_bool(tmp_path: Path) -> None:
	"""`Int32 == Int32` compiles and evaluates correctly."""
	src = """\
module main;
fn main() nothrow -> Int {
	val a = cast<Int32>(-3);
	val b = cast<Int32>(-3);
	val c = cast<Int32>(4);
	var r = 0;
	if a == b { r = r + 1; }
	if a != c { r = r + 10; }
	return r;
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 11


def test_int32_ordering_signed(tmp_path: Path) -> None:
	"""`Int32` ordering uses SIGNED semantics: a negative value is less than a
	positive one (`icmp s*`)."""
	src = """\
module main;
fn main() nothrow -> Int {
	val a = cast<Int32>(-1);
	val b = cast<Int32>(2);
	var r = 0;
	if a < b { r = r + 1; }
	if b > a { r = r + 10; }
	if a <= a { r = r + 100; }
	if a > b { r = r + 1000; }
	return r;
}
"""
	# -1 < 2, 2 > -1, -1 <= -1, NOT(-1 > 2) -> 111
	assert _compile_and_run(tmp_path, src).returncode == 111


def test_uint32_ordering_unsigned_high_bit(tmp_path: Path) -> None:
	"""`Uint32` ordering uses UNSIGNED semantics: a high-bit value
	(`4294967295` = 0xFFFFFFFF) is GREATER than `1` (`icmp u*`).  Under the
	previous signed-best-effort lowering this was silently wrong (0xFFFFFFFF as
	signed i32 is -1, so `-1 > 1` would be false)."""
	src = """\
module main;
fn main() nothrow -> Int {
	val big = cast<Uint32>(4294967295u);
	val one = cast<Uint32>(1u);
	var r = 0;
	if big > one { r = r + 1; }
	if one < big { r = r + 10; }
	if big >= one { r = r + 100; }
	if big < one { r = r + 1000; }
	return r;
}
"""
	# unsigned: big > one, one < big, big >= one, NOT(big < one) -> 111
	assert _compile_and_run(tmp_path, src).returncode == 111


def test_uint32_high_bit_inverse_less_than(tmp_path: Path) -> None:
	"""Inverse `<` high-bit case: `1 < 4294967295` is true under unsigned
	semantics."""
	src = """\
module main;
fn main() nothrow -> Int {
	val big = cast<Uint32>(4294967295u);
	val one = cast<Uint32>(1u);
	if one < big { return 7; }
	return 0;
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 7
