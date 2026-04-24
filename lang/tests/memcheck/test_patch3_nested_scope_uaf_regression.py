# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Patch 3 nested-scope migration regression — fat-Arc-interface-view UAF.

Historical pin for the heap-use-after-free that surfaced during the
first patch-3 attempt (2026-04-23) when `lower_block` end-of-block
fall-through cleanup was migrated to the `M.CleanupHook` +
`cleanup_authoring` post-pass pattern.  Diagnosis: the
`core.drop_value(local)` intrinsic was lowered as `LoadLocal +
DropValue` (no `MoveOut`), so the ledger saw `inner` as still LIVE
after consumption; `cleanup_authoring`'s `verdict_at` query returned
`MUST_DROP` and authored a redundant scope-exit drop, double-running
`AppService::destroy` and over-decrementing the counter Arc.  Full
analysis in `work/ownership-ledger/patch-3-diagnosis.md`.

Patch 3 landed (2026-04-24) once the HIR→MIR `core.drop_value`
lowering was fixed to emit `MoveOut + DropValue` for non-Copy local
args (pinned by `lang/tests/stage2/test_drop_value_intrinsic_ownership.py`).
This carrier is now the end-to-end gate proving that nested-scope
cleanup re-authoring does not double-drop a destructible inside a
fat `Arc<Interface>` view.  A regression in either the lowering or
the cleanup-authoring pass would resurface as SIGABRT here.

Crash signature for a regression: glibc `tcache_thread_shutdown():
unaligned tcache chunk detected` (rc=134), or ASAN
`heap-use-after-free in drift_atomic_load_int`.

Minimization: the smallest source that reproduced the original
patch-3 crash.  Removing the interface view OR the method call
through the interface eliminates the crash.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SOURCE = """\
module main;

import std.concurrent as conc;
import std.core as core;
import lang.atomic as atomic;

pub interface I1 { fn m1(self: &Self) nothrow -> Int; }

pub struct AppService {
\tpub n: Int,
\tpub counter: conc.Arc<atomic.AtomicInt>
}

implement I1 for AppService { pub fn m1(self: &AppService) nothrow -> Int { return self.n; } }

implement core.Destructible for AppService {
\tpub fn destroy(var self: AppService) nothrow -> Void {
\t\tval _ = atomic.atomic_fetch_add_int(self.counter.get(), 1, 0);
\t\treturn;
\t}
}

fn run(counter: &conc.Arc<atomic.AtomicInt>) nothrow -> Void {
\tval arc = conc.arc(AppService(n = 1, counter = counter.clone()));
\tval v1 = arc.as_interface<type I1>();
\tval _ = v1.get().m1();
\treturn;
}

pub fn main() nothrow -> Int {
\tval counter = conc.arc(atomic.atomic_int(0));
\trun(&counter);
\treturn atomic.atomic_load_int(counter.get(), 0);
}
"""


def test_patch3_nested_scope_fat_iface_uaf_carrier(tmp_path: Path) -> None:
	"""Compile + run the minimal nested-scope-cleanup carrier; assert
	no SIGABRT.  The destructor fires once and writes 1 to the
	counter, then `drift_main` reads counter (returns 1).

	Expected: exit=1 (success) under landed patch 3.

	Regression signature: SIGABRT (rc=134) from heap-use-after-free
	on counter Arc storage, indicating either (a) the
	`core.drop_value` HIR→MIR lowering regressed back to bare
	`LoadLocal + DropValue` so the ledger no longer sees the
	consumption, or (b) `cleanup_authoring` authored a redundant
	scope-exit drop for an already-consumed local."""
	src = tmp_path / "main.drift"
	src.write_text(SOURCE)
	out_bin = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	assert out_bin.exists()
	run_res = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=30)
	assert run_res.returncode == 1, (
		f"patch-3 nested-scope migration regression: expected the "
		f"destructor to fire exactly once (rc=1), got rc={run_res.returncode}\n"
		f"stdout={run_res.stdout!r}\n"
		f"stderr={run_res.stderr!r}\n"
		f"\n"
		f"This is the fat-Arc-interface-view UAF carrier from\n"
		f"`work/ownership-ledger/patch-3-diagnosis.md`.  Crash signature\n"
		f"under patch 3: `tcache_thread_shutdown(): unaligned tcache\n"
		f"chunk detected` (rc=134) or ASAN heap-use-after-free in\n"
		f"`drift_atomic_load_int`."
	)
