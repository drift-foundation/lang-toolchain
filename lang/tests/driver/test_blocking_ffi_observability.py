# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Blocking-FFI observability pins (ABI 21 slice).

The facility contract: a wedged blocking worker must be diagnosable
from `kill -USR2` alone — which subsystem (executor name), which
operation (label), which extern call (symbol + Drift file/line), who
submitted it, and who is waiting for admission behind it.

Pinned here against a REAL stuck C call (a long `usleep` occupying a
1-worker/1-slot named executor, with a labeled second submission parked
in admission):

1. JSON: `execs[]` entry with name/queue/running/waiters/capacity; the
   RUNNING VT carries op label, submitter vtid, exec id, and
   `ffi: {symbol, file, line}` pointing at the Drift callsite; the
   waiter shows `wait.kind == "blocking-admission"` with the target
   exec id and its admission deadline.
2. stderr top-running summary names the op, exec id, and ffi site.
3. The FFI marker CLEARS: after the extern call returns, no VT reports
   an `ffi` field.
4. Instrumentation scope: user `extern "C"` calls are bracketed
   (enter/exit counts match in IR); a program with no user externs
   emits no `drift_ffi_enter` calls — `@intrinsic` runtime externs and
   stdlib-declared (`std.*`/`lang.*`) externs pay nothing; only
   user-module extern declarations are instrumented.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

_STUCK_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

extern "C" fn usleep(usec: Uint32) nothrow -> Int32;

fn stuck_c_call() nothrow -> Int {
	unsafe {
		val _ = usleep(cast<Uint32>(30000000));
	}
	return 0;
}

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	b.queue_limit(1);
	b.timeout(conc.Duration(millis = 60000));
	val ex = conc.build_blocking_executor(b.build(), "storage-demo");
	match conc.spawn_blocking_on(&ex, "demo.stuck_op", core.callback0(|| => { return stuck_c_call(); })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	val exw = ex;
	val _w = conc.spawn_on_labeled(conc.default_executor(), "demo.submitter", core.callback0(|| captures(copy exw) => {
		match conc.spawn_blocking_on(&exw, "demo.waiting_op", core.callback0(|| => { return 0; })) {
			core.Result::Ok(_) => {},
			core.Result::Err(_) => {},
		}
		return 0;
	}));
	console.println("stuck-ready");
	val _ = conc.sleep(conc.Duration(millis = 25000));
	return 0;
}
"""

_CLEARED_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

extern "C" fn usleep(usec: Uint32) nothrow -> Int32;

fn quick_c_call() nothrow -> Int {
	unsafe {
		val _ = usleep(cast<Uint32>(1000));
	}
	return 0;
}

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	val ex = conc.build_blocking_executor(b.build(), "quick-demo");
	match conc.run_blocking_on(&ex, "demo.quick_op", core.callback0(|| => { return quick_c_call(); })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	console.println("quick-done");
	val _ = conc.sleep(conc.Duration(millis = 25000));
	return 0;
}
"""

_NO_EXTERN_SOURCE = """\
module main;

import std.console as console;

pub fn main() nothrow -> Int {
	var s = "a" + "b";
	s = s + "c";
	console.println(s);
	return 0;
}
"""


_HOSTILE_LABEL_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

extern "C" fn usleep(usec: Uint32) nothrow -> Int32;

fn hold_ffi() nothrow -> Int {
	unsafe {
		val _ = usleep(cast<Uint32>(20000000));
	}
	return 0;
}

pub fn main() nothrow -> Int {
	// An UNLABELED plain cooperative VT: must report no op/submitter/ffi.
	val _plain = conc.spawn_cb(|| => {
		val _ = conc.sleep(conc.Duration(millis = 20000));
		return 0;
	});
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	// Hostile bytes in BOTH the executor name and the op label:
	// quote, backslash, newline, tab.
	val ex = conc.build_blocking_executor(b.build(), "sto\\"rage\\\\demo\\n");
	match conc.spawn_blocking_on(&ex, "op\\"quote\\\\back\\nnl\\tt", core.callback0(|| => { return hold_ffi(); })) {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	console.println("hostile-ready");
	val _ = conc.sleep(conc.Duration(millis = 25000));
	return 0;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--allow-unsafe",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	return out


def _run_until_marker_then_usr2(out: Path, tmp_path: Path, marker: str,
                                settle_s: float = 1.5) -> tuple[dict, str]:
	"""Start the binary, wait for the stdout marker, settle, USR2, wait
	for the JSON dump, kill, return (json, stderr_text)."""
	dump = tmp_path / "liveness.json"
	env = os.environ.copy()
	env["DRIFT_LIVENESS_JSON_PATH"] = str(dump)
	out_f = tmp_path / "run.out"
	err_f = tmp_path / "run.err"
	with open(out_f, "w") as fo, open(err_f, "w") as fe:
		proc = subprocess.Popen([str(out)], stdout=fo, stderr=fe, env=env)
		try:
			deadline = time.monotonic() + sanitizer_timeout(30)
			while time.monotonic() < deadline:
				if marker in out_f.read_text():
					break
				time.sleep(0.1)
			else:
				raise AssertionError(f"marker {marker!r} never appeared")
			time.sleep(settle_s)
			proc.send_signal(signal.SIGUSR2)
			deadline = time.monotonic() + sanitizer_timeout(20)
			while time.monotonic() < deadline:
				if dump.exists() and dump.stat().st_size > 0:
					try:
						data = json.loads(dump.read_text())
						break
					except json.JSONDecodeError:
						pass
				time.sleep(0.1)
			else:
				raise AssertionError("liveness JSON never appeared")
		finally:
			proc.kill()
			proc.wait(timeout=10)
	return data, err_f.read_text()


def test_stuck_ffi_liveness_names_everything(tmp_path: Path) -> None:
	out = _compile(tmp_path, _STUCK_SOURCE)
	data, err = _run_until_marker_then_usr2(out, tmp_path, "stuck-ready")

	# (1) execs[]: the named executor with its capacity picture.
	execs = data.get("execs") or []
	named = [e for e in execs if e.get("name") == "storage-demo"]
	assert named, f"storage-demo exec entry missing: {execs}"
	e = named[0]
	assert e["queue_limit"] == 1 and e["running"] == 1 and e["waiters"] >= 1, e
	exec_id = e["id"]

	# (2) the RUNNING blocking task: op, submitter, exec, ffi site.
	running = [v for v in data["vts"]
	           if v.get("op") == "demo.stuck_op" and v.get("state") == "RUNNING"]
	assert running, f"stuck op VT missing: {data['vts']}"
	v = running[0]
	assert v.get("exec_id") == exec_id, v
	assert isinstance(v.get("submitter"), int) and v["submitter"] > 0, v
	assert v.get("carrier_tid"), v
	ffi = v.get("ffi")
	assert ffi and ffi["symbol"] == "usleep", v
	assert ffi["file"].endswith("main.drift") and isinstance(ffi["line"], int) and ffi["line"] > 0, ffi

	# (3) the admission waiter: wait kind, target exec, deadline.
	waiters = [v for v in data["vts"]
	           if (v.get("wait") or {}).get("kind") == "blocking-admission"]
	assert waiters, f"blocking-admission waiter missing: {data['vts']}"
	w = waiters[0]
	assert w["wait"].get("exec_id") == exec_id, w
	assert isinstance(w["wait"].get("deadline_ms"), int), w

	# (4) the labeled-but-unadmitted submission is visible too.
	labeled = [v for v in data["vts"] if v.get("op") == "demo.waiting_op"]
	assert labeled, "waiting_op submission not labeled in snapshot"

	# (5) stderr summary is actionable on its own — BOTH lines.
	assert "op=demo.stuck_op" in err, err
	assert re.search(r"ffi=usleep@\S*main\.drift:\d+", err), err
	# (6) the top-parked line carries the admission context and the
	# waiter fiber's own label (review test-gap finding): state name,
	# op=, and wait=blocking-admission exec_id= joined to the executor.
	m = re.search(
		r"vtid=\d+ PARKED_BLOCKING_ADMISSION wait_id=(\d+) parked_for=\d+ms"
		r" op=demo\.submitter wait=blocking-admission exec_id=(\d+)",
		err,
	)
	assert m, f"parked summary line missing admission context:\n{err}"
	assert int(m.group(1)) == exec_id and int(m.group(2)) == exec_id, (m.groups(), exec_id)


def test_ffi_marker_cleared_after_call(tmp_path: Path) -> None:
	out = _compile(tmp_path, _CLEARED_SOURCE)
	data, _err = _run_until_marker_then_usr2(out, tmp_path, "quick-done")
	still_marked = [v for v in data["vts"] if v.get("ffi")]
	assert not still_marked, f"stale ffi markers after call returned: {still_marked}"
	# (No assert on the op label here: the completed submission VT is
	# destroyed on join and leaves the registry — the label's presence
	# during execution is pinned by the stuck test.)


def test_instrumentation_scope(tmp_path: Path) -> None:
	"""User extern calls are bracketed; intrinsic-only programs emit no
	enter calls at all."""
	d1 = tmp_path / "with_extern"
	d1.mkdir()
	out1 = _compile(d1, _CLEARED_SOURCE)
	ir1 = (d1 / "test_bin.ll").read_text()
	enters = len(re.findall(r"call void @drift_ffi_enter\(ptr @__drift_ffi_site_\d+\)", ir1))
	exits = len(re.findall(r"call void @drift_ffi_exit\(\)", ir1))
	assert enters >= 1 and enters == exits, (enters, exits)
	assert "@__drift_ffi_site_0 = private unnamed_addr constant" in ir1

	d2 = tmp_path / "no_extern"
	d2.mkdir()
	out2 = _compile(d2, _NO_EXTERN_SOURCE)
	ir2 = (d2 / "test_bin.ll").read_text()
	assert not re.search(r"call void @drift_ffi_enter", ir2), "intrinsic-only program got FFI instrumentation"
	run = subprocess.run([str(out2)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0


def test_hostile_labels_json_valid_and_unlabeled_vts_clean(tmp_path: Path) -> None:
	"""(a) Labels/names containing quotes/backslashes/newlines must not
	corrupt the JSON document (json.loads is the pin) and must
	round-trip exactly; (b) an unlabeled cooperative VT reports NO
	op/submitter/ffi (spawn-time zero-init of the malloc'd fields)."""
	out = _compile(tmp_path, _HOSTILE_LABEL_SOURCE)
	data, _err = _run_until_marker_then_usr2(out, tmp_path, "hostile-ready")

	execs = [e for e in data.get("execs") or [] if e.get("name")]
	hostile = [e for e in execs if 'sto"rage\\demo\n' == e["name"]]
	assert hostile, f"escaped exec name did not round-trip: {[e.get('name') for e in execs]}"

	labeled = [v for v in data["vts"] if v.get("op") == 'op"quote\\back\nnl\tt']
	assert labeled, f"escaped op label did not round-trip: {[v.get('op') for v in data['vts'] if v.get('op')]}"

	# Unlabeled VTs must be clean — no garbage from uninitialized fields.
	for v in data["vts"]:
		if v.get("op") is None:
			assert "submitter" not in v, v
			assert "ffi" not in v, v


_HEAP_LABEL_BALANCE_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
	var b = conc.blocking_executor_builder();
	b.min_threads(1);
	b.max_threads(1);
	val ex = conc.build_blocking_executor(b.build(), "heap-" + "name");
	var i = 0;
	while i < 5 {
		match conc.run_blocking_on(&ex, "heap." + "label", core.callback0(|| => { return 1; })) {
			core.Result::Ok(_) => {},
			core.Result::Err(_) => { return 1; },
		}
		i = i + 1;
	}
	console.println("balance-done");
	return 0;
}
"""


@pytest.mark.skipif(__import__("shutil").which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_heap_labels_balanced_valgrind(tmp_path: Path) -> None:
	"""Ownership pin for the name/label externs, decisive in BOTH
	directions because the strings are HEAP (concat — static literals
	no-op retain/release and mask everything): the stdlib call sites
	pass `move`, so the runtime receivers own the stake and must
	release exactly once (DRIFT_OWNED_STRING shadow).  A missing
	release leaks per call; a wrong extra release (Convention-B
	misclassification) double-frees.  Valgrind must be silent."""
	out = _compile(tmp_path, _HEAP_LABEL_BALANCE_SOURCE)
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		["valgrind", "--leak-check=full",
		 "--errors-for-leak-kinds=definite,indirect",
		 "--error-exitcode=97", f"--log-file={vg_log}", str(out)],
		capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)
	log = vg_log.read_text() if vg_log.exists() else ""
	assert vg.returncode == 0, f"valgrind errors:\n{log[-1500:]}"
	assert "balance-done" in vg.stdout
