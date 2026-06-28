# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Indexing a closure-captured array (or a captured struct's array field) must
read the ENV slot, not the skipped capture local.

CORE_BUG #5 (drift-query M7.1c).  Moving an `Array<T>` into a callback and
reading `captured[i]...` inside the body aborted at runtime (SIGABRT, exit
134).  Root cause was NOT a destructor double-free and NOT element-field-count:
`_lower_addr_of_place` only redirects a place to the lambda env when ALL of the
place's projections are fields (`_capture_key_for_expr`).  A captured place with
an INDEX projection (`captured[i].field`) fell through to `AddrOfLocal` on the
capture local — which a MOVE/SHARE callback capture leaves UNINITIALIZED (the
prologue materializes nothing; reads route through the env) — so the array
header read as zero (len 0) and the bounds check aborted.  `captured.len()`
(field-only place) worked, which is why the loop condition passed but the body's
`captured[i]` aborted.

Fix: match the longest field-only prefix of the place against a capture slot,
seed addr/cur_ty from that env field, and apply the remaining (index/field)
projections.

Each `main` returns the COMPUTED value as its exit code, so a wrong value (or
the pre-fix abort=134) fails the assert — not just "compiled".
Reduced from the drift-query repro (triage evidence only; not a dependency).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import host_word_bits, sanitizer_timeout
from lang.driftc.parser import stdlib_root


def _compile_and_run(tmp_path: Path, source: str, *, expect_exit: int) -> None:
	src = tmp_path / "repro.drift"
	src.write_text(source.lstrip(), encoding="utf-8")
	out_bin = tmp_path / "bin"
	root_path = Path(__file__).resolve().parents[3]
	cmd = [
		sys.executable, "-m", "lang.driftc.driftc", "--dev",
		"--stdlib-root", str(stdlib_root() or (root_path / "stdlib")),
		"--target-word-bits", str(host_word_bits()),
		"--entry", "repro::main", "-o", str(out_bin), str(src),
	]
	res = subprocess.run(
		cmd, cwd=root_path, capture_output=True, text=True,
		timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed (rc={res.returncode}):\n{res.stderr[-1500:]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	# Pre-fix: SIGABRT (exit 134) from drift_bounds_check_fail.
	assert run.returncode == expect_exit, (
		f"binary exited {run.returncode}, expected {expect_exit} "
		f"(134 == the pre-fix bounds-check abort); stderr: {run.stderr[:300]}"
	)


_PREAMBLE = """
module repro;
import std.core as core;

fn b1(x: Int) nothrow -> Array<Byte> {
\tvar o: Array<Byte> = [];
\to.push(cast<Byte>(x));
\treturn move o;
}
fn drive(var cb: core.Callback1<Int, Int>) nothrow -> Int { return cb.call(0); }
"""


def test_captured_array_of_struct_indexed_field(tmp_path: Path) -> None:
	"""`captured[i].field` on a captured `Array<struct{2 owning fields}>`."""
	src = _PREAMBLE + """
pub struct KVB { key: Array<Byte>, value: Array<Byte> }
fn main() nothrow -> Int {
\tvar pairs: Array<KVB> = [];
\tpairs.push(KVB(key = b1(1), value = b1(11)));
\tpairs.push(KVB(key = b1(2), value = b1(22)));
\treturn drive(core.callback1(| z: Int | captures(move pairs) => {
\t\tvar s = 0; var i = 0;
\t\twhile i < pairs.len() { s = s + pairs[i].key.len() + pairs[i].value.len(); i = i + 1; }
\t\treturn s;
\t}));
}
"""
	_compile_and_run(tmp_path, src, expect_exit=4)  # 2 elems × (1+1)


def test_captured_array_of_struct_one_field(tmp_path: Path) -> None:
	"""Same shape with ONE owning field — pins 'not field-count dependent'."""
	src = _PREAMBLE + """
pub struct K1 { key: Array<Byte> }
fn main() nothrow -> Int {
\tvar pairs: Array<K1> = [];
\tpairs.push(K1(key = b1(1)));
\tpairs.push(K1(key = b1(2)));
\treturn drive(core.callback1(| z: Int | captures(move pairs) => {
\t\tvar s = 0; var i = 0;
\t\twhile i < pairs.len() { s = s + pairs[i].key.len(); i = i + 1; }
\t\treturn s;
\t}));
}
"""
	_compile_and_run(tmp_path, src, expect_exit=2)


def test_captured_array_of_array_indexed(tmp_path: Path) -> None:
	"""`captured[i]` on a captured `Array<Array<Byte>>` — also pins not-field-count."""
	src = _PREAMBLE + """
fn main() nothrow -> Int {
\tvar pairs: Array<Array<Byte> > = [];
\tpairs.push(b1(1)); pairs.push(b1(2)); pairs.push(b1(3));
\treturn drive(core.callback1(| z: Int | captures(move pairs) => {
\t\tvar s = 0; var i = 0;
\t\twhile i < pairs.len() { s = s + pairs[i].len(); i = i + 1; }
\t\treturn s;
\t}));
}
"""
	_compile_and_run(tmp_path, src, expect_exit=3)


def test_captured_struct_field_prefix_index(tmp_path: Path) -> None:
	"""`captured.a[i]` — the capture slot matches a FIELD PREFIX of the place."""
	src = _PREAMBLE + """
pub struct Holder { a: Array<Byte> }
fn main() nothrow -> Int {
\tvar h: Holder = Holder(a = b1(5));
\treturn drive(core.callback1(| z: Int | captures(move h) => {
\t\tvar s = 0; var i = 0;
\t\twhile i < h.a.len() { s = s + cast<Int>(h.a[i]); i = i + 1; }
\t\treturn s;
\t}));
}
"""
	_compile_and_run(tmp_path, src, expect_exit=5)


def test_captured_array_indexed_place_mutation(tmp_path: Path) -> None:
	"""`captured[i].field = v` — a MUTABLE indexed place on a captured array."""
	src = _PREAMBLE + """
pub struct KVB { key: Array<Byte>, value: Array<Byte> }
fn main() nothrow -> Int {
\tvar pairs: Array<KVB> = [];
\tpairs.push(KVB(key = b1(1), value = b1(2)));
\treturn drive(core.callback1(| z: Int | captures(move pairs) => {
\t\tvar k: Array<Byte> = []; k.push(cast<Byte>(7)); k.push(cast<Byte>(8)); k.push(cast<Byte>(9));
\t\tpairs[0].key = move k;
\t\treturn pairs[0].key.len() + pairs[0].value.len();
\t}));
}
"""
	_compile_and_run(tmp_path, src, expect_exit=4)  # new key len 3 + value len 1


def test_noncapture_indexed_place_control(tmp_path: Path) -> None:
	"""Control: the same indexed-place read on a plain LOCAL (no closure) is
	unaffected by the fix."""
	src = _PREAMBLE + """
pub struct KVB { key: Array<Byte>, value: Array<Byte> }
fn main() nothrow -> Int {
\tvar pairs: Array<KVB> = [];
\tpairs.push(KVB(key = b1(1), value = b1(2)));
\tpairs.push(KVB(key = b1(3), value = b1(4)));
\tvar s = 0; var i = 0;
\twhile i < pairs.len() { s = s + pairs[i].key.len() + pairs[i].value.len(); i = i + 1; }
\treturn s;
}
"""
	_compile_and_run(tmp_path, src, expect_exit=4)
