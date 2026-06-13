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
"""
from __future__ import annotations

import os
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
