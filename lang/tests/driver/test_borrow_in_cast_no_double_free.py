# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""A `&local` borrow nested inside a `cast<…>(…)` must not double-free the local
(CORE_BUG / LANGUAGE_BUG).

`cast<mem.Ptr<Uint>>(io.buffer_ptr(&b))` — where `b` is a `var` Buffer (owns a
heap allocation, has a destructor) — double-freed `b` at scope exit:

    free(): double free detected in tcache 2     # exit 134

Root cause: the stage-1 place-canonicalizer (`place_canonicalize._rewrite_expr`)
and the borrow-materializer (`borrow_materialize._rewrite_expr`) had no `HCast`
case — they did not recurse into a cast operand.  So a borrow nested inside a
cast kept a bare `HVar` subject (instead of an `HPlaceExpr`), fell into the
HIR→MIR rvalue-borrow fallback, and the named local was materialized into a
`__borrow_tmp` AND dropped a second time at scope exit.  Binding the pointer to a
plain `val` first (no cast around the borrow-bearing call) was the reporter's
workaround and is unaffected.

Fix: both walkers now recurse into `HCast` (and `HResultOk`) operands.
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
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--target-word-bits", "64", "--stdlib-root", str(stdlib),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	return subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def test_cast_of_borrow_bearing_call_no_double_free(tmp_path: Path) -> None:
	"""`cast<mem.Ptr<Uint>>(io.buffer_ptr(&b))` runs clean (was a double-free /
	exit 134)."""
	src = """\
module main;
import std.io as io;
import std.mem as mem;
fn main() nothrow -> Int {
	unsafe {
		var b = io.buffer(8);
		val p = cast<mem.Ptr<Uint> >(io.buffer_ptr(&b));
	}
	return 0;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, (
		f"double-free / abort: rc={run.returncode}, stderr={run.stderr[-300:]}"
	)


def test_reporter_nested_cast_chain_no_double_free(tmp_path: Path) -> None:
	"""The reporter's exact nested shape — a `cast` chain with a deeply nested
	`io.buffer_ptr(&b)` — runs clean."""
	src = """\
module main;
import std.io as io;
import std.mem as mem;
import std.console as console;
fn create(p: mem.Ptr<Byte>) nothrow -> Int { return 0; }
fn main() nothrow -> Int {
	unsafe {
		var env_slot = io.buffer(8);
		val rc1 = create(io.buffer_ptr(&env_slot));
		val env = cast<RawPtr<Byte> >(mem.ptr_read<type Uint>(cast<mem.Ptr<Uint> >(io.buffer_ptr(&env_slot))));
		console.println("mapsize ok");
	}
	console.println("done");
	return 0;
}
"""
	run = _compile_and_run(tmp_path, src)
	assert run.returncode == 0, (
		f"double-free / abort: rc={run.returncode}, stderr={run.stderr[-300:]}"
	)
	assert "done" in run.stdout, run.stdout


def test_borrow_directly_bound_still_ok(tmp_path: Path) -> None:
	"""Control: binding the pointer to a `val` first (the reporter's workaround)
	still works — the place path was always correct here."""
	src = """\
module main;
import std.io as io;
import std.mem as mem;
fn main() nothrow -> Int {
	unsafe {
		var b = io.buffer(8);
		val sp = io.buffer_ptr(&b);
		val p = cast<mem.Ptr<Uint> >(sp);
	}
	return 0;
}
"""
	assert _compile_and_run(tmp_path, src).returncode == 0
