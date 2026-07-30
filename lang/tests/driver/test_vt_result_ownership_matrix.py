# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regressions for the `VirtualThread<T>` result-ownership
LANGUAGE_BUG (R2-R8 — closed at 0.33.1).

Sibling to `test_vt_drop_started_running_uaf.py`, which pins R1
(started-running drop UAF, the originally-reported defect).  R2-R4
were uncovered by design review (2026-05-26) of plan §5 fix shapes;
R5 mirrors R4 in `join_timeout`; R6-R8 are the adjacent
`FutureGroup.join_any` cluster surfaced during implementation
review.  All eight share the same touched file
(`stdlib/std/concurrent/concurrent.drift`).  The landed fix at
0.33.1 closes the whole matrix; these tests pin each case so a
future regression in the same path fails the suite.

Cases:

  R2 — submit-error drop double-free.  `spawn` / `spawn_cb` deallocate
       the result buffer on the submit-error path and return a VT with
       `submit_error != 0` and `handle == 0`.  When that VT is dropped
       without `.join()`, `Destructible::destroy`
       (`stdlib/std/concurrent/concurrent.drift:1442`) deallocates the
       same buffer a second time.

  R3 — completed-unjoined drop leaks the unobserved owned result.
       Dropping a `VirtualThread<String>` whose cb has already
       published its result calls `mem.dealloc<T>(buf)` (layout-only —
       lowers to `drift_free_array(ptr)` in
       `lang/codegen/llvm/llvm_codegen.py:9956`).  T's `Destructible`
       never runs on the buf contents; any heap-owning state inside
       the unobserved `T` (e.g. a heap-allocated String body) leaks.

  R4 — cancel-then-publish join-CANCELLED leaks the discarded result.
       After `cancel()` succeeds (cb is parked, vt_cancel returns 0,
       self.cancelled = true), the cb may still wake, run past the
       cancel safe-point, and publish a `T`.  `join()` then takes the
       cancellation branch
       (`stdlib/std/concurrent/concurrent.drift:1314`) which
       deallocates the buffer WITHOUT reading the published `T` —
       leaks any heap-owning state.

  R5 — same as R4 but via `join_timeout()` instead of `join()`.
       The cancellation branch of `join_timeout`
       (`stdlib/std/concurrent/concurrent.drift:1374-1385`)
       duplicates the same dealloc-without-read shape and exhibits
       the same leak.

  R6 — `FutureGroup<T>::join_any()` for `T = String` (and any other
       `T: Copy` with a `Destructible`-bearing retain/release).
       `join_any` used `mem.read` as a non-consuming peek, leaving
       `ResultState.initialized = true`.  In Drift, `T: Copy` does
       NOT imply "no destructor" — `String` is `Copy` with
       retain/release semantics — so the raw `mem.read` moves the
       single ownership stake out of the slot while the slot
       still claims to own it.  Subsequent `join_all` (or the
       futures' destructors) read the same stale bytes, take their
       own stake, and the double-release aborts the process via
       `drift_string_release` underflow.

  R7 — `FutureGroup<T>::join_any()` reads uninitialised result
       storage for a future that was cancelled before its callback
       started.  `vt_is_completed` returns 1 once the executor's
       worker observes `cancelled && !started` and drops the cb —
       but the result slot was never written.  The peek
       (`*mem.ptr_at_ref`) reads garbage bytes; for `T = String`
       this is observable to memcheck as "Conditional jump or move
       depends on uninitialised value(s)" inside
       `drift_string_release` on the returned `Ok(garbage)`.

  R8 — `FutureGroup<T>::join_any()` hangs when a future has
       `submit_error != 0`.  Submission failures leave the future
       with `handle == 0`; `thread.vt_is_completed(0)` returns 0
       forever; the polling loop sees `pending = true` (no
       `joined` skip), parks 1 ms, repeats indefinitely.  No
       valgrind shape — pure liveness defect.

All eight regressions failed on the pre-fix tree and pass post-fix
at 0.33.1.  R1-R7 ran under valgrind memcheck (memory-safety
defects: UAF / double-free / leak / uninit-read / double-release).
R8 is the outlier — a pure liveness defect, verified
uninstrumented against a tight subprocess timeout (`vt_is_completed`
spins forever on `handle == 0`, so there is no memcheck shape to
catch; the test fails when the polling loop fails to terminate).
The fix is option (d) from plan §5.3 — Drift-side
`Arc<Mutex<ResultState<T>>>` shared between VT handle and cb thunk,
with `ResultState<T>::destroy` as the single deallocation point —
plus targeted `FutureGroup.join_any` hardening (R6-R8).  ABI
unchanged at 14.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout, valgrind_cmd


def _asan_active() -> bool:
	return os.environ.get("DRIFT_ASAN") in ("1", "true", "True")


ROOT = Path(__file__).resolve().parents[3]


# ─── R2: submit-error drop double-free ──────────────────────────────

_SOURCE_R2 = """\
module main;
import std.concurrent as conc;
import std.core as core;
import std.console as console;
import lang.thread as thread;

pub fn main() nothrow -> Int {
\t/* Force every exec_submit to fail with code 1.  This drives
\t * spawn() down the submit-error branch
\t * (stdlib/std/concurrent/concurrent.drift:1136-1144): the buffer
\t * is dealloc'd, h is dropped, and the returned VT carries
\t * submit_error = 1 with handle = 0. */
\tthread.exec_submit_test_override(1);
\tconsole.eprintln("repro:override-set");

\tval cb: core.Callback0<String> = core.callback0(| | => {
\t\treturn "should-not-run".clone();
\t});
\t{
\t\tvar vt = conc.spawn(move cb);
\t\tconsole.eprintln("repro:vt-with-submit-error-created");
\t\t/* vt drops here.  Destructible::destroy sees
\t\t * handle == 0 (skips vt_drop) and unconditionally calls
\t\t * mem.dealloc<String>(buf) — but spawn ALREADY freed buf
\t\t * on the submit-error path.  → Invalid free. */
\t}
\tconsole.eprintln("repro:vt-dropped");
\treturn 0;
}
"""


# ─── R3: completed-unjoined drop leaks unobserved owned result ──────

_SOURCE_R3 = """\
module main;
import std.concurrent as conc;
import std.core as core;
import std.console as console;

pub fn main() nothrow -> Int {
\t/* The cb returns a heap-allocated String (produced via concat —
\t * literal `.clone()` would be a static refcount no-op and leak
\t * nothing).  After spawn, we wait long enough for the cb to
\t * definitely have published its result; then we drop the
\t * VirtualThread<String> WITHOUT calling .join(). */
\tval cb: core.Callback0<String> = core.callback0(| | => {
\t\tval a: String = "completed-unjoined-leak-";
\t\tval b: String = "canary-heap-zzz-ABCDEFGHIJ";
\t\treturn a + b;
\t});
\tvar vt = conc.spawn(move cb);
\tconsole.eprintln("repro:spawned");

\tval _ = conc.sleep(conc.Duration(millis = 200));
\tconsole.eprintln("repro:cb-should-be-done");

\t{
\t\tvar local_vt = move vt;
\t\t/* local_vt drops here.  Destructible::destroy calls
\t\t * vt_drop (which destroys h since completed=1) then
\t\t * mem.dealloc<String>(buf) — layout-only free, no
\t\t * String::destroy on the buf contents.  → 67 bytes of
\t\t * concat-allocated String body leak. */
\t}
\tconsole.eprintln("repro:dropped");
\treturn 0;
}
"""


# ─── R4 / R5: cancel-then-publish leaks discarded result ────────────
#
# R4 and R5 share the cb shape; the only difference is whether the
# supervisor calls `join()` or `join_timeout(d)` to surface the
# cancellation.  Both branches in `concurrent.drift` have the same
# dealloc-without-read pattern.  Templated into _SOURCE_R4 (uses
# `join()`) and _SOURCE_R5 (uses `join_timeout(100ms)`).

_SOURCE_R4 = """\
module main;
import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.console as console;
import lang.atomic as atomic;

pub struct Signal {
\tpub started: conc.AtomicBool
}

pub fn main() nothrow -> Int {
\tval signal: arc.Arc<Signal> = arc.arc(Signal(
\t\tstarted = conc.atomic_bool(false)
\t));
\tval signal_for_cb = signal.clone();

\t/* The cb signals 'started', parks in conc.sleep, then unconditionally
\t * constructs and returns a heap-allocated String.  No cooperative
\t * cancellation check after the sleep — the cancel-broadcast wakes
\t * the park but the cb still publishes. */
\tval cb: core.Callback0<String> = core.callback0(
\t\t| | captures(move signal_for_cb) => {
\t\t\tatomic.atomic_store_bool(
\t\t\t\tsignal_for_cb.get().started, true, 2
\t\t\t);
\t\t\tval _ = conc.sleep(conc.Duration(millis = 50));
\t\t\tval a: String = "cancel-discarded-result-";
\t\t\tval b: String = "canary-heap-yyy-ABCDEFGHIJ";
\t\t\treturn a + b;
\t\t}
\t);

\tvar vt = conc.spawn(move cb);
\tconsole.eprintln("repro:spawned");

\t/* Wait for cb to mark 'started' so vt_cancel succeeds
\t * (cancelled transitions 0 → 1; self.cancelled = true). */
\tvar spins: Int = 0;
\twhile !atomic.atomic_load_bool(signal.get().started, 1) {
\t\tval _ = conc.sleep(conc.Duration(millis = 1));
\t\tspins = spins + 1;
\t\tif spins > 5000 {
\t\t\tconsole.eprintln("repro:handshake-timeout");
\t\t\treturn 2;
\t\t}
\t}
\tconsole.eprintln("repro:observed-started");

\tvt.cancel();
\tconsole.eprintln("repro:cancelled");

\t/* Let the cb wake from sleep, build the String, and publish to
\t * the result buffer. */
\tval _ = conc.sleep(conc.Duration(millis = 150));
\tconsole.eprintln("repro:cb-should-be-done");

\t/* join() sees self.cancelled = true → cancellation branch at
\t * stdlib/std/concurrent/concurrent.drift:1314-1325, which calls
\t * vt_join + mem.dealloc(buf) WITHOUT mem.read(v).  The published
\t * String body is leaked.  Returns Err(CANCELLED). */
\tmatch vt.join() {
\t\tOk(_) => {
\t\t\tconsole.eprintln("repro:unexpected-ok");
\t\t\treturn 3;
\t\t},
\t\tErr(_) => {
\t\t\tconsole.eprintln("repro:got-err-cancelled");
\t\t},
\t\tdefault => { return 4; }
\t}
\treturn 0;
}
"""


# Same shape as R4 but uses `join_timeout(100ms)` to take the
# cancellation branch in `join_timeout` rather than the one in `join`.
_SOURCE_R5 = """\
module main;
import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.console as console;
import lang.atomic as atomic;

pub struct Signal {
\tpub started: conc.AtomicBool
}

pub fn main() nothrow -> Int {
\tval signal: arc.Arc<Signal> = arc.arc(Signal(
\t\tstarted = conc.atomic_bool(false)
\t));
\tval signal_for_cb = signal.clone();

\tval cb: core.Callback0<String> = core.callback0(
\t\t| | captures(move signal_for_cb) => {
\t\t\tatomic.atomic_store_bool(
\t\t\t\tsignal_for_cb.get().started, true, 2
\t\t\t);
\t\t\tval _ = conc.sleep(conc.Duration(millis = 50));
\t\t\tval a: String = "cancel-discarded-jt-";
\t\t\tval b: String = "canary-heap-r5-ABCDEFGHIJ";
\t\t\treturn a + b;
\t\t}
\t);

\tvar vt = conc.spawn(move cb);
\tconsole.eprintln("repro:spawned");

\tvar spins: Int = 0;
\twhile !atomic.atomic_load_bool(signal.get().started, 1) {
\t\tval _ = conc.sleep(conc.Duration(millis = 1));
\t\tspins = spins + 1;
\t\tif spins > 5000 {
\t\t\tconsole.eprintln("repro:handshake-timeout");
\t\t\treturn 2;
\t\t}
\t}
\tconsole.eprintln("repro:observed-started");

\tvt.cancel();
\tconsole.eprintln("repro:cancelled");

\tval _ = conc.sleep(conc.Duration(millis = 150));
\tconsole.eprintln("repro:cb-should-be-done");

\t/* join_timeout sees self.cancelled = true → takes the cancellation
\t * branch at concurrent.drift:1374-1385, which also deallocates the
\t * buffer WITHOUT mem.read(v).  Same leak shape as R4. */
\tmatch vt.join_timeout(conc.Duration(millis = 100)) {
\t\tOk(_) => {
\t\t\tconsole.eprintln("repro:unexpected-ok");
\t\t\treturn 3;
\t\t},
\t\tErr(_) => {
\t\t\tconsole.eprintln("repro:got-err-cancelled-via-jt");
\t\t},
\t\tdefault => { return 4; }
\t}
\treturn 0;
}
"""


# ─── R6: FutureGroup<String>::join_any double-release ───────────────
#
# `FutureGroup<T>` requires `T: core.Copy`.  In Drift, Copy does NOT
# imply "no destructor": `String` is Copy with retain/release
# refcount semantics.  A raw `mem.read` peek moves the slot's
# ownership stake without retaining; subsequent reads (or destructor
# runs) then double-release the same DriftString backing storage,
# which `drift_string_release` detects as a refcount underflow and
# aborts via SIGABRT.

_SOURCE_R6 = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.console as console;
import lang.thread as thread;

pub fn main() nothrow -> Int {
\tvar g = conc.future_group<type String>();

\t/* f1 returns first (short park, heap String via concat). */
\tval cb1: core.Callback0<String> = core.callback0(| | => {
\t\tthread.vt_park_until(thread.now_ms() + 5);
\t\tval a: String = "futgroup-canary-";
\t\tval b: String = "first-heap-AAAAAA";
\t\treturn a + b;
\t});
\t/* f2 returns later (long park; ensures f1 is the join_any winner). */
\tval cb2: core.Callback0<String> = core.callback0(| | => {
\t\tthread.vt_park_until(thread.now_ms() + 100);
\t\tval a: String = "futgroup-canary-";
\t\tval b: String = "second-heap-BBBBBB";
\t\treturn a + b;
\t});

\tvar f1 = conc.spawn_future<type String>(move cb1);
\tvar f2 = conc.spawn_future<type String>(move cb2);
\tg.add(move f1);
\tg.add(move f2);

\t/* join_any returns the first ready (f1).  Under the legacy
\t * peek-without-consume contract the future stays in the group;
\t * subsequent `join_all` then re-observes both. */
\tvar any_result = g.join_any();
\tmatch any_result {
\t\tOk(_v) => { console.eprintln("repro:join_any-ok"); },
\t\tErr(_) => { return 1; },
\t\tdefault => { return 2; }
\t}

\tvar all_result = g.join_all();
\tmatch all_result {
\t\tOk(_vs) => { console.eprintln("repro:join_all-ok"); },
\t\tErr(_) => { console.eprintln("repro:join_all-err"); },
\t\tdefault => { return 3; }
\t}

\treturn 0;
}
"""


# ─── R7: FutureGroup<String>::join_any uninit-read for cancel-before-start ──
#
# Cancelled-before-start futures complete (worker drops the cb in
# the `cancelled && !started` pickup branch and sets
# `vt->completed = 1`) but never publish a result.  The legacy
# `join_any` treats `vt_is_completed != 0` as proof of publication
# and `*ref`s uninitialised slot bytes.

_SOURCE_R7 = """\
module main;
import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.console as console;
import lang.atomic as atomic;
import lang.thread as thread;

pub struct Sigs {
\tpub blocker_started: conc.AtomicBool,
\tpub blocker_stop: conc.AtomicBool,
\t/* Set by the blocker iff its carrier-holding busy-spin hit the
\t * deadline instead of observing `blocker_stop`.  A non-cooperative
\t * spin is the only way to keep the single carrier occupied (parking
\t * would hand it to the queued target and start it), but an UNBOUNDED
\t * spin masks a logic regression as a teardown hang / runner timeout.
\t * Bounded + checked → a deadline expiry is a distinct visible failure
\t * (exit 5), never a silent false pass. */
\tpub blocker_timed_out: conc.AtomicBool
}

pub fn main() nothrow -> Int {
\t/* One-carrier executor with room to queue more than one task. */
\tvar policy_b = conc.executor_policy_builder();
\tpolicy_b.min_threads(1);
\tpolicy_b.max_threads(1);
\tpolicy_b.queue_limit(10);
\tval exec = policy_b.build_executor();
\tconc.set_default_executor(exec);

\tval sigs: arc.Arc<Sigs> = arc.arc(Sigs(
\t\tblocker_started = conc.atomic_bool(false),
\t\tblocker_stop = conc.atomic_bool(false),
\t\tblocker_timed_out = conc.atomic_bool(false)
\t));
\tval sigs_for_blocker = sigs.clone();

\t/* CPU-bound blocker holds the single carrier on its fiber so the
\t * subsequently-spawned target stays queued (un-started) until we
\t * release it.  Non-cooperative busy-spin (no park/yield) so the
\t * carrier is never handed back to run the queued target — hard-capped
\t * by a monotonic deadline so a regression that never releases it
\t * surfaces as a distinct `blocker_timed_out` failure (exit 5) rather
\t * than a masked teardown hang. */
\tval blocker_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move sigs_for_blocker) => {
\t\t\tatomic.atomic_store_bool(
\t\t\t\tsigs_for_blocker.get().blocker_started, true, 2
\t\t\t);
\t\t\tval deadline = thread.now_ms() + 20000;
\t\t\twhile !atomic.atomic_load_bool(
\t\t\t\tsigs_for_blocker.get().blocker_stop, 1
\t\t\t) {
\t\t\t\tif thread.now_ms() > deadline {
\t\t\t\t\tatomic.atomic_store_bool(
\t\t\t\t\t\tsigs_for_blocker.get().blocker_timed_out, true, 2
\t\t\t\t\t);
\t\t\t\t\tbreak;
\t\t\t\t}
\t\t\t}
\t\t\treturn 0;
\t\t}
\t);
\tvar blocker = conc.spawn(move blocker_cb);
\tconsole.eprintln("repro:blocker-spawned");

\t/* Wait until the blocker's cb is on the carrier (it sets
\t * `blocker_started` as its first act).  Poll with conc.sleep — a
\t * real yield that relinquishes the OS thread — never a tight VT spin:
\t * under Valgrind's serialized scheduler a tight spin here would
\t * starve the very worker we are waiting on.  This is a condition
\t * wait, not a fixed delay; the bounded `spins` guard only converts a
\t * never-starts regression into a distinct exit code (10), and always
\t * releases the blocker first so its spin cannot outlive main. */
\tvar spins: Int = 0;
\twhile !atomic.atomic_load_bool(sigs.get().blocker_started, 1) {
\t\tval _ = conc.sleep(conc.Duration(millis = 5));
\t\tspins = spins + 1;
\t\tif spins > 5000 {
\t\t\tatomic.atomic_store_bool(sigs.get().blocker_stop, true, 2);
\t\t\tval _j = blocker.join();
\t\t\treturn 10;
\t\t}
\t}
\tconsole.eprintln("repro:blocker-running");

\t/* Target stays queued; carrier is busy spinning on blocker. */
\tval target_cb: core.Callback0<String> = core.callback0(| | => {
\t\tval a: String = "should-not-";
\t\tval b: String = "run-result-XXXXXX";
\t\treturn a + b;
\t});
\tvar target_future = conc.spawn_future(move target_cb);
\tconsole.eprintln("repro:target-spawned");

\t/* Cancel BEFORE the worker can start the target.
\t * The worker will later pick up the queued, cancelled,
\t * !started task, drop the cb without running it, and mark
\t * completed = 1. */
\ttarget_future.cancel();
\tconsole.eprintln("repro:target-cancelled");

\t/* Release the blocker so the carrier is freed to process the
\t * cancelled target's cancel-drop branch. */
\tatomic.atomic_store_bool(sigs.get().blocker_stop, true, 2);
\tval _ = blocker.join();
\tconsole.eprintln("repro:blocker-joined");

\tif atomic.atomic_load_bool(sigs.get().blocker_timed_out, 1) {
\t\tconsole.eprintln("repro:blocker-timed-out");
\t\treturn 5;
\t}

\t/* Establish the R7 precondition by STATE, not a fixed sleep: wait
\t * until the runtime has observed the cancelled queued target as
\t * terminal (completed == 1).  ResultState.initialized stays false
\t * because the callback never ran — exactly the case join_any must
\t * not mistake for a published value.  Same condition-wait shape and
\t * bounded guard (exit 11) as above. */
\tvar spins2: Int = 0;
\twhile !target_future.is_complete() {
\t\tval _ = conc.sleep(conc.Duration(millis = 5));
\t\tspins2 = spins2 + 1;
\t\tif spins2 > 5000 { return 11; }
\t}
\tconsole.eprintln("repro:target-complete");

\tvar g = conc.future_group<type String>();
\tg.add(move target_future);

\tvar res = g.join_any();
\tmatch res {
\t\tErr(e) => {
\t\t\tif e.kind == conc.CONCURRENCY_KIND_CANCELLED {
\t\t\t\tconsole.eprintln("repro:got-cancelled");
\t\t\t\treturn 0;
\t\t\t}
\t\t\tconsole.eprintln("repro:got-other-err");
\t\t\treturn 1;
\t\t},
\t\tOk(_) => {
\t\t\tconsole.eprintln("repro:unexpected-ok");
\t\t\treturn 2;
\t\t},
\t\tdefault => { return 3; }
\t}
}
"""


# ─── R8: FutureGroup<T>::join_any hangs on submit-error future ───────
#
# A future whose submission failed has `handle == 0`; the runtime's
# `vt_is_completed(0)` returns 0 forever, and `join_any`'s polling
# loop has no terminal check for this case → infinite spin.

_SOURCE_R8 = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.console as console;
import lang.thread as thread;

pub fn main() nothrow -> Int {
\t/* Force every exec_submit to fail so the spawned future
\t * carries submit_error != 0 with handle == 0. */
\tthread.exec_submit_test_override(1);
\tconsole.eprintln("repro:override-set");

\tval cb: core.Callback0<String> = core.callback0(| | => {
\t\tval a: String = "should-not-";
\t\tval b: String = "run-XXXXXX";
\t\treturn a + b;
\t});
\tvar f = conc.spawn_future(move cb);
\tconsole.eprintln("repro:future-with-submit-error");

\t/* Clear override so any internal stdlib spawns that might run
\t * during the rest of main don't also fail unexpectedly. */
\tthread.exec_submit_test_override(0);

\tvar g = conc.future_group<type String>();
\tg.add(move f);
\tconsole.eprintln("repro:about-to-join-any");

\t/* On HEAD: join_any sees handle==0, vt_is_completed returns 0
\t * forever, pending stays true on every sweep — hang. */
\tvar res = g.join_any();
\tmatch res {
\t\tErr(e) => {
\t\t\tif e.kind == conc.CONCURRENCY_KIND_FAILED {
\t\t\t\tconsole.eprintln("repro:got-failed");
\t\t\t\treturn 0;
\t\t\t}
\t\t\tconsole.eprintln("repro:got-other-err");
\t\t\treturn 1;
\t\t},
\t\tOk(_) => {
\t\t\tconsole.eprintln("repro:unexpected-ok");
\t\t\treturn 2;
\t\t},
\t\tdefault => { return 3; }
\t}
}
"""


def _compile(tmp_path: Path, source: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "repro"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		f"compile failed: rc={res.returncode}\n"
		f"stderr: {res.stderr[:800]}"
	)
	assert out.exists()
	return out


# Markers that indicate the LANGUAGE_BUG fired.  Any of these in stderr
# means the regression reproduced (and the test must FAIL).  Once the
# fix lands, none of these should appear and the tests will PASS.
_DOUBLE_FREE_MARKERS = (
	"Invalid free",
	"Invalid free() / delete",
)

_INVALID_RW_MARKERS = (
	"Invalid write",
	"Invalid read",
)


def _definite_leak_bytes(stderr: str) -> int:
	"""Extract `definitely lost: N bytes in M blocks` from a valgrind
	leak-check report.  Returns N (0 if absent or zero)."""
	for line in stderr.splitlines():
		s = line.strip()
		# Valgrind line format: "==pid== definitely lost: N bytes in M blocks"
		if "definitely lost:" in s:
			parts = s.split("definitely lost:")[-1].strip()
			n = parts.split("bytes")[0].strip().replace(",", "")
			try:
				return int(n)
			except ValueError:
				return 0
	return 0


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — Invalid-free / leak detection "
	"requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind "
	"(shadow-memory ranges interleave; ASan aborts before exit).  "
	"This regression's natural lane is Valgrind; ASan would catch the "
	"double-free identically through its own allocator hooks.")
def test_r2_submit_error_drop_double_free(tmp_path: Path) -> None:
	"""R2: VT with submit_error != 0 must not double-free its result
	buffer when dropped without join()."""
	binary = _compile(tmp_path, _SOURCE_R2)
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=77", "--track-origins=yes",
			str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	found = [m for m in _DOUBLE_FREE_MARKERS if m in combined]
	assert not found, (
		f"valgrind reported double-free markers {found}; this is R2\n"
		f"of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert "repro:vt-with-submit-error-created" in combined, (
		"spawn did not return; submit-error VT was never constructed."
	)
	assert "repro:vt-dropped" in combined, (
		"VT drop point not reached."
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = error detected)"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — leak detection requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind. "
	"This regression's natural lane is Valgrind's leak-check; "
	"LeakSanitizer would catch the same leak through ASan's "
	"end-of-run scan.")
def test_r3_completed_unjoined_drop_no_leak(tmp_path: Path) -> None:
	"""R3: dropping an already-completed unjoined VT<String> must
	destroy the unobserved owned result rather than leak it."""
	binary = _compile(tmp_path, _SOURCE_R3)
	res = subprocess.run(
		valgrind_cmd("--leak-check=full", "--show-leak-kinds=definite",
			"--error-exitcode=77", str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	leaked = _definite_leak_bytes(res.stderr)
	assert leaked == 0, (
		f"valgrind reported {leaked} bytes definitely lost; this is\n"
		f"R3 of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  The unobserved\n"
		f"String result was never destroyed before mem.dealloc.\n"
		f"Excerpt:\n{combined[-1500:]}"
	)
	assert "repro:dropped" in combined, (
		"VT drop point not reached."
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = leak detected)"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — leak detection requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind. "
	"This regression's natural lane is Valgrind's leak-check; "
	"LeakSanitizer would catch the same leak.")
def test_r4_cancel_publish_join_cancelled_no_leak(tmp_path: Path) -> None:
	"""R4: cancelling a started task that subsequently publishes a
	`T`, then joining with Err(CANCELLED), must destroy the discarded
	`T` rather than leak it."""
	binary = _compile(tmp_path, _SOURCE_R4)
	res = subprocess.run(
		valgrind_cmd("--leak-check=full", "--show-leak-kinds=definite",
			"--error-exitcode=77", str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	leaked = _definite_leak_bytes(res.stderr)
	assert leaked == 0, (
		f"valgrind reported {leaked} bytes definitely lost; this is\n"
		f"R4 of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  The cb published\n"
		f"a result before join took the cancellation branch, and that\n"
		f"discarded T was never destroyed.  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert "repro:observed-started" in combined, (
		"cb-started handshake never fired."
	)
	assert "repro:got-err-cancelled" in combined, (
		"join did not take the cancellation branch — test premise broken."
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = leak detected)"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — leak detection requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind. "
	"This regression's natural lane is Valgrind's leak-check; "
	"LeakSanitizer would catch the same leak.")
def test_r5_cancel_publish_join_timeout_cancelled_no_leak(tmp_path: Path) -> None:
	"""R5: same as R4 but via `join_timeout()`.  Cancellation branch
	in `join_timeout` must destroy the discarded published `T`
	rather than leak it."""
	binary = _compile(tmp_path, _SOURCE_R5)
	res = subprocess.run(
		valgrind_cmd("--leak-check=full", "--show-leak-kinds=definite",
			"--error-exitcode=77", str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	leaked = _definite_leak_bytes(res.stderr)
	assert leaked == 0, (
		f"valgrind reported {leaked} bytes definitely lost; this is\n"
		f"R5 of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  The cb published\n"
		f"a result before join_timeout took the cancellation branch,\n"
		f"and that discarded T was never destroyed.  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert "repro:observed-started" in combined, (
		"cb-started handshake never fired."
	)
	assert "repro:got-err-cancelled-via-jt" in combined, (
		"join_timeout did not take the cancellation branch — test premise broken."
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = leak detected)"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — invalid-access / double-release "
	"detection requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind. "
	"This regression's natural lane is Valgrind; ASan would catch "
	"the same use-after-free / double-free through its own hooks.")
def test_r6_future_group_join_any_string_double_release(tmp_path: Path) -> None:
	"""R6: `FutureGroup<String>::join_any()` must not double-release
	the future's `String` result.  `mem.read`-as-peek violates Drift's
	Copy=retain/release contract for `String`."""
	binary = _compile(tmp_path, _SOURCE_R6)
	res = subprocess.run(
		valgrind_cmd("--leak-check=full", "--show-leak-kinds=definite",
			"--error-exitcode=77", str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	# On HEAD this aborts via SIGABRT inside drift_string_release;
	# any abort signature, invalid R/W marker, or definite leak is a
	# regression failure.
	abort_markers = [m for m in ("SIGABRT", "default action of signal 6",
		"drift_string_release") if m in combined]
	rw_markers = [m for m in _INVALID_RW_MARKERS if m in combined]
	leaked = _definite_leak_bytes(res.stderr)
	assert not abort_markers, (
		f"valgrind reported abort markers {abort_markers}; this is\n"
		f"R6 of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  The join_any\n"
		f"peek double-released the String result.  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert not rw_markers, (
		f"valgrind reported invalid-access markers {rw_markers} (R6).\n"
		f"Excerpt:\n{combined[-1500:]}"
	)
	assert leaked == 0, (
		f"valgrind reported {leaked} bytes definitely lost (R6).\n"
		f"Excerpt:\n{combined[-1500:]}"
	)
	assert "repro:join_any-ok" in combined, (
		"join_any never returned Ok — test premise broken."
	)
	assert ("repro:join_all-ok" in combined
			or "repro:join_all-err" in combined), (
		"join_all never returned a Result — process likely aborted "
		"before reaching it (R6 firing).\n"
		f"stderr: {res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = error detected; "
		f"non-zero process abort otherwise)"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — uninit-read detection requires "
	"memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind. "
	"This regression's natural lane is Valgrind's uninit-value check; "
	"the equivalent under ASan is the use-of-uninitialised-memory "
	"detector — both catch the same root cause.")
def test_r7_future_group_join_any_cancelled_before_start_no_uninit_read(tmp_path: Path) -> None:
	"""R7: `FutureGroup<T>::join_any()` must not read uninitialised
	result storage for a future cancelled before its callback
	started.  The runtime marks such a VT completed=1 (worker
	pickup branch drops the cb without running it), but the result
	slot was never written.  `join_any` must detect this case via
	`ResultState.initialized = false` and route through
	`.join()`'s cancellation cleanup.

	Scheduling note: the fixture holds the single executor carrier
	with a non-cooperative busy-spin (the only way to keep the queued
	target un-started — parking would hand the carrier over and start
	it).  Under Valgrind's DEFAULT scheduler that spin can monopolise
	the one serialized execution slot and starve `main`, so `main`
	intermittently failed to drive the scenario within the subprocess
	budget.  `--fair-sched=yes` forces fair round-robin scheduling so
	`main` gets regular slices and the scenario completes in well under
	a second.  This is a determinism fix, not a budget bump; the
	fixture itself establishes every step by STATE (blocker-on-carrier
	handshake, then `is_complete()` for the cancelled target) rather
	than by fixed sleeps."""
	binary = _compile(tmp_path, _SOURCE_R7)
	# valgrind_cmd() supplies --fair-sched=yes by default: the blocker's
	# non-cooperative carrier-holding spin would otherwise monopolize
	# Valgrind's serialized scheduler and starve main (the original flake).
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=77", "--track-origins=yes",
			str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(30),
	)
	combined = res.stderr + "\n" + res.stdout
	uninit_markers = [m for m in (
		"Conditional jump or move depends on uninitialised",
		"Use of uninitialised value",
		"uninitialised value(s)",
	) if m in combined]
	assert not uninit_markers, (
		f"valgrind reported uninit markers {uninit_markers}; this is\n"
		f"R7 of the result-ownership LANGUAGE_BUG (closed at 0.33.1; plan §5).  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert "repro:blocker-running" in combined, (
		"blocker handshake never fired — test premise broken."
	)
	assert "repro:blocker-timed-out" not in combined, (
		"blocker busy-spin hit its 20s deadline before main released it "
		"(exit 5): main was starved while holding the carrier.  Expected "
		"--fair-sched=yes to keep main scheduled — a regression here means "
		"the scheduling guarantee broke, not the join_any fix.\n"
		f"{combined[-1500:]}"
	)
	assert "repro:target-cancelled" in combined, (
		"target was not cancelled — test premise broken."
	)
	assert "repro:target-complete" in combined, (
		"cancelled target never reached the terminal (completed) state — "
		"the R7 precondition was not established.\n"
		f"{combined[-1500:]}"
	)
	assert "repro:got-cancelled" in combined, (
		"join_any did not return CANCELLED for the cancel-before-start "
		"future.  Excerpt:\n"
		f"{combined[-1500:]}"
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = error detected; "
		f"non-zero process abort otherwise)"
	)


def test_r8_future_group_join_any_submit_error_no_hang(tmp_path: Path) -> None:
	"""R8: `FutureGroup<T>::join_any()` must not hang on a future
	whose submission failed (`handle == 0`).  `vt_is_completed(0)`
	returns 0 forever, so the polling loop spun indefinitely on
	HEAD.  Fix detects `submit_error != 0` and routes through
	`.join()`'s submit-error path which returns `Err(FAILED)`."""
	binary = _compile(tmp_path, _SOURCE_R8)
	# Tight timeout — if the loop spins, this trips well within
	# 10s even on slow CI.  No need for valgrind here; the defect
	# is a pure liveness bug, observable as a hang in the
	# uninstrumented binary.
	try:
		res = subprocess.run(
			[str(binary)],
			capture_output=True, text=True,
			timeout=10,
		)
	except subprocess.TimeoutExpired as ex:
		stderr_seen = (ex.stderr or b"").decode("utf-8", errors="replace") if isinstance(ex.stderr, bytes) else (ex.stderr or "")
		stdout_seen = (ex.stdout or b"").decode("utf-8", errors="replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
		raise AssertionError(
			"join_any hung (10s timeout) — R8 of the result-ownership "
			"LANGUAGE_BUG (closed at 0.33.1; plan §5).\n"
			f"stderr (partial): {stderr_seen[:800]}\n"
			f"stdout (partial): {stdout_seen[:800]}"
		)
	combined = res.stderr + "\n" + res.stdout
	assert "repro:about-to-join-any" in combined, (
		"join_any was never reached — test premise broken."
	)
	assert "repro:got-failed" in combined, (
		"join_any did not return FAILED for the submit-error future.\n"
		f"Excerpt:\n{combined[-1500:]}"
	)
	assert res.returncode == 0, (
		f"process exited {res.returncode}; expected 0 (matched).\n"
		f"stderr: {res.stderr[-800:]}"
	)
