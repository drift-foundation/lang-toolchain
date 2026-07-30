# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: registered Arc<T: Destructible> + spawned VT parked in
conc.sleep must not leak captured Arc clones at process exit.

Filed 2026-05-22 from bookkeeper-shutdown-hang follow-up.  The original
SIGTERM hang (0.32.6) was fixed in 0.32.7 by draining the runtime
registry from `drift_run_main_on_vt` BEFORE reactor/exec shutdown.
That fix exposed a second bug: the executor worker fires
`drift_drop_callback(&vt->cb)` whenever it picks up a VT whose
`cancelled` flag is set, regardless of whether the fiber had previously
been started.  For a parked fiber (e.g. inside `conc.sleep`), this
forcibly frees the closure env while the fiber's stack still holds
owning captures the lambda body moved out of the env (typically
`Arc<U>` clones passed as function parameters).  When the fiber stack
is torn down by `drift_worker_vt_finish` no destructors run, so those
captures leak.

Fix: in `lang/language_runtime/posix/thread_runtime.c::drift_exec_worker`,
change the `started` mark to `atomic_exchange(...) -> was_started` and
gate the cancel-path drop on `!was_started`.  When `was_started=1` the
worker falls through to fiber resume; `conc.sleep` observes cancel,
returns Err, `_keepalive`-style loops exit normally, and the fiber's
function-exit cleanup decrements the captured Arc.

This test pins the shape that triggered the leak in mariadb-rpc's
`pool.ConnectionPool` keepalive thread: a registered `Arc<Pooled>`
whose Destructible::destroy cancels + joins a VT that captured a clone
of an inner `Arc<U>`.  Asserts the binary exits cleanly AND that
valgrind reports zero definitely-lost blocks under `--leak-check=full`.
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

_SOURCE = """\
module main;
import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.console as console;
import lang.atomic as atomic;
import std.mem as mem;
import std.runtime as rt;

pub struct PoolInner {
\tpub stop: conc.AtomicBool,
\tpub payload: String
}

fn _keepalive(inner: arc.Arc<PoolInner>) nothrow -> Int {
\twhile !atomic.atomic_load_bool(inner.get().stop, 1) {
\t\tmatch conc.sleep(conc.Duration(millis = 50)) {
\t\t\tcore.Result::Err(_) => { return 0; },
\t\t\tcore.Result::Ok(_) => {}
\t\t}
\t}
\treturn 0;
}

pub struct ConnectionPool {
\tpub inner: arc.Arc<PoolInner>,
\tpub vt: Optional<conc.VirtualThread<Int> >
}

implement ConnectionPool {
\tpub fn close(self: &mut ConnectionPool) nothrow -> Void {
\t\tatomic.atomic_store_bool(self.inner.get().stop, true, 2);
\t\tval taken = mem.replace(
\t\t\tself.vt,
\t\t\tOptional<type conc.VirtualThread<Int> >::None()
\t\t);
\t\tmatch taken {
\t\t\tOptional::None => {},
\t\t\tOptional::Some(v) => {
\t\t\t\tvar vt = move v;
\t\t\t\tvt.cancel();
\t\t\t\tval _ = vt.join();
\t\t\t}
\t\t}
\t}
}

pub fn open_pool() nothrow -> ConnectionPool {
\tval inner: arc.Arc<PoolInner> = arc.arc(PoolInner(
\t\tstop = conc.atomic_bool(false),
\t\tpayload = "registry-arc-parked-vt-canary".clone()
\t));
\tval for_vt = inner.clone();
\tval cb: core.Callback0<Int> = core.callback0(| | captures(move for_vt) => { return _keepalive(move for_vt); });
\tvar vt = conc.spawn(move cb);
\treturn ConnectionPool(
\t\tinner = move inner,
\t\tvt = Optional<type conc.VirtualThread<Int> >::Some(move vt)
\t);
}

pub struct Pooled
\trequire Self is core.Destructible
{
\tmu: conc.Mutex<ConnectionPool>
}

implement core.Destructible for Pooled {
\tpub fn destroy(var self: Pooled) nothrow -> Void {
\t\tvar guard = self.mu.lock();
\t\tguard.get_mut().close();
\t}
}

pub fn main() nothrow -> Int {
\tconsole.eprintln("repro:start");
\tval p = Pooled(mu = conc.mutex(open_pool()));
\tval a: arc.Arc<Pooled> = arc.arc(move p);
\tval reg = rt.global_registry();
\tval _ = reg.set<type arc.Arc<Pooled>>(move a);
\tconsole.eprintln("repro:registered");
\t/* Give the keepalive VT time to enter conc.sleep so the
\t * cancel+join path hits the parked-fiber code path. */
\tval _ = conc.sleep(conc.Duration(millis = 200));
\tconsole.eprintln("repro:slept");
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
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:400]}"
	assert out.exists()
	return out


def test_registered_arc_with_parked_vt_exits_clean(tmp_path: Path) -> None:
	"""Process exits 0 within ~1s — the original bookkeeper SIGTERM
	hang fix must hold."""
	binary = _compile(tmp_path)
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(5))
	assert res.returncode == 0, (
		f"expected clean exit, got {res.returncode}\n"
		f"stderr: {res.stderr[:400]}"
	)
	# stderr markers prove main reached return 0 (not e.g. an uncaught throw)
	assert "repro:start" in res.stderr
	assert "repro:registered" in res.stderr
	assert "repro:slept" in res.stderr


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — leak check requires memcheck")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind: "
	"shadow-memory ranges interleave and ASan aborts before exit. "
	"This leak check belongs to the non-ASan lane; the ASan lane gets "
	"its own coverage via test_registered_arc_with_parked_vt_exits_clean.")
def test_registered_arc_with_parked_vt_no_leak(tmp_path: Path) -> None:
	"""Under valgrind --leak-check=full, all heap blocks must be freed.

	Pinned bug: the captured `Arc<PoolInner>` clone in the keepalive VT's
	closure was leaked because the executor worker dropped the cb on a
	cancelled, *previously-started* VT without resuming the fiber to let
	its cleanup decrement the captured Arc.  392-byte direct + 72-byte
	indirect was the bookkeeper signature on 0.32.7; on the stdlib-only
	repro the leak is the 48-byte `ArcBox<PoolInner>` itself."""
	binary = _compile(tmp_path)
	res = subprocess.run(
		valgrind_cmd("--leak-check=full", "--show-leak-kinds=definite",
			"--error-exitcode=99", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(60),
	)
	# Two valgrind shapes count as "no leaks": either it skips LEAK SUMMARY
	# entirely with "All heap blocks were freed" (the clean-exit case), or
	# it emits a LEAK SUMMARY with "definitely lost: 0 bytes in 0 blocks".
	# Any non-zero "definitely lost: N bytes" is a regression.
	clean_no_summary = "All heap blocks were freed -- no leaks are possible" in res.stderr
	summary_zero = "definitely lost: 0 bytes in 0 blocks" in res.stderr
	assert clean_no_summary or summary_zero, (
		f"expected no definite leaks; valgrind exit={res.returncode}\n"
		f"summary: {res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode} (99 = leaks detected)\n"
		f"summary: {res.stderr[-1500:]}"
	)
