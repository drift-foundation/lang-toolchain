# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Reference + regression: the reload-coordinator protocol (doc/design/reload-substrate.md).

Composes the slice's two new pieces with existing primitives:
  SIGUSR1 -> [signal VT] await_signal()=User1 -> channel send
          -> [worker VT] recv() -> read_dir(config_dir) -> stage -> verify
          -> Arc<Mutex<State>> swap via mem.replace (old dropped outside the lock)

The test sends a real SIGUSR1, then asserts the worker rescanned the config
directory and atomically published the new entry count.
"""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]


def _read_line(stream, timeout_s: float) -> str:
	"""Read one newline-terminated line from `stream` within `timeout_s`.

	stderr is unbuffered in the runtime, so each worker ack arrives as soon as it
	is written.  Using select (not a fixed sleep) makes the reload ordering an
	observed fact, not a timing assumption — and a stuck worker fails the test
	with a clear timeout instead of hanging."""
	deadline = time.monotonic() + timeout_s
	buf = b""
	fd = stream.fileno()
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise AssertionError(f"timed out after {timeout_s}s waiting for a line; partial={buf!r}")
		r, _, _ = select.select([fd], [], [], remaining)
		if not r:
			continue
		chunk = os.read(fd, 1)
		if not chunk:
			raise AssertionError(f"stream closed before newline; partial={buf!r}")
		if chunk == b"\n":
			return buf.decode()
		buf += chunk


def _compile(tmp_path: Path, source: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "coordinator"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:600]}"
	return out


# Coordinator: a signal VT forwards SIGUSR1 to a worker VT over a channel; the
# worker rescans CONFIG_DIR with read_dir and publishes the entry count into the
# shared Arc<Mutex<Int>> via mem.replace.  Main waits for the published count and
# prints it.  -1 is the "not yet loaded" sentinel.
_COORDINATOR_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.mem as mem;
import std.console as cons;
import std.format as fmt;

// Read the published state under the lock; the guard drops at return, so the
// lock is held only for the read (never across other work).
fn _peek(m: &conc.Mutex<Int>) nothrow -> Int {
\tvar g = conc.lock<type Int>(m);
\treturn g.get_mut();
}

pub fn main() nothrow -> Int {
\t// Published live state: entry count (-1 = not yet loaded).
\tval state: core.Arc<conc.Mutex<Int>> = core.arc<type conc.Mutex<Int>>(conc.mutex<type Int>(0 - 1));
\tval state_worker = state.clone();

\t// Reload-request channel (content-free trigger).
\tvar halves = conc.channel<type Int>();
\tvar sender = halves.take_sender();
\tvar receiver = halves.take_receiver();

\t// Signal VT: the single await_signal waiter; forwards User1 to the worker.
\tvar sig_vt = conc.spawn<type Int>(core.callback0(| | captures(move sender) => {
\t\tmatch conc.await_signal() {
\t\t\tconc.ProcessSignal::User1() => {
\t\t\t\tmatch sender.send(0) { Ok(_) => { }, Err(e) => { } }
\t\t\t\treturn 0;
\t\t\t},
\t\t\tconc.ProcessSignal::Interrupt() => { return 1; },
\t\t\tconc.ProcessSignal::Terminate() => { return 2; }
\t\t}
\t}));

\t// Worker VT: recv -> read_dir -> stage/verify -> atomic swap.
\tvar work_vt = conc.spawn<type Int>(core.callback0(| | captures(move receiver, move state_worker) => {
\t\tmatch receiver.recv() {
\t\t\tOk(_) => {
\t\t\t\tmatch fs.read_dir("__CONFIG_DIR__", conc.Duration(millis = 10000)) {
\t\t\t\t\tOk(entries) => {
\t\t\t\t\t\tval staged = entries.len;   // stage
\t\t\t\t\t\tif staged >= 0 {            // verify (trivial)
\t\t\t\t\t\t\tvar guard = conc.lock<type Int>(state_worker.get());
\t\t\t\t\t\t\tval old = mem.replace<type Int>(guard.get_mut(), staged);
\t\t\t\t\t\t\t// `old` (an Int) drops here; the guard drops at arm end,
\t\t\t\t\t\t\t// publishing the new state atomically.
\t\t\t\t\t\t}
\t\t\t\t\t\treturn 0;
\t\t\t\t\t},
\t\t\t\t\tErr(e) => { return 0 - 1; }
\t\t\t\t}
\t\t\t},
\t\t\tErr(e) => { return 0 - 2; }
\t\t}
\t}));

\t// Joining the worker transitively waits for the whole chain (SIGUSR1 ->
\t// send -> recv -> read_dir -> swap), so the published state is final here.
\tval _s = sig_vt.join();
\tval _w = work_vt.join();
\tval published = _peek(state.get());
\tcons.println("count:" + fmt.format_int(published));
\tif published >= 0 { return 0; }
\treturn 1;
}
"""


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="Linux-only")
def test_reload_coordinator_signal_to_swap(tmp_path: Path) -> None:
	"""SIGUSR1 -> channel -> worker read_dir rescan -> atomic state swap."""
	config = tmp_path / "config"
	config.mkdir()
	(config / "a.conf").write_text("a")
	(config / "b.conf").write_text("b")
	(config / "c.conf").write_text("c")
	source = _COORDINATOR_SOURCE.replace("__CONFIG_DIR__", str(config))
	binary = _compile(tmp_path, source)

	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	time.sleep(0.4)  # let the signal VT park on await_signal
	os.kill(proc.pid, signal.SIGUSR1)
	stdout, stderr = proc.communicate(timeout=15)
	assert proc.returncode == 0, (
		f"expected clean swap, got rc={proc.returncode}\n"
		f"stdout={stdout.decode()!r} stderr={stderr.decode()[:300]}"
	)
	# The worker rescanned the 3-entry config dir and published the count.
	assert stdout.decode() == "count:3\n", stdout.decode()


# Comprehensive reload-coordinator contract: a Destructible old state whose
# destructor RE-LOCKS the live mutex (so a drop inside the critical section would
# self-deadlock — completing proves drop-outside-lock); two sequential SIGUSR1
# reloads with the directory changed between them; and a third reload whose read
# fails, which must leave the published state untouched.
_COORD2_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.mem as mem;
import std.sync as sync;
import std.console as cons;
import std.format as fmt;

fn _so() nothrow -> sync.MemoryOrder { return sync.MemoryOrder::SeqCst(); }

// Destructible displaced state.  destroy() RE-LOCKS the live mutex and bumps a
// drop counter: if it were dropped while the swap still holds the lock this would
// self-deadlock, so reaching `final:` proves the old state dropped OUTSIDE the lock.
pub struct OldState { count: Int, live: core.Arc<conc.Mutex<Int>>, drops: core.Arc<sync.AtomicInt> }
implement core.Destructible for OldState {
\tpub fn destroy(var self: OldState) nothrow -> Void {
\t\tvar g = conc.lock<type Int>(self.live.get());
\t\tval _ = g.get_mut();
\t\tself.drops.get().fetch_add(1, _so());
\t}
}

// Stage -> verify -> atomic swap.  Returns the new count, or -1 if the read/verify
// failed (in which case the live state is left untouched).
fn _reload(dir: String, live: &core.Arc<conc.Mutex<Int>>, drops: &core.Arc<sync.AtomicInt>) nothrow -> Int {
\tmatch fs.read_dir(dir, conc.Duration(millis = 10000)) {
\t\tOk(entries) => {
\t\t\tval staged = entries.len;   // stage
\t\t\tvar old_holder: Optional<OldState> = Optional::None();
\t\t\t{
\t\t\t\tvar g = conc.lock<type Int>(live.get());
\t\t\t\tval oldc = mem.replace<type Int>(g.get_mut(), staged);   // publish under lock
\t\t\t\told_holder = Optional::Some(OldState(count = oldc, live = live.clone(), drops = drops.clone()));
\t\t\t}   // guard released here
\t\t\t// old_holder drops at function return — AFTER the lock is free.
\t\t\tmatch old_holder { Optional::Some(o) => { val _ = o.count; }, Optional::None() => { } }
\t\t\treturn staged;
\t\t},
\t\tErr(e) => { return 0 - 1; }   // read failed -> live untouched
\t}
}

fn _peek(m: &conc.Mutex<Int>) nothrow -> Int { var g = conc.lock<type Int>(m); return g.get_mut(); }

pub fn main() nothrow -> Int {
\tval live: core.Arc<conc.Mutex<Int>> = core.arc<type conc.Mutex<Int>>(conc.mutex<type Int>(0 - 1));
\tval live_w = live.clone();
\tval drops: core.Arc<sync.AtomicInt> = core.arc<type sync.AtomicInt>(sync.atomic_int(0));
\tval drops_w = drops.clone();
\tvar halves = conc.channel<type Int>();
\tvar sender = halves.take_sender();
\tvar receiver = halves.take_receiver();
\tvar sig_vt = conc.spawn<type Int>(core.callback0(| | captures(move sender) => {
\t\tvar k = 0;
\t\twhile k < 3 {
\t\t\tmatch conc.await_signal() {
\t\t\t\tconc.ProcessSignal::User1() => { match sender.send(0) { Ok(_) => { }, Err(e) => { } } },
\t\t\t\tconc.ProcessSignal::Interrupt() => { return 1; },
\t\t\t\tconc.ProcessSignal::Terminate() => { return 2; }
\t\t\t}
\t\t\tk = k + 1;
\t\t}
\t\treturn 0;
\t}));
\tvar work_vt = conc.spawn<type Int>(core.callback0(| | captures(move receiver, move live_w, move drops_w) => {
\t\tvar k = 0;
\t\twhile k < 3 {
\t\t\tmatch receiver.recv() {
\t\t\t\tOk(_) => {
\t\t\t\t\tval c = _reload("__DIR__", &live_w, &drops_w);
\t\t\t\t\t// Per-reload ack on stderr (unbuffered): the test reads this to
\t\t\t\t\t// confirm reload k observed exactly `c` entries BEFORE mutating the
\t\t\t\t\t// directory for reload k+1 — no timing sleeps.
\t\t\t\t\tcons.eprintln("ack:" + fmt.format_int(c));
\t\t\t\t},
\t\t\t\tErr(e) => { k = 3; }
\t\t\t}
\t\t\tk = k + 1;
\t\t}
\t\treturn 0;
\t}));
\t// Readiness gate: the process-wide SIGUSR1 mask + signalfd are installed before
\t// any carrier runs, so once user code prints this the test may send the first
\t// signal; every later SIGUSR1 is captured pending even before the signal VT
\t// re-parks on await_signal.  This removes all inter-reload timing sleeps.
\tcons.eprintln("ready");
\tval _s = sig_vt.join();
\tval _w = work_vt.join();
\tcons.println("final:" + fmt.format_int(_peek(live.get())) + " drops:" + fmt.format_int(drops.get().load(_so())));
\treturn 0;
}
"""


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="Linux-only")
def test_reload_coordinator_sequential_reloads_and_failure(tmp_path: Path) -> None:
	"""Two sequential SIGUSR1 reloads with the directory changed between them, a
	third reload whose read fails (state left untouched), and a Destructible old
	state dropped outside the lock (its destructor re-locks; completing = no
	self-deadlock = drop-outside-lock)."""
	cfg = tmp_path / "config"
	cfg.mkdir()
	(cfg / "a.conf").write_text("a")
	(cfg / "b.conf").write_text("b")  # 2 entries
	source = _COORD2_SOURCE.replace("__DIR__", str(cfg))
	binary = _compile(tmp_path, source)

	import shutil as _shutil

	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	try:
		# Gate on the runtime being up (mask + signalfd installed) — not a sleep.
		assert _read_line(proc.stderr, 15) == "ready"

		# Reload 1: directory has exactly 2 entries.  Observing "ack:2" PROVES the
		# worker read the 2-entry directory before we add the third file — a delayed
		# reload reading 3 would surface here as "ack:3" and fail.
		os.kill(proc.pid, signal.SIGUSR1)
		assert _read_line(proc.stderr, 15) == "ack:2", "reload 1 did not observe exactly 2 entries"

		# Mutate the directory to 3 entries only AFTER reload 1 acknowledged 2.
		(cfg / "c.conf").write_text("c")
		os.kill(proc.pid, signal.SIGUSR1)
		assert _read_line(proc.stderr, 15) == "ack:3", "reload 2 did not observe the added entry"

		# Remove the directory only AFTER reload 2 acknowledged 3; reload 3's read
		# must fail and leave the published state untouched.
		_shutil.rmtree(cfg)
		os.kill(proc.pid, signal.SIGUSR1)
		assert _read_line(proc.stderr, 15) == "ack:-1", "reload 3 should have failed the read"

		stdout, _stderr = proc.communicate(timeout=15)
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.communicate()
	assert proc.returncode == 0, f"rc={proc.returncode}"
	# live=3 (the 2nd reload's count; the failed 3rd left it untouched);
	# drops=2 (two successful swaps each dropped one old state, OUTSIDE the lock).
	assert stdout.decode() == "final:3 drops:2\n", stdout.decode()
