# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Stress regression for `std.concurrent.channel<T>` close/send races.

The channel's protocol must linearize through one mutex so that no
combination of concurrent `send()` and `Receiver` drop can leave a
payload in an unowned slot.  The conservation invariant under test:

  For every payload constructed by a producer, exactly one of three
  outcomes is observed:
    (a) the receiver pulled it via `recv()` and the consumer dropped it;
    (b) `send()` returned Err(CLOSED) and the moved-in payload was
        dropped at the failed-send path (Sender::send drops `var v: T`
        on the CLOSED return);
    (c) the value was queued before the receiver closed, then the
        Receiver's destructor drained-and-destroyed it.

Each payload's `Destructible::destroy` bumps a shared atomic counter.
At end of run, total destructions == total payloads produced.  Any
double-release would show up as either a SIGABRT in
`drift_string_release`-style underflow (we use a plain Int counter
to avoid that machinery here) or as an overshoot of the destruction
counter past the produced count.  An unowned slot would show up as
an undershoot.

This complements the codegen e2e channel cases (single-threaded,
deterministic) with multi-producer + concurrent-receiver-drop
contention.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout


def _asan_active() -> bool:
	return os.environ.get("DRIFT_ASAN") in ("1", "true", "True")


ROOT = Path(__file__).resolve().parents[3]


# 4 producer VTs each send N payloads through `Arc<Sender>`; a
# separate "killer" VT drops the receiver after a small delay,
# concurrent with the producers still trying to send.  Conservation
# invariant is asserted at end of run from the bookkeeping counter.
_SOURCE = """\
module main;
import std.concurrent as conc;
import std.core as core;
import std.core.arc as arc;
import std.console as console;
import std.mem as mem;
import lang.atomic as atomic;

pub struct Payload
\trequire Self is core.Destructible
{
\tpub id: Int,
\tpub bookkeeping: arc.Arc<conc.AtomicInt>
}

implement core.Destructible for Payload {
\tpub fn destroy(var self: Payload) nothrow -> Void {
\t\tval _ = atomic.atomic_fetch_add_int(&self.bookkeeping.get(), 1, 2);
\t}
}

pub struct Sigs {
\tpub kill: conc.AtomicBool
}

pub fn main() nothrow -> Int {
\t/* Use a dedicated executor with enough carriers for 4 producers
\t * + 1 killer + 1 reaper.  Otherwise the spinning producers (which
\t * call atomic.atomic_fetch_add — non-parking) could starve the
\t * killer VT. */
\tvar b = conc.executor_policy_builder();
\tb.min_threads(8);
\tb.max_threads(8);
\tval exec = b.build_executor();
\tconc.set_default_executor(exec);

\t/* `produced_count` is the count of Payloads ever constructed;
\t * `destroyed_count` is the count of Destructible::destroy calls.
\t * Each producer creates `created++` BEFORE constructing each
\t * Payload; each Payload's destroy fires exactly once on its own
\t * drop. */
\tval produced_count: arc.Arc<conc.AtomicInt> = arc.arc(conc.atomic_int(0));
\tval destroyed_count: arc.Arc<conc.AtomicInt> = arc.arc(conc.atomic_int(0));

\tvar halves = conc.channel<type Payload>();
\tval sender_arc: arc.Arc<conc.Sender<Payload>> = arc.arc(halves.take_sender());
\tvar receiver_opt: Optional<conc.Receiver<Payload>> = Optional<type conc.Receiver<Payload>>::Some(halves.take_receiver());

\tval sigs: arc.Arc<Sigs> = arc.arc(Sigs(kill = conc.atomic_bool(false)));

\tval per_producer: Int = 25;

\t/* 4 producers — inline cb body so we don't have to thread
\t * arguments through a separate fn (avoids closure-capture
\t * gymnastics for the Int `count` and `base_id`). */
\tval p1_send = sender_arc.clone();
\tval p1_dest = destroyed_count.clone();
\tval p1_prod = produced_count.clone();
\tval p1_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move p1_send, move p1_dest, move p1_prod, copy per_producer) => {
\t\t\tvar i: Int = 0;
\t\t\twhile i < per_producer {
\t\t\t\tval _ = atomic.atomic_fetch_add_int(&p1_prod.get(), 1, 2);
\t\t\t\tval p = Payload(id = i, bookkeeping = p1_dest.clone());
\t\t\t\tval _ = p1_send.get().send(p);
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\treturn 0;
\t\t}
\t);
\tvar p1 = conc.spawn(move p1_cb);

\tval p2_send = sender_arc.clone();
\tval p2_dest = destroyed_count.clone();
\tval p2_prod = produced_count.clone();
\tval p2_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move p2_send, move p2_dest, move p2_prod, copy per_producer) => {
\t\t\tvar i: Int = 0;
\t\t\twhile i < per_producer {
\t\t\t\tval _ = atomic.atomic_fetch_add_int(&p2_prod.get(), 1, 2);
\t\t\t\tval p = Payload(id = 1000 + i, bookkeeping = p2_dest.clone());
\t\t\t\tval _ = p2_send.get().send(p);
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\treturn 0;
\t\t}
\t);
\tvar p2 = conc.spawn(move p2_cb);

\tval p3_send = sender_arc.clone();
\tval p3_dest = destroyed_count.clone();
\tval p3_prod = produced_count.clone();
\tval p3_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move p3_send, move p3_dest, move p3_prod, copy per_producer) => {
\t\t\tvar i: Int = 0;
\t\t\twhile i < per_producer {
\t\t\t\tval _ = atomic.atomic_fetch_add_int(&p3_prod.get(), 1, 2);
\t\t\t\tval p = Payload(id = 2000 + i, bookkeeping = p3_dest.clone());
\t\t\t\tval _ = p3_send.get().send(p);
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\treturn 0;
\t\t}
\t);
\tvar p3 = conc.spawn(move p3_cb);

\tval p4_send = sender_arc.clone();
\tval p4_dest = destroyed_count.clone();
\tval p4_prod = produced_count.clone();
\tval p4_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move p4_send, move p4_dest, move p4_prod, copy per_producer) => {
\t\t\tvar i: Int = 0;
\t\t\twhile i < per_producer {
\t\t\t\tval _ = atomic.atomic_fetch_add_int(&p4_prod.get(), 1, 2);
\t\t\t\tval p = Payload(id = 3000 + i, bookkeeping = p4_dest.clone());
\t\t\t\tval _ = p4_send.get().send(p);
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\treturn 0;
\t\t}
\t);
\tvar p4 = conc.spawn(move p4_cb);

\t/* Killer VT: waits briefly, then drops the receiver concurrent
\t * with the still-running producers.  Receiver-drop drains the
\t * queue (destructibles fire on each queued payload). */
\tval sigs_killer = sigs.clone();
\tval kill_cb: core.Callback0<Int> = core.callback0(
\t\t| | captures(move sigs_killer) => {
\t\t\tval _ = conc.sleep(conc.Duration(millis = 15));
\t\t\tatomic.atomic_store_bool(&sigs_killer.get().kill, true, 2);
\t\t\treturn 0;
\t\t}
\t);
\tvar killer = conc.spawn(move kill_cb);

\t/* Main: wait for killer's signal, then drop receiver. */
\tvar spins: Int = 0;
\twhile !atomic.atomic_load_bool(&sigs.get().kill, 1) {
\t\tval _ = conc.sleep(conc.Duration(millis = 2));
\t\tspins = spins + 1;
\t\tif spins > 2000 { return 90; }
\t}

\t/* Drop the receiver, draining whatever's queued. */
\tvar receiver_taken = mem.replace(
\t\t&mut receiver_opt,
\t\tOptional<type conc.Receiver<Payload>>::None()
\t);
\tmatch receiver_taken {
\t\tSome(r) => {
\t\t\tvar drop_r = move r;
\t\t\t/* drop_r drops at end of arm — receiver_closed = true
\t\t\t * + queue drained */
\t\t},
\t\tNone => { return 91; },
\t\tdefault => { return 92; }
\t}

\t/* Join producers + killer. */
\tval _ = p1.join();
\tval _ = p2.join();
\tval _ = p3.join();
\tval _ = p4.join();
\tval _ = killer.join();

\t/* Drop all sender Arc clones — sender_arc decremented to 0
\t * when this scope ends (we're the only owner). */
\tvar root_drop = move sender_arc;
\tval _ = move root_drop;
\t/* (Note: the producer cbs each captured-by-move their own
\t * Arc<Sender> clone; those drop when the cbs return, which
\t * already happened by the .join() above.) */

\tval produced = atomic.atomic_load_int(&produced_count.get(), 1);
\tval destroyed = atomic.atomic_load_int(&destroyed_count.get(), 1);
\tconsole.eprintln("done");
\tif produced != destroyed {
\t\t/* Overshoot ≥ +1 (double-release) → exit 50+drift;
\t\t * Undershoot ≥ -1 (lost payload) → exit 100+drift. */
\t\tval drift = destroyed - produced;
\t\tif drift > 0 {
\t\t\treturn 50 + drift;
\t\t}
\t\treturn 100 + (0 - drift);
\t}
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


def test_channel_close_race_conservation(tmp_path: Path) -> None:
	"""4 producers × 25 sends each (100 payloads total) racing the
	receiver drop.  Conservation invariant: every constructed
	payload's `Destructible::destroy` fires exactly once."""
	binary = _compile(tmp_path)
	res = subprocess.run(
		[str(binary)],
		capture_output=True, text=True,
		timeout=sanitizer_timeout(20),
	)
	combined = res.stderr + "\n" + res.stdout
	assert "done" in combined, (
		f"binary did not reach `done` marker.\n"
		f"stderr: {res.stderr[-1500:]}"
	)
	assert res.returncode == 0, (
		f"conservation invariant violated; rc={res.returncode}.\n"
		f"  rc 50+N → destruction overshoot by N (double-release / "
		f"double-drop)\n"
		f"  rc 100+N → destruction undershoot by N (stranded payload)\n"
		f"stderr: {res.stderr[-1500:]}"
	)


@pytest.mark.skipif(shutil.which("valgrind") is None,
	reason="valgrind not installed — memcheck required")
@pytest.mark.skipif(_asan_active(),
	reason="ASan-instrumented binaries cannot run under Valgrind")
def test_channel_close_race_valgrind_clean(tmp_path: Path) -> None:
	"""Same shape under memcheck — no invalid accesses, no leaks
	from the closed-while-sending race."""
	binary = _compile(tmp_path)
	res = subprocess.run(
		["valgrind", "--tool=memcheck", "--leak-check=full",
		 "--show-leak-kinds=definite", "--error-exitcode=77",
		 str(binary)],
		capture_output=True, text=True,
		timeout=sanitizer_timeout(120),
	)
	combined = res.stderr + "\n" + res.stdout
	bad_markers = [m for m in (
		"Invalid write", "Invalid read", "Invalid free",
		"Conditional jump or move depends on uninitialised",
		"definitely lost: ",
	) if m in combined]
	# `definitely lost: 0 bytes in 0 blocks` is fine; filter on
	# nonzero leak.
	if "definitely lost: " in combined:
		# Crude parse — if the byte-count is 0, drop the marker.
		for line in res.stderr.splitlines():
			if "definitely lost:" in line and " 0 bytes " in line:
				if "definitely lost: " in bad_markers:
					bad_markers.remove("definitely lost: ")
				break
	assert not bad_markers, (
		f"valgrind reported {bad_markers}.  Conservation may be ok "
		f"but memory safety is not.\n"
		f"Excerpt:\n{combined[-2000:]}"
	)
	assert res.returncode == 0, (
		f"valgrind exited {res.returncode}.\n"
		f"stderr: {res.stderr[-2000:]}"
	)
