# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: dropping an unjoined VirtualThread<T> whose
task has already started running is a heap-use-after-free on the
result buffer.

Classified 2026-05-26.  See
`project_vt_drop_started_running_uaf.md` for diagnosis and
`work/stdlib-concurrency/plan.md` §5 for the fix-shape track.

Mechanism summary:

  * `spawn<T>(cb)` at `stdlib/std/concurrent/concurrent.drift:1120`
    allocates `buf = mem.alloc_uninit<T>(1)` and captures
    `buf_for_cb` into the cb's thunk.  The returned
    `VirtualThread<T>.result` field is a second `RawBuffer<T>` over
    the same ptr/cap.
  * Worker dequeues the task, exchanges `h->started = 1`, resumes the
    fiber.  The thunk shape is: `var v = cb.call(); mem.write(&mut
    buf_for_cb, 0, move v);`
  * The Drift destructor at
    `stdlib/std/concurrent/concurrent.drift:1442` calls
    `thread.vt_drop(handle)` then unconditionally
    `mem.dealloc<T>(buf)`.
  * `drift_thread_drop` at
    `lang/language_runtime/posix/thread_runtime.c:2087` for
    started-and-running only sets `cancelled = 1` and broadcasts cv;
    it does NOT join.  The Drift destructor's subsequent
    `mem.dealloc` runs while the worker's thunk is still going to
    execute `mem.write(&mut buf_for_cb, 0, move v)` — UAF.

Deterministic handshake:

  1. The cb sets a shared `AtomicBool started = true` BEFORE its
     blocker.  This proves the worker has reached the cb body, which
     means `h->started = 1` was set earlier in the worker pickup
     path.
  2. The supervising VT spin-waits until `started == true` BEFORE
     dropping the handle, so vt_drop reliably takes the
     started-running branch (not the pre-start
     `!is_started && exec == NULL` cleanup branch, which is safe and
     does NOT reproduce the bug).
  3. After observing started, the supervisor drops the unjoined VT.
     vt_drop broadcasts cv, waking the cb's parked `conc.sleep`.
  4. The cb resumes, constructs and returns its owned `String`; the
     spawn thunk performs the `mem.write` into the now-freed buffer.

ASAN catches the heap-use-after-free at the mem.write site.
Valgrind catches it as an Invalid write.  Either is sufficient.

This regression failed on the pre-fix tree (UAF reproduced) and
passes post-fix at 0.33.1.  The fix is option (d) from plan §5.3
— Drift-side `Arc<Mutex<ResultState<T>>>` shared between VT handle
and cb thunk; `Destructible::destroy` sets `abandoned = true` under
the state lock so the late-publishing cb thunk drops its `T`
locally instead of writing to the buffer the destructor would
otherwise free.
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


# The canary string is deliberately long and recognizable so the UAF
# write is large enough for ASAN's shadow memory to flag cleanly.  Its
# contents are not asserted against post-mortem; ASAN reports the
# access itself.
_SOURCE = """\
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

\t/* The cb signals 'started' through the shared Arc<Signal>, then
\t * parks in conc.sleep.  When the supervising VT drops the
\t * VirtualThread<String> handle, vt_drop broadcasts cv → the
\t * parked sleep wakes → the cb falls through to its String return
\t * → the spawn thunk writes the String into the buffer that the
\t * VirtualThread destructor already freed. */
\tval cb: core.Callback0<String> = core.callback0(
\t\t| | captures(move signal_for_cb) => {
\t\t\tatomic.atomic_store_bool(
\t\t\t\tsignal_for_cb.get().started, true, 2
\t\t\t);
\t\t\tval _ = conc.sleep(conc.Duration(millis = 500));
\t\t\treturn "uaf-canary-result-string-0123456789abcdef".clone();
\t\t}
\t);

\tvar vt = conc.spawn(move cb);
\tconsole.eprintln("repro:spawned");

\t/* Spin-wait until the cb confirms 'started'.  This guarantees the
\t * worker has set h->started = 1 (which happens in the runtime
\t * worker pickup path BEFORE the fiber resumes into the cb body),
\t * so the upcoming drop takes the started-running branch of
\t * vt_drop, not the pre-start cleanup branch. */
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

\t/* Drop the unjoined VT handle.  Inner scope makes the drop point
\t * explicit; vt's Destructible::destroy runs at the closing brace.
\t * On current HEAD: drift_thread_drop sees started=1, !completed →
\t * sets cancelled + broadcasts cv but returns without joining →
\t * Drift-side destructor proceeds to mem.dealloc the result
\t * buffer.  The cb (still parked in conc.sleep on the worker
\t * fiber) is woken by the broadcast, returns its String, and the
\t * spawn thunk writes into the freed buffer. */
\t{
\t\tvar local_vt = move vt;
\t\t/* local_vt's Destructible::destroy runs at the closing
\t\t * brace — that is the load-bearing call point. */
\t}
\tconsole.eprintln("repro:vt-dropped");

\t/* Give the worker fiber time to wake from the cancel broadcast,
\t * fall through conc.sleep, construct the canary String, and
\t * perform the offending mem.write into the freed buffer. */
\tval _ = conc.sleep(conc.Duration(millis = 600));
\tconsole.eprintln("repro:done");
\treturn 0;
}
"""


def _compile(tmp_path: Path) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
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


_ASAN_UAF_MARKERS = (
	"heap-use-after-free",
	"AddressSanitizer:",
	"ERROR: AddressSanitizer",
	"ERROR: LeakSanitizer",
)


_VALGRIND_UAF_MARKERS = (
	"Invalid write",
	"Invalid read",
	# `Invalid free` would surface if any future fix overcorrected by
	# transferring buffer ownership to the runtime without removing the
	# Drift-side dealloc.  Failing it here pins both directions.
	"Invalid free",
)


@pytest.mark.skipif(not _asan_active(),
	reason="ASan-only repro: heap-use-after-free is observable through "
	"ASan shadow memory.  Set DRIFT_ASAN=1 to opt this lane in; the "
	"non-ASan-instrumented run does not crash because the freed buffer "
	"is typically still mapped and the UAF is silent without "
	"instrumentation.")
def test_started_running_vt_drop_no_uaf_under_asan(tmp_path: Path) -> None:
	"""HEAD must fail this test (UAF reported); a sound fix to
	`drift_thread_drop` / `Destructible::destroy` for VirtualThread<T>
	must make it pass."""
	binary = _compile(tmp_path)
	res = subprocess.run(
		[str(binary)],
		capture_output=True, text=True,
		timeout=sanitizer_timeout(15),
	)
	combined = res.stderr + "\n" + res.stdout
	found = [m for m in _ASAN_UAF_MARKERS if m in combined]
	assert not found, (
		f"ASan reported UAF markers {found}; this is the open\n"
		f"LANGUAGE_BUG documented in\n"
		f"work/stdlib-concurrency/plan.md §5.  Excerpt:\n"
		f"{combined[-2000:]}"
	)
	assert "repro:observed-started" in combined, (
		"handshake never fired — the cb did not signal 'started'.\n"
		f"stderr: {res.stderr[:800]}"
	)
	assert "repro:vt-dropped" in combined, (
		"vt drop point not reached — the test never exercised vt_drop "
		"in the started-running branch.\n"
		f"stderr: {res.stderr[:800]}"
	)
	assert res.returncode == 0, (
		f"expected clean exit; got rc={res.returncode}\n"
		f"stderr: {res.stderr[:800]}"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — Invalid write detection requires "
	"memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind: "
	"shadow-memory ranges interleave and ASan aborts before exit. "
	"This Valgrind lane is exclusive with the ASan lane.")
def test_started_running_vt_drop_no_uaf_under_valgrind(tmp_path: Path) -> None:
	"""HEAD must fail this test (Invalid write reported by memcheck);
	a sound fix must make it pass.  Provides an instrumentation-
	independent second witness alongside the ASan lane."""
	binary = _compile(tmp_path)
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=77", "--track-origins=yes",
			str(binary)),
		capture_output=True, text=True,
		timeout=sanitizer_timeout(60),
	)
	combined = res.stderr + "\n" + res.stdout
	found = [m for m in _VALGRIND_UAF_MARKERS if m in combined]
	assert not found, (
		f"valgrind reported invalid-access markers {found}; this is\n"
		f"the open LANGUAGE_BUG documented in\n"
		f"work/stdlib-concurrency/plan.md §5.  Excerpt:\n"
		f"{combined[-2000:]}"
	)
	assert "repro:observed-started" in combined, (
		"handshake never fired — the cb did not signal 'started'.\n"
		f"stderr: {res.stderr[:1500]}"
	)
	assert "repro:vt-dropped" in combined, (
		"vt drop point not reached — the test never exercised vt_drop "
		"in the started-running branch.\n"
		f"stderr: {res.stderr[:1500]}"
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (77 = invalid access "
		f"detected)\nsummary: {res.stderr[-1500:]}"
	)
