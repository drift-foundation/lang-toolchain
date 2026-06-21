# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: `conc.yield_now()` (Phase 1, the only public concurrency primitive
shipping in this slice).

A VT that loops `yield_now()` lets a co-located peer VT make progress on the same
single cooperative worker, and does so FAST — proving it is the scheduler
relinquish (`thread.vt_yield`), not the `sleep(1ms)` floor (which would make N
iterations take ~N ms).

Over the existing `thread.vt_yield` intrinsic — no new runtime symbols, ABI 17.

(Single-fd `io.poll` was prototyped on this branch but intentionally NOT shipped —
the public readiness API will be the unified wait-set / multi-fd `poll_many`; see
`work/concurrent-server-primitives/F3-multifd-plan.md`.)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout, asan_active, valgrind_cmd

_VALGRIND_SKIP = pytest.mark.skipif(
	shutil.which("valgrind") is None or asan_active(),
	reason="valgrind requires a non-ASan binary (ASan shadow memory collides)",
)

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str, name: str = "bin") -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:900]}"
	assert out.exists()
	return out


def _run(binary: Path, timeout_s: int = 30) -> tuple[int, float]:
	t0 = time.monotonic()
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(timeout_s))
	return res.returncode, time.monotonic() - t0


# Worker increments a shared atomic N times, yielding each step. Main yields in a
# loop until the worker finishes, then asserts (in-program) it completed in well
# under the sleep(1ms) floor (N ms). Returns 0 on success.
_YIELD_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;
import lang.atomic as atomic;
import lang.thread as thread;

const N: Int = 2000;

pub fn main() nothrow -> Int {
\tvar shared = conc.arc(conc.atomic_int(0));
\tval start = thread.now_ms();
\tvar vt = conc.spawn(| | captures(share shared) => {
\t\tvar i = 0;
\t\twhile i < N {
\t\t\tval _ = shared.get().fetch_add(1, atomic.MemoryOrder::SeqCst());
\t\t\tconc.yield_now();
\t\t\ti = i + 1;
\t\t}
\t\treturn 0;
\t});
\tvar spins = 0;
\twhile shared.get().load(atomic.MemoryOrder::SeqCst()) < N {
\t\tconc.yield_now();
\t\tspins = spins + 1;
\t\tif spins > 100000000 { return 1; }
\t}
\tval _j = vt.join();
\tval elapsed = thread.now_ms() - start;
\t// Real relinquish completes N iterations in milliseconds; the sleep(1ms)
\t// floor would need >= N ms (~2s). Fail if it looks like the sleep path.
\tif elapsed > 500 { return 3; }
\treturn shared.get().load(atomic.MemoryOrder::SeqCst()) == N ? 0 : 2;
}
"""


def test_yield_now_hands_off_and_is_not_sleep(tmp_path: Path) -> None:
	rc, wall = _run(_compile(tmp_path, _YIELD_SOURCE, "yld"))
	assert rc == 0, f"yield_now handoff failed (rc={rc}; 3=looked like sleep path)"
	# 2000 iterations: the sleep(1ms) path would be ~2s wall; the relinquish path
	# is well under a second even with process startup.
	assert wall < 1.5, f"yield_now too slow ({wall:.2f}s) — likely a sleep path"


@_VALGRIND_SKIP
def test_yield_now_memcheck(tmp_path: Path) -> None:
	binary = _compile(tmp_path, _YIELD_SOURCE, "yld_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--fair-sched=yes", "--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:900]}"
	assert res.returncode == 0, f"program failed under valgrind: {res.stderr[:500]}"
