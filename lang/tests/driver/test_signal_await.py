# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: conc.await_signal() — Linux-only process signal wait.

Pins:
1. SIGINT delivery wakes the waiter and returns Interrupt
2. SIGTERM delivery wakes the waiter and returns Terminate
3. SIGUSR1 delivery wakes the waiter and returns User1
4. Clean shutdown path from a waiting VT
5. No regression in normal startup/shutdown (no await_signal used)
6. Signal before await — delivered immediately from kernel buffer
7. Second concurrent waiter aborts with the single-waiter diagnostic
8. Signal delivered with NO waiter ever registered does not busy-spin
   (signalfd is level-triggered; the reactor must drain it even with no
   waiter, or epoll_wait returns immediately forever — see
   drift_reactor_drain_signal_fd in thread_runtime.c)
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

# All three fixtures share the same match (exit 42=Interrupt, 43=Terminate,
# 44=User1); each test delivers a different signal and asserts the mapping.
_AWAIT_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tmatch conc.await_signal() {
\t\tconc.ProcessSignal::Interrupt() => { return 42; },
\t\tconc.ProcessSignal::Terminate() => { return 43; },
\t\tconc.ProcessSignal::User1() => { return 44; }
\t}
}
"""

_SIGINT_SOURCE = _AWAIT_SOURCE
_SIGTERM_SOURCE = _AWAIT_SOURCE
_SIGUSR1_SOURCE = _AWAIT_SOURCE

# Two VTs both call await_signal(); the single-waiter CAS lets exactly one
# park and the other receives the runtime's -1, which the (now contract-
# honoring) await_signal turns into an assert/abort that takes down the
# whole process.  No signal is ever delivered — the abort is what ends it.
_SECOND_WAITER_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

fn _waiter() nothrow -> Int {
\tmatch conc.await_signal() {
\t\tconc.ProcessSignal::Interrupt() => { return 1; },
\t\tconc.ProcessSignal::Terminate() => { return 2; },
\t\tconc.ProcessSignal::User1() => { return 3; }
\t}
}

pub fn main() nothrow -> Int {
\tvar a = conc.spawn<type Int>(core.callback0(| | => { return _waiter(); }));
\tvar b = conc.spawn<type Int>(core.callback0(| | => { return _waiter(); }));
\tval _ra = a.join();
\tval _rb = b.join();
\treturn 0;
}
"""

_NORMAL_SOURCE = """\
module main;
import std.core as core;

pub fn main() nothrow -> Int {
\treturn 0;
}
"""

# No await_signal() call anywhere in the program — nothing ever registers as
# the signal waiter.  A signal delivered here must not make the reactor spin.
# Prints "ready" to stderr (unbuffered in the runtime) BEFORE the long sleep,
# so the parent can observe true readiness instead of guessing with a sleep.
_NO_WAITER_SOURCE = """\
module main;
import std.core as core;
import std.console as console;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tconsole.eprintln("ready");
\tconc.sleep(conc.Duration(millis = 60000));
\treturn 0;
}
"""

# Signal is sent before this VT ever calls await_signal(); the 300ms sleep
# gives the sender a comfortable window to deliver before the call runs. The
# "ready" marker (see _NO_WAITER_SOURCE) proves the runtime has already
# blocked SIGINT/SIGTERM/SIGUSR1 (drift_run_main_on_vt does this before
# main() runs), so the parent's SIGTERM cannot race the runtime's own
# startup and hit the pre-block default disposition instead.
_DELAYED_AWAIT_SOURCE = """\
module main;
import std.core as core;
import std.console as console;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tconsole.eprintln("ready");
\tconc.sleep(conc.Duration(millis = 300));
\tmatch conc.await_signal() {
\t\tconc.ProcessSignal::Interrupt() => { return 42; },
\t\tconc.ProcessSignal::Terminate() => { return 43; },
\t\tconc.ProcessSignal::User1() => { return 44; }
\t}
}
"""


def _read_line(stream, timeout_s: float) -> str:
	"""Read one newline-terminated line from `stream` within `timeout_s`.

	stderr is unbuffered in the runtime, so the "ready" marker arrives as
	soon as it's written. Using select (not a fixed sleep) makes readiness an
	observed fact, not a timing assumption — and a stuck child fails the test
	with a clear timeout instead of hanging or racing."""
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


def _cpu_ticks(pid: int) -> int:
	"""utime+stime (clock ticks) for pid, from /proc/<pid>/stat.  comm may
	contain spaces/parens, so split on the LAST ')' per proc(5)."""
	with open(f"/proc/{pid}/stat") as f:
		fields = f.read().rsplit(")", 1)[1].split()
	# fields[0] is state (proc field 3); utime/stime are proc fields 14/15.
	return int(fields[11]) + int(fields[12])


def _compile(tmp_path: Path, source: str, name: str = "test_bin") -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:300]}"
	assert out.exists()
	return out


@pytest.mark.skipif(not hasattr(signal, "SIGINT"), reason="Linux-only")
def test_sigint_wakes_waiter(tmp_path: Path) -> None:
	"""SIGINT delivery wakes await_signal and returns Interrupt (exit 42)."""
	binary = _compile(tmp_path, _SIGINT_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	time.sleep(0.3)  # let the VT park on await_signal
	os.kill(proc.pid, signal.SIGINT)
	stdout, stderr = proc.communicate(timeout=5)
	assert proc.returncode == 42, (
		f"expected exit 42 (Interrupt), got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:200]}"
	)


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="Linux-only")
def test_sigterm_wakes_waiter(tmp_path: Path) -> None:
	"""SIGTERM delivery wakes await_signal and returns Terminate (exit 43)."""
	binary = _compile(tmp_path, _SIGTERM_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	time.sleep(0.3)
	os.kill(proc.pid, signal.SIGTERM)
	stdout, stderr = proc.communicate(timeout=5)
	assert proc.returncode == 43, (
		f"expected exit 43 (Terminate), got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:200]}"
	)


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="Linux-only")
def test_sigusr1_wakes_waiter(tmp_path: Path) -> None:
	"""SIGUSR1 delivery wakes await_signal and returns User1 (exit 44)."""
	binary = _compile(tmp_path, _SIGUSR1_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	time.sleep(0.3)  # let the VT park on await_signal
	os.kill(proc.pid, signal.SIGUSR1)
	stdout, stderr = proc.communicate(timeout=5)
	assert proc.returncode == 44, (
		f"expected exit 44 (User1), got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:200]}"
	)


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="Linux-only")
def test_sigusr2_still_drives_liveness_not_await(tmp_path: Path) -> None:
	"""SIGUSR1/SIGUSR2 are not cross-wired: SIGUSR2 is consumed by the
	liveness interrogator (sigwait thread), never surfaced through
	await_signal.  Sending SIGUSR2 to a parked await_signal waiter must
	NOT wake it as a ProcessSignal; a following SIGUSR1 still returns
	User1 (exit 44)."""
	binary = _compile(tmp_path, _SIGUSR1_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	time.sleep(0.3)
	os.kill(proc.pid, signal.SIGUSR2)  # liveness path — must not wake await_signal
	time.sleep(0.3)
	assert proc.poll() is None, "SIGUSR2 wrongly woke/terminated the await_signal waiter"
	os.kill(proc.pid, signal.SIGUSR1)  # the real wake
	stdout, stderr = proc.communicate(timeout=5)
	assert proc.returncode == 44, (
		f"expected exit 44 (User1) after SIGUSR2 ignored, got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:200]}"
	)


@pytest.mark.skipif(not hasattr(signal, "SIGINT"), reason="Linux-only")
def test_second_concurrent_waiter_aborts(tmp_path: Path) -> None:
	"""Two VTs both calling await_signal: the single-waiter contract makes
	the second one abort with a diagnostic (process dies via SIGABRT), per
	the documented 'misuse aborts' contract — never a fabricated Interrupt."""
	binary = _compile(tmp_path, _SECOND_WAITER_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	stdout, stderr = proc.communicate(timeout=10)
	# abort() raises SIGABRT (6) -> negative returncode -6, or 134 if shell-wrapped.
	assert proc.returncode in (-signal.SIGABRT, 134), (
		f"expected SIGABRT from second-waiter abort, got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:300]}"
	)
	assert b"await_signal" in stderr or b"single-waiter" in stderr, (
		f"expected single-waiter diagnostic on stderr, got: {stderr.decode()[:300]}"
	)


def test_normal_startup_shutdown_no_regression(tmp_path: Path) -> None:
	"""Normal program without await_signal still starts and exits cleanly."""
	binary = _compile(tmp_path, _NORMAL_SOURCE)
	res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
	assert res.returncode == 0, f"normal program should exit 0, got {res.returncode}"


@pytest.mark.skipif(
	not hasattr(signal, "SIGTERM") or sys.platform != "linux",
	reason="Linux-only (reads /proc/<pid>/stat, which only exists there)",
)
def test_sigterm_no_waiter_does_not_busy_spin(tmp_path: Path) -> None:
	"""A program that never calls await_signal() must not busy-spin when
	SIGTERM arrives.  signalfd is level-triggered: pre-fix, the reactor left
	an unread signal readable on the fd whenever there was no waiter, so
	epoll_wait returned immediately forever (one core pegged, ~100k+
	epoll_wait/sec, process never exits on SIGTERM/SIGINT).  This measures
	process CPU time (utime+stime from /proc/<pid>/stat) across a wall-clock
	window after delivery: a busy-spin consumes ~the whole window in CPU
	time, a correctly-blocked reactor consumes ~none of it."""
	binary = _compile(tmp_path, _NO_WAITER_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	try:
		assert _read_line(proc.stderr, 15) == "ready"
		ticks_before = _cpu_ticks(proc.pid)
		os.kill(proc.pid, signal.SIGTERM)
		time.sleep(0.8)
		assert proc.poll() is None, "process exited unexpectedly during the spin-check window"
		ticks_after = _cpu_ticks(proc.pid)
		clk_tck = os.sysconf("SC_CLK_TCK")
		cpu_seconds = (ticks_after - ticks_before) / clk_tck
		assert cpu_seconds < 0.3, (
			f"process burned {cpu_seconds:.3f}s of CPU over a 0.8s window after "
			f"SIGTERM with no await_signal() waiter — looks like the signalfd "
			f"busy-spin regression (epoll_wait returning immediately forever)"
		)
	finally:
		proc.kill()
		proc.communicate(timeout=5)


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="Linux-only")
def test_signal_before_await_delivered_to_later_waiter(tmp_path: Path) -> None:
	"""Pin 6: SIGTERM sent before the VT ever calls await_signal() (during
	its initial 300ms sleep) must still be observed once await_signal() runs
	— the pending signal is stashed, not lost, and not re-requested from the
	kernel (non-realtime signals don't queue)."""
	binary = _compile(tmp_path, _DELAYED_AWAIT_SOURCE)
	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	# Wait for the "ready" marker (printed before the 300ms sleep) instead of
	# guessing with a fixed delay — proves the runtime has already blocked
	# SIGINT/SIGTERM/SIGUSR1 before we deliver, so this can't race the
	# runtime's own startup and hit the pre-block default disposition.
	assert _read_line(proc.stderr, 15) == "ready"
	start = time.monotonic()
	os.kill(proc.pid, signal.SIGTERM)  # delivered well before the 300ms sleep elapses
	stdout, stderr = proc.communicate(timeout=5)
	elapsed = time.monotonic() - start
	assert proc.returncode == 43, (
		f"expected exit 43 (Terminate) from the stashed pre-await signal, got {proc.returncode}\n"
		f"stderr: {stderr.decode()[:200]}"
	)
	# The program's own sleep is 300ms; await_signal() must return the
	# stashed signal immediately once it runs, not wait for a NEW signal
	# (which would never come — non-realtime signals don't queue) or an
	# extra reactor cycle. Generous bound for compile/startup + sanitizer
	# jitter, but well short of the 5s communicate() timeout.
	assert elapsed < 2.0, (
		f"expected prompt delivery of the pre-await signal (~0.3s sleep), "
		f"took {elapsed:.2f}s — await_signal() may not be checking the "
		f"pending-signal stash immediately"
	)
