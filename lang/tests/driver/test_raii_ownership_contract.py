# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Named RAII / ownership contract surface.

A small set of probes that pin the *promises* the Drift ownership
model makes to users, in language anyone reading a failure can
diagnose without scavenging across stage2 / memcheck / e2e / ledger
suites:

  1. **Scope-exit single destroy** — an owned local is destroyed
     exactly once when its scope ends.

  2. **Conditional-ownership guarded cleanup** — a value
     constructed only in a taken branch is destroyed exactly once;
     a value declared in a not-taken branch is never constructed
     and never destroyed.

  3. **Return transfers ownership** — `return move p` moves the
     value out of the callee; only the receiving scope destroys it
     (no leak, no double-drop).

  4. **Destructor side-effect timed at cleanup** — the destructor
     fires at the *owning scope's* exit, not at program exit and
     not at value construction.  Interleaved stdout writes pin the
     ordering.

  5. **Moved value not spuriously destroyed** — after `move p` into
     another function, the source slot is not destroyed again at
     the moving scope's exit.

Observation channel: each `Probe::destroy` writes a tagged line to
stdout via `thread.console_writeln`.  Tests assert deterministic
stdout content + exit code.  No valgrind here — leak-class drift is
caught orthogonally by `lang/tests/memcheck/`.  This file is the
*contract-legibility* surface; memcheck is the *byte-balance*
surface.

When a test in this file fails, the test name spells the broken
promise, and the assertion failure quotes the actual stdout vs.
expected.  No scavenging required.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout
from lang.driftc.parser import stdlib_root

ROOT = Path(__file__).resolve().parents[3]


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
	"""Compile `source` and run the produced binary; return the run result.
	Asserts the compile succeeded.
	"""
	src = tmp_path / "main.drift"
	src.write_text(source, encoding="utf-8")
	out_bin = tmp_path / "bin"
	stdlib = stdlib_root() or (ROOT / "stdlib")
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(stdlib),
		 str(src), "--entry", "m::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[:1500]}"
	assert out_bin.exists(), "binary not produced"
	return subprocess.run(
		[str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20),
	)


# ---------------------------------------------------------------------------
# Promise 1: scope-exit destroys an owned local exactly once.
# ---------------------------------------------------------------------------


def test_scope_exit_destroys_owned_exactly_once(tmp_path: Path) -> None:
	"""Owned local `p` declared in `main`'s scope is destroyed once
	at scope exit.  Failure signals: 0 drops (destructor never
	fires) → cleanup-authoring gap; >1 drops → double-drop or
	tombstone discipline regression.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module m;

import std.core as core;
import lang.thread as thread;

struct Probe { tag: Int }

implement core.Destructible for Probe {
	pub fn destroy(var self: Probe) nothrow -> Void {
		thread.console_writeln("DROP");
		return;
	}
}

pub fn main() nothrow -> Int {
	val p = Probe(tag = 1);
	val _t = p.tag;
	return 0;
}
""".lstrip(),
	)
	assert run.returncode == 0, f"exit={run.returncode}, stderr={run.stderr[:200]}"
	assert run.stdout == "DROP\n", (
		f"contract: owned local must drop exactly once at scope exit.\n"
		f"expected stdout: 'DROP\\n'\n"
		f"actual stdout:   {run.stdout!r}\n"
		f"(0 drops = cleanup-authoring gap; >1 = double-drop)"
	)


# ---------------------------------------------------------------------------
# Promise 2: conditional ownership uses guarded cleanup correctly.
# A value constructed in a taken branch is dropped exactly once;
# a value declared in a not-taken branch is never constructed → never dropped.
# ---------------------------------------------------------------------------


def test_conditional_ownership_uses_guarded_cleanup(tmp_path: Path) -> None:
	"""Only the taken branch's owned value gets destroyed.
	Failure signals: extra `B_DROP` → cleanup fired on a never-
	constructed value (path-insensitive `_moved_locals` regression);
	missing `A_DROP` → guarded-cleanup discipline regression.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module m;

import std.core as core;
import lang.thread as thread;

struct A { tag: Int }
struct B { tag: Int }

implement core.Destructible for A {
	pub fn destroy(var self: A) nothrow -> Void {
		thread.console_writeln("A_DROP");
		return;
	}
}

implement core.Destructible for B {
	pub fn destroy(var self: B) nothrow -> Void {
		thread.console_writeln("B_DROP");
		return;
	}
}

pub fn main() nothrow -> Int {
	val which = 1;
	if which == 1 {
		val a = A(tag = 1);
		val _ = a.tag;
	} else {
		val b = B(tag = 2);
		val _ = b.tag;
	}
	return 0;
}
""".lstrip(),
	)
	assert run.returncode == 0, f"exit={run.returncode}, stderr={run.stderr[:200]}"
	assert run.stdout == "A_DROP\n", (
		f"contract: only the taken branch's value drops; not-taken branch's "
		f"value is never constructed.\n"
		f"expected stdout: 'A_DROP\\n'\n"
		f"actual stdout:   {run.stdout!r}\n"
		f"('B_DROP' present = cleanup on never-constructed value; "
		f"missing 'A_DROP' = guarded-cleanup gap)"
	)


# ---------------------------------------------------------------------------
# Promise 3: return transfers ownership — no leak, no double-drop.
# ---------------------------------------------------------------------------


def test_return_transfers_ownership_no_leak_no_double_drop(tmp_path: Path) -> None:
	"""`return move p` from `make()` hands `p` to the caller; the
	caller's scope is the unique destruction site.  Failure
	signals: 0 drops → ownership leaked across the return; 2 drops
	→ both producer and consumer destroyed (return-as-move gap).
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module m;

import std.core as core;
import lang.thread as thread;

struct Probe { tag: Int }

implement core.Destructible for Probe {
	pub fn destroy(var self: Probe) nothrow -> Void {
		thread.console_writeln("DROP");
		return;
	}
}

fn make() nothrow -> Probe {
	val p = Probe(tag = 99);
	return move p;
}

pub fn main() nothrow -> Int {
	val received = make();
	val _ = received.tag;
	return 0;
}
""".lstrip(),
	)
	assert run.returncode == 0, f"exit={run.returncode}, stderr={run.stderr[:200]}"
	assert run.stdout == "DROP\n", (
		f"contract: return moves ownership; destroyed exactly once at "
		f"the receiving scope.\n"
		f"expected stdout: 'DROP\\n'\n"
		f"actual stdout:   {run.stdout!r}\n"
		f"(empty = leak across return; 'DROP\\nDROP\\n' = double-drop)"
	)


# ---------------------------------------------------------------------------
# Promise 4: destructor side effects fire at the *owning scope* exit,
# not at value construction and not at program exit.
# ---------------------------------------------------------------------------


def test_destructor_side_effects_fire_at_cleanup_point(tmp_path: Path) -> None:
	"""The drop happens between `STEP_3_INSIDE` and
	`STEP_4_AFTER_INNER` — at the END of `inner()`'s scope, not
	at construction (would show before STEP_3) and not at program
	exit (would show after STEP_4).  Failure signals: drop in the
	wrong position → cleanup-authoring placement bug.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module m;

import std.core as core;
import lang.thread as thread;

struct Probe { tag: Int }

implement core.Destructible for Probe {
	pub fn destroy(var self: Probe) nothrow -> Void {
		thread.console_writeln("DROP");
		return;
	}
}

fn inner() nothrow -> Void {
	thread.console_writeln("STEP_2_INSIDE");
	val p = Probe(tag = 1);
	val _ = p.tag;
	thread.console_writeln("STEP_3_INSIDE");
	return;
}

pub fn main() nothrow -> Int {
	thread.console_writeln("STEP_1_BEFORE_INNER");
	inner();
	thread.console_writeln("STEP_4_AFTER_INNER");
	return 0;
}
""".lstrip(),
	)
	expected = (
		"STEP_1_BEFORE_INNER\n"
		"STEP_2_INSIDE\n"
		"STEP_3_INSIDE\n"
		"DROP\n"
		"STEP_4_AFTER_INNER\n"
	)
	assert run.returncode == 0, f"exit={run.returncode}, stderr={run.stderr[:200]}"
	assert run.stdout == expected, (
		f"contract: destructor fires at the *owning scope's* exit, "
		f"between the last in-scope statement and the first out-of-scope one.\n"
		f"expected stdout:\n{expected}\n"
		f"actual stdout:\n{run.stdout}"
	)


# ---------------------------------------------------------------------------
# Promise 5: moved value is not destroyed at the source scope.
# `move p` into another fn → only the destination destroys.
# ---------------------------------------------------------------------------


def test_moved_value_not_spuriously_destroyed_at_source(tmp_path: Path) -> None:
	"""After `consume(move b)`, `b`'s slot in `main` is tombstoned
	and must not destroy at main's scope exit.  Probe `a` (never
	moved) destroys normally.  Expected order: `B_DROP` from
	`consume`'s scope exit; then `A_DROP` from `main`'s scope exit.
	Failure signals: `B_DROP` twice → moved-slot still destroyed
	at source; `B_DROP` missing → consumed-by-move not destroyed
	at destination either.
	"""
	run = _compile_and_run(
		tmp_path,
		"""
module m;

import std.core as core;
import lang.thread as thread;

struct Probe { tag: String }

implement core.Destructible for Probe {
	pub fn destroy(var self: Probe) nothrow -> Void {
		thread.console_writeln(self.tag);
		return;
	}
}

fn consume(p: Probe) nothrow -> Void {
	val _ = p.tag.byte_length();
	return;
}

pub fn main() nothrow -> Int {
	val a = Probe(tag = "A_DROP");
	val b = Probe(tag = "B_DROP");
	consume(move b);
	val _ = a.tag.byte_length();
	return 0;
}
""".lstrip(),
	)
	expected = "B_DROP\nA_DROP\n"
	assert run.returncode == 0, f"exit={run.returncode}, stderr={run.stderr[:200]}"
	assert run.stdout == expected, (
		f"contract: a moved value is destroyed exactly once, at the "
		f"destination scope; the source slot is tombstoned and not "
		f"re-destroyed.\n"
		f"expected stdout:\n{expected}\n"
		f"actual stdout:\n{run.stdout}\n"
		f"(B_DROP twice = moved-slot still destroyed at source; "
		f"B_DROP missing = move drained both endpoints)"
	)
