# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Receiver<T>::destroy must NOT hold the channel state mutex while
running queued `T` destructors.

`T` is unrestricted (no `Send` bound in v1; documented future
constraint).  A queued payload's `Destructible::destroy` is
arbitrary user code that may re-enter the channel — for example
by releasing the last `Arc<Sender<T>>` for this channel, which
fires `Sender::destroy` and tries to re-acquire the same state
mutex.  Drift's `Mutex<T>` is spin-CAS, so the observable failure
mode is an infinite CPU spin (process never terminates) rather
than a futex deadlock.

The fix in `concurrent.drift` Receiver destructor: under the
lock, set `receiver_closed = true` and `mem.replace` the queue
out into a local `detached: Array<T>` (O(1) pointer swap); release
the guard; let the `detached` array drop at fn-exit, which runs
each `T::destroy` outside the lock.  Re-entrant destructors can
then safely re-acquire the channel state mutex.

This regression is the failing test the fix is gated on: pre-fix
the binary spins indefinitely on the CAS retry inside
`Sender::destroy` for the last-Arc<Sender> drop nested inside the
queued payload's destructor.  Post-fix the binary completes in
~milliseconds.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout


ROOT = Path(__file__).resolve().parents[3]


_SOURCE = """\
module main;
import std.concurrent as conc;
import std.core as core;
import std.core.arc as arc;
import std.console as console;

pub struct SenderCarrier
\trequire Self is core.Destructible
{
\tpub sender_clone: arc.Arc<conc.Sender<SenderCarrier>>
}

implement core.Destructible for SenderCarrier {
\tpub fn destroy(var self: SenderCarrier) nothrow -> Void {
\t\t/* sender_clone Arc drops as part of struct teardown; if it's
\t\t * the last clone, Sender::destroy fires and tries to lock
\t\t * the channel state mutex.  Pre-fix that mutex is already
\t\t * held by Receiver::destroy → spin-CAS forever.  Post-fix
\t\t * this is dropped outside the lock. */
\t}
}

pub fn main() nothrow -> Int {
\tvar halves = conc.channel<type SenderCarrier>();
\tval sender_arc = arc.arc(halves.take_sender());
\tvar receiver = halves.take_receiver();

\t/* Queue a SenderCarrier holding a clone of the channel's own
\t * Arc<Sender>.  Two clones exist post-send: `sender_arc` (main)
\t * and the one inside the queued SenderCarrier. */
\tval carrier_clone = sender_arc.clone();
\tval _ = sender_arc.get().send(SenderCarrier(
\t\tsender_clone = move carrier_clone
\t));
\tconsole.eprintln("repro:sent");

\t/* Drop main's Arc<Sender>; the queued SenderCarrier now holds
\t * the LAST Arc<Sender> for this channel. */
\t{ val _ = move sender_arc; }
\tconsole.eprintln("repro:dropped-main-sender");

\t/* Drop the receiver.  Receiver::destroy must:
\t *   1. Lock channel state mutex.
\t *   2. Set receiver_closed = true.
\t *   3. Detach (mem.replace) the queued payloads OUT of the
\t *      state.queue array — into a detached local array.
\t *   4. Release the lock.
\t *   5. Drop the detached local → each SenderCarrier::destroy
\t *      fires → Arc<Sender> drop → Sender::destroy → lock the
\t *      (now-released) state mutex cleanly.
\t * If step 3+4 are reversed (drop under the lock), Sender::destroy
\t * in step 5 re-enters the held lock → spin-CAS hang. */
\t{ val _ = move receiver; }
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
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, (
		f"compile failed: rc={res.returncode}\n"
		f"stderr: {res.stderr[:800]}"
	)
	assert out.exists()
	return out


def test_receiver_destroy_must_not_hold_lock_during_payload_destruction(tmp_path: Path) -> None:
	"""Pre-fix this hangs on the spin-CAS retry inside
	Sender::destroy for the last-Arc<Sender> drop nested inside the
	queued payload's destructor; post-fix it completes
	immediately."""
	binary = _compile(tmp_path)
	try:
		res = subprocess.run(
			[str(binary)],
			capture_output=True, text=True,
			timeout=sanitizer_timeout(10),
		)
	except subprocess.TimeoutExpired as ex:
		stderr_seen = (ex.stderr or b"").decode("utf-8", errors="replace") if isinstance(ex.stderr, bytes) else (ex.stderr or "")
		stdout_seen = (ex.stdout or b"").decode("utf-8", errors="replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
		raise AssertionError(
			"Receiver::destroy hung (10s timeout) — the queued "
			"payload's destructor re-entered the channel state "
			"mutex while Receiver::destroy still held it.  The "
			"fix is to detach the queued values via mem.replace "
			"under the lock, then drop them outside the lock.\n"
			f"stderr (partial): {stderr_seen[:800]}\n"
			f"stdout (partial): {stdout_seen[:800]}"
		)
	combined = res.stderr + "\n" + res.stdout
	assert "repro:done" in combined, (
		f"Receiver::destroy did not reach `done`.\n"
		f"stderr: {res.stderr[-800:]}"
	)
	assert res.returncode == 0, (
		f"unexpected rc={res.returncode}\n"
		f"stderr: {res.stderr[-800:]}"
	)
