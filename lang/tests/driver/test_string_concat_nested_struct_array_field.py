# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression + bounded ownership matrix: heap `String` reached through
`arrayElem -> by-value struct field -> String field` fed to `+`.

CORE_BUG (drift-query): `fields[j].value.s + ""` where `fields: Array<Field>`,
`Field.value: Value`, `Value.s: String` (heap strings) FREED the array's live
buffer — values silently degraded across passes, then a later allocation aborted
(`malloc(): unaligned tcache chunk detected`).

Root cause (lowering): the `HField(HIndex)` fast path in
`stage2/hir_to_mir.py::_visit_expr_HField` borrowed an element's NON-Copy struct
field (`.value`) via AddrOf+LoadRef but returned it WITHOUT flagging it in
`_ref_field_temps`.  A subsequent projection (`.s`) off that unflagged struct then
hit the `source_is_owned_rvalue` path and emitted a spurious drop of the struct's
String — freeing the live array element (a double free).  Fix: flag the borrowed
non-bitcopy field read as a ref-field alias, mirroring the general field path.

This file is the bounded ownership matrix the `String ownership-authoring
conformance matrix` refactor trigger requires:
  - producers:    heap concat ("p"+fmt..) vs static literal control;
  - projections:  array element, nested struct field (1/2/3 hops), plain local
                  (no array), borrow-penultimate idiom;
  - consumer:     `+` concat vs direct read (no concat) control;
  - exit:         normal teardown (multi-pass so a freed buffer corrupts later
                  reads) + an explicit valgrind memcheck on the failing shape.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "test_bin"
	env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
	env["PYTHONPATH"] = str(ROOT)
	build = subprocess.run(
		[
			sys.executable, "-m", "lang.driftc.driftc",
			"--stdlib-root", str(ROOT / "stdlib"),
			str(src), "--entry", "m::main", "-o", str(out_bin),
		],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120), env=env,
	)
	assert build.returncode == 0, f"compile failed:\n{build.stderr}\n{build.stdout}"
	return out_bin


def _run(out_bin: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(30))


# ── Program templates ──────────────────────────────────────────────
#
# Every program prints its derived string once per element, for 3 passes,
# so a buffer freed on pass 0 shows up as wrong/garbled output (or an abort)
# on pass 1+.  Expected output is the same two lines repeated three times.

# The exact failing shape: array -> by-value struct -> String -> concat.
_FAILING = """
module m;
import std.console as console;
import std.format as fmt;
struct Value { s: String }
struct Field { value: Value }
pub fn main() nothrow -> Int {
	var fields: Array<Field> = [];
	var r = 0;
	while r < 2 { fields.push(Field(value = Value(s = "p" + fmt.format_int(r)))); r = r + 1; }
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < fields.len() { console.println(fields[j].value.s + "!"); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

# One hop: array -> struct field (direct) -> String -> concat.
_ONE_HOP = """
module m;
import std.console as console;
import std.format as fmt;
struct Flat { s: String }
pub fn main() nothrow -> Int {
	var fs: Array<Flat> = [];
	var r = 0;
	while r < 2 { fs.push(Flat(s = "p" + fmt.format_int(r))); r = r + 1; }
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < fs.len() { console.println(fs[j].s + "!"); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

# Three hops: array -> struct -> struct -> struct field -> String -> concat.
_THREE_HOP = """
module m;
import std.console as console;
import std.format as fmt;
struct Inner { s: String }
struct Mid { inner: Inner }
struct Outer { mid: Mid }
pub fn main() nothrow -> Int {
	var xs: Array<Outer> = [];
	var r = 0;
	while r < 2 { xs.push(Outer(mid = Mid(inner = Inner(s = "p" + fmt.format_int(r))))); r = r + 1; }
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < xs.len() { console.println(xs[j].mid.inner.s + "!"); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

# No array: plain local nested struct -> String -> concat.
_NO_ARRAY = """
module m;
import std.console as console;
struct Value { s: String }
struct Field { value: Value }
pub fn main() nothrow -> Int {
	val f0 = Field(value = Value(s = "p0"));
	val f1 = Field(value = Value(s = "p1"));
	var pass = 0;
	while pass < 3 {
		console.println(f0.value.s + "!");
		console.println(f1.value.s + "!");
		pass = pass + 1;
	}
	return 0;
}
"""

# Borrow-penultimate (the safe idiom): must still work.
_BORROW = """
module m;
import std.console as console;
import std.format as fmt;
struct Value { s: String }
struct Field { value: Value }
pub fn main() nothrow -> Int {
	var fields: Array<Field> = [];
	var r = 0;
	while r < 2 { fields.push(Field(value = Value(s = "p" + fmt.format_int(r)))); r = r + 1; }
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < fields.len() { val v = &fields[j].value; console.println(v.s + "!"); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

# Producer control: nested String is a STATIC LITERAL (not a heap concat).
_LITERAL = """
module m;
import std.console as console;
struct Value { s: String }
struct Field { value: Value }
pub fn main() nothrow -> Int {
	var fields: Array<Field> = [];
	fields.push(Field(value = Value(s = "p0")));
	fields.push(Field(value = Value(s = "p1")));
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < fields.len() { console.println(fields[j].value.s + "!"); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

# Consumer control: same projection but NO concat (direct read).
_NO_CONCAT = """
module m;
import std.console as console;
import std.format as fmt;
struct Value { s: String }
struct Field { value: Value }
pub fn main() nothrow -> Int {
	var fields: Array<Field> = [];
	var r = 0;
	while r < 2 { fields.push(Field(value = Value(s = "p" + fmt.format_int(r)))); r = r + 1; }
	var pass = 0;
	while pass < 3 {
		var j = 0;
		while j < fields.len() { console.println(fields[j].value.s); j = j + 1; }
		pass = pass + 1;
	}
	return 0;
}
"""

_CONCAT_OUT = "p0!\np1!\n" * 3
_PLAIN_OUT = "p0\np1\n" * 3

_MATRIX = [
	pytest.param(_FAILING, _CONCAT_OUT, id="array-nested-struct-string-concat (the bug)"),
	pytest.param(_ONE_HOP, _CONCAT_OUT, id="array-direct-field-string-concat"),
	pytest.param(_THREE_HOP, _CONCAT_OUT, id="array-3hop-nested-string-concat"),
	pytest.param(_NO_ARRAY, _CONCAT_OUT, id="plain-local-nested-string-concat"),
	pytest.param(_BORROW, _CONCAT_OUT, id="borrow-penultimate-struct (safe idiom)"),
	pytest.param(_LITERAL, _CONCAT_OUT, id="literal-string-producer-control"),
	pytest.param(_NO_CONCAT, _PLAIN_OUT, id="no-concat-direct-read-control"),
]


@pytest.mark.parametrize("source, expected", _MATRIX)
def test_string_ownership_matrix(tmp_path: Path, source: str, expected: str) -> None:
	"""Each shape builds heap records, projects the String, and reads it across
	three passes; a buffer freed by a spurious drop corrupts pass 1+ or aborts."""
	run = _run(_compile(tmp_path, source))
	assert run.returncode == 0, f"non-zero exit ({run.returncode}); stderr:\n{run.stderr}"
	assert run.stdout == expected, f"got {run.stdout!r}, expected {expected!r}"


@pytest.mark.skipif(asan_active(), reason="ASan shadow memory collides with valgrind")
def test_failing_shape_is_memcheck_clean(tmp_path: Path) -> None:
	"""The exact failing shape must be free of UAF + definite leaks under
	valgrind memcheck (the alloc-track exit the trigger asks for)."""
	if shutil.which("valgrind") is None:
		pytest.skip("valgrind not available")
	out_bin = _compile(tmp_path, _FAILING)
	proc = subprocess.run(
		valgrind_cmd(
			"--error-exitcode=99", "--leak-check=full",
			"--errors-for-leak-kinds=definite", "-q", str(out_bin),
		),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert proc.returncode == 0, f"valgrind reported errors:\n{proc.stderr}"
	assert proc.stdout == _CONCAT_OUT, f"got {proc.stdout!r}"
