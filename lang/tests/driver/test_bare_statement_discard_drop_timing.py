# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Discard-timing contract behind the call-discard style rule
(doc/effective-drift.md "Discarding call results", 2026-07-23):

  * a BARE EXPRESSION STATEMENT discarding a non-Copy (Destructible)
    call result drops it IMMEDIATELY — its destructor runs before the
    next statement executes;
  * a `val _ = call()` discard binding EXTENDS the result's lifetime to
    scope exit — its destructor runs after the last statement of the
    scope.

The style sweep rewrote `val _ = call();` discards across examples/ and
docs to bare call statements; this regression pins the semantic
difference that rewrite relies on (and that the style rule tells users
to think about for ownership-bearing results such as guards).

Ordering is observed via console output from `Destructible.destroy`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]

SRC = """module main;

import std.console as console;
import std.core as core;

pub struct Noisy {
	tag: String
}

implement core.Destructible for Noisy {
	pub fn destroy(var self: Noisy) nothrow -> Void {
		console.println("destroy:" + self.tag);
	}
}

fn make(tag: String) nothrow -> Noisy {
	return Noisy(tag = move tag);
}

fn bare_statement_case() nothrow -> Void {
	make("bare");
	console.println("after-bare");
}

fn discard_binding_case() nothrow -> Void {
	val _ = make("bound");
	console.println("after-bound");
}

pub fn main() nothrow -> Int {
	bare_statement_case();
	discard_binding_case();
	return 0;
}
"""


def test_bare_statement_drops_immediately_binding_extends_to_scope_exit(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(SRC, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib), str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"{res.stdout}\n---\n{res.stderr[:2000]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True,
		timeout=sanitizer_timeout(60))
	assert run.returncode == 0, run.stderr[:500]
	lines = [l for l in run.stdout.splitlines() if l]
	assert lines == [
		"destroy:bare",   # bare statement: dropped BEFORE the next statement
		"after-bare",
		"after-bound",    # discard binding: still live here...
		"destroy:bound",  # ...dropped at scope exit
	], f"unexpected drop ordering:\n{run.stdout}"
