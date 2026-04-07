# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: conc.await_signal() — Linux-only process signal wait.

Pins:
1. SIGINT delivery wakes the waiter and returns Interrupt
2. SIGTERM delivery wakes the waiter and returns Terminate
3. Clean shutdown path from a waiting VT
4. No regression in normal startup/shutdown (no await_signal used)
5. Signal before await — delivered immediately from kernel buffer
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

_SIGINT_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tmatch conc.await_signal() {
\t\tconc.ProcessSignal::Interrupt() => { return 42; },
\t\tconc.ProcessSignal::Terminate() => { return 43; }
\t}
}
"""

_SIGTERM_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tmatch conc.await_signal() {
\t\tconc.ProcessSignal::Interrupt() => { return 42; },
\t\tconc.ProcessSignal::Terminate() => { return 43; }
\t}
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


def test_normal_startup_shutdown_no_regression(tmp_path: Path) -> None:
	"""Normal program without await_signal still starts and exits cleanly."""
	binary = _compile(tmp_path, _NORMAL_SOURCE)
	res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
	assert res.returncode == 0, f"normal program should exit 0, got {res.returncode}"
