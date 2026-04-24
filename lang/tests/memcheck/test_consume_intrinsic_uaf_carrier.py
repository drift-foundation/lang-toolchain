# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Consume-via-intrinsic UAF regression — `mem.write` end-to-end carrier.

Pinned heap-use-after-free carrier for the same bug class that
`test_patch3_nested_scope_uaf_regression.py` pins for
`core.drop_value`.  The invariant being gated:
**Consume-via-intrinsic must materialize as `MoveOut` in MIR
before any ledger-authored cleanup path.**

Background.  Before the fix (2026-04-24), `mem.write(&mut buf, i, v)`
lowered the `v` arg with bare `lower_expr` (yielding
`LoadLocal + RawBufferWrite` with no MIR-level ownership
transition for the source local).  Patch-1 / patch-3
`cleanup_authoring` queried `verdict_at(v)` at scope exit, saw
state `LIVE`, returned `MUST_DROP`, and authored a redundant drop
— double-running the destructor.

This carrier uses a `Box` with a heap-touching destructor
(decrementing a shared `Arc<AtomicInt>` counter).  Under the
double-drop bug, the destructor fires twice on the same moved-from
storage; the second fire touches freed Arc storage and aborts with
`tcache_thread_shutdown(): unaligned tcache chunk detected`
(glibc), or surfaces as ASAN `heap-use-after-free in
drift_atomic_load_int`.  The variant with explicit `move b` on the
consume arg is included as a control: it lowers through
`_visit_expr_HMove` (which has always emitted `MoveOut`), so it
was never affected by the pre-fix bug.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


_SOURCE_TEMPLATE = """\
module main;

import std.concurrent as conc;
import std.core as core;
import std.mem as mem;
import lang.atomic as atomic;

pub struct Box {
\tpub n: Int,
\tpub counter: conc.Arc<atomic.AtomicInt>
}

implement core.Destructible for Box {
\tpub fn destroy(var self: Box) nothrow -> Void {
\t\tval _ = atomic.atomic_fetch_add_int(self.counter.get(), 1, 0);
\t\treturn;
\t}
}

fn run(counter: &conc.Arc<atomic.AtomicInt>) nothrow -> Void {
\tvar b = Box(n = 1, counter = counter.clone());
\tunsafe {
\t\tvar raw: mem.RawBuffer<Box> = mem.alloc_uninit<type Box>(1);
\t\tmem.write<type Box>(&mut raw, 0, __CONSUME_FORM__);
\t\tvar b2 = mem.read<type Box>(&mut raw, 0);
\t\tcore.drop_value<type Box>(b2);
\t\tmem.dealloc<type Box>(raw);
\t}
\treturn;
}

pub fn main() nothrow -> Int {
\tval counter = conc.arc(atomic.atomic_int(0));
\trun(&counter);
\treturn atomic.atomic_load_int(counter.get(), 0);
}
"""


def _compile_and_run(tmp_path: Path, consume_form: str) -> tuple[int, str]:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE_TEMPLATE.replace("__CONSUME_FORM__", consume_form))
	out_bin = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main",
		 "-o", str(out_bin),
		 "--allow-unsafe"],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=60)
	return run.returncode, run.stderr


def test_mem_write_implicit_consume_does_not_double_drop(tmp_path: Path) -> None:
	"""Implicit consume via `mem.write(&mut raw, 0, b)` (no `move`).
	Post-fix: destructor fires once, counter reads 1, exit=1.

	Regression signature: SIGABRT (rc=134) with
	`tcache_thread_shutdown(): unaligned tcache chunk detected`,
	indicating cleanup_authoring authored a redundant scope-exit
	drop of `b` because the `mem.write` lowering did not emit a
	`MoveOut`."""
	rc, stderr = _compile_and_run(tmp_path, "b")
	assert rc == 1, (
		f"consume-via-intrinsic UAF regression for `mem.write`: "
		f"expected rc=1 (destructor fires once, counter reads 1).  "
		f"Got rc={rc}, stderr={stderr[:300]!r}.  This indicates the "
		f"HIR→MIR lowering for `mem.write` regressed to bare "
		f"`LoadLocal + RawBufferWrite` without `MoveOut`, and "
		f"cleanup_authoring authored a redundant drop on the already-"
		f"consumed `b` local."
	)


def test_mem_write_explicit_move_consume_control(tmp_path: Path) -> None:
	"""Control case: `mem.write(&mut raw, 0, move b)` has always
	emitted `MoveOut` (through `_visit_expr_HMove`) and was never
	affected by the pre-fix bug.  Kept as a baseline — if this
	variant ever fails, the problem is upstream of the
	consume-via-intrinsic invariant (likely in `MoveOut` lowering
	or `cleanup_authoring` itself)."""
	rc, stderr = _compile_and_run(tmp_path, "move b")
	assert rc == 1, (
		f"baseline regression for explicit `move` at `mem.write`: "
		f"expected rc=1.  Got rc={rc}, stderr={stderr[:300]!r}.  This "
		f"is an upstream regression in either `MoveOut` lowering or "
		f"cleanup_authoring — NOT the consume-via-intrinsic invariant "
		f"(which only applies to the no-`move` form)."
	)
