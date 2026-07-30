# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: runtime liveness interrogator (Slice 1, passive dump plumbing).

Pins the operator-facing snapshot triggered by `kill -USR2 <pid>`:

1. SIGUSR2 produces a `drift.liveness.v1` JSON dump at the configured path.
2. The dump enumerates VTs with the right scheduler states / wait reasons:
   a sleeper (PARKED_TIMER), a joiner (PARKED_JOIN), and a condvar waiter
   (PARKED_CONDVAR) — covering the cold-stuck case across wait kinds.
3. The bounded stderr summary is emitted with the `[drift:liveness]` prefix.
4. `DRIFT_LIVENESS_TEXT=0` suppresses the stderr summary but still writes JSON.
5. A normal program (no signal) is unaffected — exits cleanly.

These drive an external process and use a short settle delay before signaling,
matching the existing `test_signal_await.py` convention; the subprocess
timeouts are the outer guard.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# A program that parks VTs in three distinct, indefinitely-blocked states and
# keeps the main VT alive, so an external SIGUSR2 catches a stable snapshot.
_PARKED_STATES_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\t// VT parked on a timer (PARKED_TIMER).
\tvar sleeper = conc.spawn(| | => {
\t\tmatch conc.sleep(conc.Duration(millis = 600000)) { default => { } }
\t\treturn 0;
\t});

\t// A long-sleeping child + a VT that joins it (joiner -> PARKED_JOIN).
\tvar child = conc.spawn(| | => {
\t\tmatch conc.sleep(conc.Duration(millis = 600000)) { default => { } }
\t\treturn 0;
\t});
\tvar joiner = conc.spawn_cb(core.callback0(| | captures(move child) nothrow => {
\t\tchild.join();
\t\treturn 0;
\t}));

\t// VT parked on a condvar that is never signaled (PARKED_CONDVAR).
\tval cv_arc = conc.arc(conc.condvar());
\tval pred_lock = conc.arc(conc.mutex(false));
\tval cv1 = cv_arc.clone();
\tval m1 = pred_lock.clone();
\tvar waiter = conc.spawn_cb(core.callback0(| | captures(move cv1, move m1) nothrow => {
\t\tvar g = m1.get().lock();
\t\tval v = g.get_mut();
\t\twhile not *v {
\t\t\tmatch cv1.get().wait(g) {
\t\t\t\tcore.Result::Ok(_) => { },
\t\t\t\tcore.Result::Err(_) => { return 1; }
\t\t\t}
\t\t}
\t\treturn 0;
\t}));

\t// Keep the process alive for the operator dump; the test kills it.
\tmatch conc.sleep(conc.Duration(millis = 600000)) { default => { } }
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

# Minimal program that stays alive (main parked on a long sleep) so an external
# SIGUSR2 catches a running process — used by the JSON-write-failure tests where
# the VT states don't matter, only the emit/error path does.
_ALIVE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;

pub fn main() nothrow -> Int {
\tmatch conc.sleep(conc.Duration(millis = 600000)) { default => { } }
\treturn 0;
}
"""


def _compile(tmp_path: Path, source: str, name: str = "live_bin") -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(180),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:500]}"
	assert out.exists()
	return out


def _wait_for_json(path: Path, timeout_s: float = 8.0) -> dict:
	"""Poll for the dump file to appear and parse as JSON."""
	deadline = time.time() + timeout_s
	last_err = None
	while time.time() < deadline:
		if path.exists() and path.stat().st_size > 0:
			try:
				return json.loads(path.read_text())
			except json.JSONDecodeError as e:  # mid-write; retry
				last_err = e
		time.sleep(0.05)
	raise AssertionError(f"liveness JSON not produced at {path} (last err: {last_err})")


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="Linux-only")
def test_sigusr2_dump_reports_parked_states(tmp_path: Path) -> None:
	binary = _compile(tmp_path, _PARKED_STATES_SOURCE)
	dump = tmp_path / "live.json"
	env = dict(os.environ)
	env["DRIFT_LIVENESS_JSON_PATH"] = str(dump)

	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
	                        stderr=subprocess.PIPE, env=env)
	try:
		time.sleep(0.6)  # settle: let the spawned VTs reach their parked states
		os.kill(proc.pid, signal.SIGUSR2)
		snap = _wait_for_json(dump)
	finally:
		proc.kill()
		_out, err = proc.communicate(timeout=10)

	assert snap["schema"] == "drift.liveness.v1"
	assert snap["pid"] == proc.pid
	assert "executor" in snap and "reactor" in snap and "vts" in snap

	# Every VT carries `vtid` — the same id std.log emits as `vtid`, so liveness
	# output correlates with app logs (not the old `vt_id` spelling).
	for v in snap["vts"]:
		assert "vtid" in v and "vt_id" not in v, f"expected vtid key, got {list(v)}"

	states = [v["state"] for v in snap["vts"]]
	assert "PARKED_TIMER" in states, f"no timer-parked VT in {states}"
	assert "PARKED_JOIN" in states, f"no join-parked VT in {states}"
	assert "PARKED_CONDVAR" in states, f"no condvar-parked VT in {states}"

	# A join waiter must carry the joined target's id, and parked VTs have no
	# carrier thread.
	joiners = [v for v in snap["vts"] if v["state"] == "PARKED_JOIN"]
	assert joiners and joiners[0]["wait"]["kind"] == "join"
	assert joiners[0]["wait"]["id"] != 0
	for v in snap["vts"]:
		if v["state"].startswith("PARKED_"):
			assert v["carrier_tid"] is None
		# Invariant: a RUNNING VT must never report a stale wait reason.
		# (Fast-path / cancelled / invalid-deadline resume must clear it.)
		if v["state"] == "RUNNING":
			assert v["wait"]["kind"] == "none", (
				f"running VT {v['vtid']} shows stale wait {v['wait']}"
			)

	# Bounded stderr summary present with the required prefix.
	err_text = err.decode(errors="replace")
	assert "[drift:liveness]" in err_text
	assert "drift.liveness.v1" in err_text


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="Linux-only")
def test_text_disabled_still_writes_json(tmp_path: Path) -> None:
	binary = _compile(tmp_path, _PARKED_STATES_SOURCE)
	dump = tmp_path / "live.json"
	env = dict(os.environ)
	env["DRIFT_LIVENESS_JSON_PATH"] = str(dump)
	env["DRIFT_LIVENESS_TEXT"] = "0"

	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
	                        stderr=subprocess.PIPE, env=env)
	try:
		time.sleep(0.6)
		os.kill(proc.pid, signal.SIGUSR2)
		snap = _wait_for_json(dump)
	finally:
		proc.kill()
		_out, err = proc.communicate(timeout=10)

	assert snap["schema"] == "drift.liveness.v1"
	assert "[drift:liveness]" not in err.decode(errors="replace")


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="Linux-only")
def test_json_write_failure_with_text_disabled_emits_forced_error(tmp_path: Path) -> None:
	"""DRIFT_LIVENESS_TEXT=0 + an unwritable JSON path: no file is produced, the
	process stays alive, and the single forced fallback error line is the only
	operator-visible signal on stderr (the suppressed summary must not hide a
	failed dump)."""
	binary = _compile(tmp_path, _ALIVE_SOURCE)

	# Deterministically-unwritable target: use a regular file as a parent
	# "directory" so open() fails with ENOTDIR regardless of uid / container
	# permissions (no chmod/root tricks).
	blocker = tmp_path / "blocker"
	blocker.write_text("not a directory")
	dump = blocker / "dump.json"  # <regular-file>/dump.json -> ENOTDIR

	env = dict(os.environ)
	env["DRIFT_LIVENESS_TEXT"] = "0"
	env["DRIFT_LIVENESS_JSON_PATH"] = str(dump)

	proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
	                        stderr=subprocess.PIPE, env=env)
	try:
		time.sleep(0.6)               # let main park
		assert proc.poll() is None, "process should be alive before signaling"
		os.kill(proc.pid, signal.SIGUSR2)
		time.sleep(0.5)               # let the liveness thread run the emit path
		# SIGUSR2 must not terminate the process, and the failed dump must not
		# crash it: it stays alive until we kill it below.
		assert proc.poll() is None, "process must remain alive after a failed dump"
	finally:
		proc.kill()
		_out, err = proc.communicate(timeout=10)

	err_text = err.decode(errors="replace")

	# No JSON file produced; the blocker is untouched (still a regular file).
	assert not dump.exists(), "no JSON dump must be produced on write failure"
	assert blocker.is_file()

	# The forced one-line error is present and is the ONLY operator-visible
	# liveness output (the normal summary is suppressed by DRIFT_LIVENESS_TEXT=0).
	assert "[drift:liveness] error: JSON dump write failed" in err_text, (
		f"missing forced error line; stderr was:\n{err_text[:800]}"
	)
	assert "=== drift.liveness.v1" not in err_text, (
		"summary must stay suppressed when DRIFT_LIVENESS_TEXT=0"
	)


def test_normal_program_no_regression(tmp_path: Path) -> None:
	binary = _compile(tmp_path, _NORMAL_SOURCE)
	res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
	assert res.returncode == 0, f"normal program should exit 0, got {res.returncode}"
