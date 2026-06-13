# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: std.fs.read_dir() — VT-safe, deterministic directory listing.

Pins:
1. Deterministic sorted order; FileKind per entry (symlink reported as Symlink,
   not its target); "." and ".." excluded.
2. opendir errors surface as IoError errno: ENOENT (missing), ENOTDIR (a file).
3. An invalid-UTF-8 filename fails the whole call (invalid-utf8 / EILSEQ), no
   partial listing.
4. read_dir works from a spawned VT (the blocking-pool offload + park path).
5. VT-safety: a compute VT keeps progressing while another VT's read_dir is
   stalled in the blocking pool (the walk is offloaded, not inline on the carrier).
6. Leak-clean under valgrind across success and error paths (the refcounted C
   snapshot freed exactly once; result handle always freed).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout, asan_active, valgrind_cmd

# A direct `valgrind` invocation needs a non-ASan binary; under the ASan lane the
# binary's shadow memory collides with valgrind's and it aborts at startup.  Skip
# these variants there — ASan covers the same leak/UAF surface when the non-valgrind
# sibling test runs the program directly in that lane.  (UBSan-only does NOT conflict
# with valgrind, so it still runs; a combined ASan+UBSan lane skips via the ASan term.)
_VALGRIND_SKIP = pytest.mark.skipif(
	shutil.which("valgrind") is None or asan_active(),
	reason="valgrind requires a non-ASan binary (ASan shadow memory collides)",
)

ROOT = Path(__file__).resolve().parents[3]


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
	assert res.returncode == 0, f"compile failed: {res.stderr[:600]}"
	assert out.exists()
	return out


# Lists PATH, printing "<name>:<kindchar>" per entry in read_dir order, or
# "ERR:<kind>:<code>" on error.  PATH is templated in by the test.
_LIST_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

fn _k(k: &fs.FileKind) nothrow -> String {
\tmatch k {
\t\tfs.FileKind::File() => { return "F"; },
\t\tfs.FileKind::Dir() => { return "D"; },
\t\tfs.FileKind::Symlink() => { return "L"; },
\t\tfs.FileKind::Other() => { return "O"; },
\t\tfs.FileKind::Unknown() => { return "U"; }
\t}
}

pub fn main() nothrow -> Int {
\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 10000)) {
\t\tOk(entries) => {
\t\t\tvar i = 0;
\t\t\twhile i < entries.len {
\t\t\t\tcons.println(entries[i].name + ":" + _k(&entries[i].kind));
\t\t\t\ti = i + 1;
\t\t\t}
\t\t\treturn 0;
\t\t},
\t\tErr(e) => {
\t\t\tcons.println("ERR:" + e.kind + ":" + fmt.format_int(e.code));
\t\t\treturn 1;
\t\t}
\t}
}
"""

# Runs read_dir on a spawned VT (offload path) and prints the count.
_VT_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

fn _count() nothrow -> Int {
\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 10000)) {
\t\tOk(entries) => { return entries.len; },
\t\tErr(e) => { return 0 - e.code; }
\t}
}

pub fn main() nothrow -> Int {
\tvar vt = conc.spawn<type Int>(core.callback0(| | => { return _count(); }));
\tmatch vt.join() {
\t\tOk(n) => { cons.println("count:" + fmt.format_int(n)); return 0; },
\t\tErr(e) => { cons.println("joinerr"); return 2; }
\t}
}
"""


def _list_source(path: Path) -> str:
	return _LIST_SOURCE.replace("__PATH__", str(path))


def _run(binary: Path, env: dict | None = None) -> subprocess.CompletedProcess:
	full_env = dict(os.environ)
	if env:
		full_env.update(env)
	return subprocess.run([str(binary)], capture_output=True, text=True,
	                      timeout=30, env=full_env)


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
	"""A directory with: two files, a subdir, and a symlink to a file."""
	d = tmp_path / "scan"
	d.mkdir()
	(d / "alpha.txt").write_text("a")
	(d / "bravo.txt").write_text("b")
	(d / "zsub").mkdir()
	os.symlink(d / "alpha.txt", d / "mlink")
	return d


def test_read_dir_basic(tmp_path: Path, fixture_dir: Path) -> None:
	"""Deterministic sorted order, correct kinds, symlink not resolved, ./.. gone."""
	binary = _compile(tmp_path, _list_source(fixture_dir))
	res = _run(binary)
	assert res.returncode == 0, f"stderr: {res.stderr[:300]}"
	# Sorted by unsigned-byte name; symlink 'mlink' is L (not F, its target).
	assert res.stdout == "alpha.txt:F\nbravo.txt:F\nmlink:L\nzsub:D\n", res.stdout


def test_read_dir_enoent(tmp_path: Path) -> None:
	"""Missing path → Err(errno=ENOENT=2), no listing."""
	binary = _compile(tmp_path, _list_source(tmp_path / "nope_xyz"))
	res = _run(binary)
	assert res.returncode == 1
	assert res.stdout == "ERR:errno:2\n", res.stdout


def test_read_dir_enotdir(tmp_path: Path) -> None:
	"""Path is a regular file → Err(errno=ENOTDIR=20)."""
	f = tmp_path / "afile"
	f.write_text("x")
	binary = _compile(tmp_path, _list_source(f))
	res = _run(binary)
	assert res.returncode == 1
	assert res.stdout == "ERR:errno:20\n", res.stdout


def test_read_dir_invalid_utf8(tmp_path: Path) -> None:
	"""An entry whose name is not valid UTF-8 fails the whole call
	(invalid-utf8 / EILSEQ=84), with no partial listing."""
	d = tmp_path / "badnames"
	d.mkdir()
	(d / "good.txt").write_text("g")
	# Create an entry with a raw 0xFF byte name (not valid UTF-8).
	bad = bytes(d) + b"/\xff\xfe"
	fd = os.open(bad, os.O_CREAT | os.O_WRONLY, 0o644)
	os.close(fd)
	binary = _compile(tmp_path, _list_source(d))
	res = _run(binary)
	assert res.returncode == 1, f"stdout: {res.stdout!r}"
	assert res.stdout == "ERR:invalid-utf8:84\n", res.stdout


def test_read_dir_from_vt(tmp_path: Path, fixture_dir: Path) -> None:
	"""read_dir from a spawned VT exercises the blocking-pool offload + park."""
	source = _VT_SOURCE.replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source)
	res = _run(binary)
	assert res.returncode == 0, f"stderr: {res.stderr[:300]}"
	assert res.stdout == "count:4\n", res.stdout


def test_read_dir_carrier_not_blocked(tmp_path: Path, fixture_dir: Path) -> None:
	"""Carrier-safety proof: on a SINGLE carrier, a compute VT must finish
	(join_timeout 500ms) while a reader's read_dir is still stalled 2s in the
	pool. If read_dir blocked the carrier, the compute VT could not run until the
	stall ended and the join would time out."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.sync as sync;
import std.console as cons;
import std.format as fmt;

fn _so() nothrow -> sync.MemoryOrder { return sync.MemoryOrder::SeqCst(); }

pub fn main() nothrow -> Int {
\t// Pin a single-carrier executor (builder defaults to min=max=1; set it
\t// explicitly so a blocked carrier is observable and not masked by parallelism).
\tvar pb = conc.executor_policy_builder();
\tconc.set_default_executor(pb.build_executor());

\t// Handshake: the reader sets `ready` immediately before entering read_dir, so
\t// the compute VT is only introduced AFTER the reader has reached (and, on the
\t// lone carrier, parked in) read_dir — closing the ordering hole.
\tval ready: core.Arc<sync.AtomicInt> = core.arc<type sync.AtomicInt>(sync.atomic_int(0));
\tval ready_vt = ready.clone();
\tvar reader = conc.spawn<type Int>(core.callback0(| | captures(move ready_vt) => {
\t\tready_vt.get().store(1, _so());
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 20000)) {
\t\t\tOk(entries) => { return entries.len; },
\t\t\tErr(e) => { return 0 - e.code; }
\t\t}
\t}));
\t// Wait until the reader has reached read_dir (the sleep yields the lone carrier
\t// to the reader, which sets `ready` and then parks in the blocking-pool offload).
\twhile ready.get().load(_so()) == 0 {
\t\tmatch conc.sleep(conc.Duration(millis = 5)) { Ok(_) => { }, Err(e) => { } }
\t}
\t// Compute returns immediately — but only gets to run if the carrier is free.
\tvar compute = conc.spawn<type Int>(core.callback0(| | => { return 7; }));

\tmatch compute.join_timeout(conc.Duration(millis = 500)) {
\t\tOk(v) => {
\t\t\tval _r = reader.join();
\t\t\tif v == 7 { cons.println("live"); return 0; }
\t\t\tcons.println("badval"); return 1;
\t\t},
\t\tErr(e) => {
\t\t\tval _r = reader.join();
\t\t\tcons.println("carrier-blocked"); return 2;
\t\t}
\t}
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source)
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "2000"})
	assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "live\n", res.stdout


# Reader VT: read_dir with a __DEADLINE__ms deadline; returns a code identifying
# the outcome kind so the test can assert the distinct failure mode.
_OUTCOME_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.io as io;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

fn _outcome() nothrow -> Int {
\tmatch fs.read_dir("__PATH__", conc.Duration(millis = __DEADLINE__)) {
\t\tOk(entries) => { return 1000 + entries.len; },
\t\tErr(e) => {
\t\t\tif e.kind == io.IO_ERROR_KIND_TIMEOUT { return 1; }
\t\t\tif e.kind == io.IO_ERROR_KIND_CANCELLED { return 2; }
\t\t\tif e.kind == io.IO_ERROR_KIND_SATURATED { return 3; }
\t\t\treturn 0 - e.code;
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\tvar vt = conc.spawn<type Int>(core.callback0(| | => { return _outcome(); }));
__BODY__
}
"""


def _outcome_binary(tmp_path: Path, fixture_dir: Path, deadline_ms: int, body: str,
                    name: str) -> Path:
	source = (_OUTCOME_SOURCE
	          .replace("__PATH__", str(fixture_dir))
	          .replace("__DEADLINE__", str(deadline_ms))
	          .replace("__BODY__", body))
	return _compile(tmp_path, source, name)


def test_read_dir_timeout(tmp_path: Path, fixture_dir: Path) -> None:
	"""A short deadline against a stalled directory yields a distinct `timeout`
	error (not a plain errno), promptly and with no partial listing."""
	# Join the reader; it abandons at the 200ms deadline despite the 2s stall.
	body = (
		"\tmatch vt.join() {\n"
		"\t\tOk(n) => { cons.println(fmt.format_int(n)); return 0; },\n"
		"\t\tErr(e) => { cons.println(\"joinerr\"); return 2; }\n"
		"\t}\n"
	)
	binary = _outcome_binary(tmp_path, fixture_dir, 200, body, "timeout_bin")
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "2000"})
	assert res.returncode == 0, f"stderr: {res.stderr[:300]}"
	assert res.stdout == "1\n", res.stdout  # 1 == timeout kind


# Cancellation source: the reader writes the read_dir outcome kind into a shared
# atomic BEFORE returning, because cancelling the VT makes vt.join() itself return
# the cancellation (discarding the VT's value).  Main cancels the in-flight reader
# and reads the atomic to observe read_dir's own `cancelled` error kind.
_CANCEL_SOURCE = """\
module main;
import std.core as core;
import std.fs as fs;
import std.io as io;
import std.concurrent as conc;
import std.sync as sync;
import std.console as cons;
import std.format as fmt;

fn _so() nothrow -> sync.MemoryOrder { return sync.MemoryOrder::SeqCst(); }

pub fn main() nothrow -> Int {
\tval slot: core.Arc<sync.AtomicInt> = core.arc<type sync.AtomicInt>(sync.atomic_int(0));
\tval slot_vt = slot.clone();
\tvar vt = conc.spawn<type Int>(core.callback0(| | captures(move slot_vt) => {
\t\tvar code = 0;
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 400)) {
\t\t\tOk(entries) => { code = 1000 + entries.len; },
\t\t\tErr(e) => {
\t\t\t\tif e.kind == io.IO_ERROR_KIND_CANCELLED { code = 2; }
\t\t\t\telse { if e.kind == io.IO_ERROR_KIND_TIMEOUT { code = 1; } else { code = 0 - e.code; } }
\t\t\t}
\t\t}
\t\tslot_vt.get().store(code, _so());
\t\treturn code;
\t}));
\tmatch conc.sleep(conc.Duration(millis = 100)) { Ok(_) => { }, Err(e) => { } }
\tvt.cancel();
\t// Join waits for the reader to finish; it returns the cancellation, which we
\t// ignore — the read_dir outcome kind is in the shared slot.
\tval _j = vt.join();
\tval observed = slot.get().load(_so());
\tcons.println(fmt.format_int(observed));
\tif observed == 2 { return 0; }
\treturn 1;
}
"""


def _cancel_binary(tmp_path: Path, fixture_dir: Path, name: str) -> Path:
	return _compile(tmp_path, _CANCEL_SOURCE.replace("__PATH__", str(fixture_dir)), name)


def test_read_dir_cancel_abandon(tmp_path: Path, fixture_dir: Path) -> None:
	"""Cancelling a VT whose read_dir is in flight: read_dir returns a distinct
	`cancelled` error (observed via a shared slot), the call abandons promptly."""
	binary = _cancel_binary(tmp_path, fixture_dir, "cancel_bin")
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "2000"})
	assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "2\n", res.stdout  # 2 == cancelled kind


@_VALGRIND_SKIP
def test_read_dir_cancel_abandon_memcheck(tmp_path: Path, fixture_dir: Path) -> None:
	"""The cancel/abandon path is leak-clean: the cancelled VT abandons its job
	and the stalled worker frees the abandoned snapshot exactly once."""
	binary = _cancel_binary(tmp_path, fixture_dir, "cancel_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
		             "--errors-for-leak-kinds=definite", "-q", str(binary)),
		capture_output=True, text=True, timeout=120,
		env={**os.environ, "DRIFT_FS_TEST_STALL_MS": "500"},
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:800]}"
	# The program itself must exit cleanly (the cancelled kind printed) — a startup
	# crash or abort must not pass as "no valgrind error".
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "2\n", res.stdout  # 2 == cancelled kind


def test_cancel_resumes_blocking_parked_vt_promptly(tmp_path: Path, fixture_dir: Path) -> None:
	"""Runtime regression (central VT cancellation): a VT parked on a blocking-pool
	job with a 30s deadline must resume PROMPTLY on cancel (~ms), not wait for the
	deadline. The program self-measures cancel->resume latency and requires < 500ms;
	a broken cancel (no unpark of a started fiber-parked VT) would yield ~30000ms."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.io as io;
import std.concurrent as conc;
import std.sync as sync;
import lang.thread as thread;
import std.console as cons;
import std.format as fmt;

fn _so() nothrow -> sync.MemoryOrder { return sync.MemoryOrder::SeqCst(); }

pub fn main() nothrow -> Int {
\tval slot: core.Arc<sync.AtomicInt> = core.arc<type sync.AtomicInt>(sync.atomic_int(0));
\tval slot_vt = slot.clone();
\t// 30s deadline AND 30s stall: neither the deadline nor worker completion can
\t// mask a broken cancel — only a prompt cancel-resume returns quickly.
\tvar vt = conc.spawn<type Int>(core.callback0(| | captures(move slot_vt) => {
\t\tvar code = 0;
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 30000)) {
\t\t\tOk(e) => { code = 1000; },
\t\t\tErr(er) => { if er.kind == io.IO_ERROR_KIND_CANCELLED { code = 2; } else { code = 9; } }
\t\t}
\t\tslot_vt.get().store(code, _so());
\t\treturn code;
\t}));
\tmatch conc.sleep(conc.Duration(millis = 200)) { Ok(_) => { }, Err(e) => { } }
\tval t0 = thread.now_ms();
\tvt.cancel();
\tval _j = vt.join();
\tval latency = thread.now_ms() - t0;
\tcons.println("latency_ms=" + fmt.format_int(latency) + " code=" + fmt.format_int(slot.get().load(_so())));
\tif latency < 500 { return 0; }
\treturn 1;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "cresume_bin")
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "30000"})
	assert res.returncode == 0, f"cancel did not resume promptly: {res.stdout!r} {res.stderr[:200]}"
	assert "code=2" in res.stdout, res.stdout  # read_dir saw the cancellation


def test_timeout_permits_prompt_process_exit(tmp_path: Path, fixture_dir: Path) -> None:
	"""Runtime regression (bounded shutdown): a timed-out read_dir whose worker is
	stuck in a 30s stall must NOT block process exit for 30s. The process must exit
	within a few seconds (bounded by the shutdown join budget), abandoning the stuck
	worker rather than joining it indefinitely at atexit."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;

pub fn main() nothrow -> Int {
\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 200)) {
\t\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t\t}
\t}));
\tval _j = vt.join();
\tcons.println("done");
\treturn 0;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "promptexit_bin")
	import time as _time
	start = _time.monotonic()
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "30000"})
	elapsed = _time.monotonic() - start
	assert res.returncode == 0, f"stderr: {res.stderr[:300]}"
	assert res.stdout == "done\n", res.stdout
	# A 30s stall must not delay exit to ~30s; the shutdown budget caps it (~2s).
	assert elapsed < 8.0, f"process exit blocked on the abandoned worker: {elapsed:.1f}s"


@_VALGRIND_SKIP
def test_read_dir_timeout_abandon_memcheck(tmp_path: Path, fixture_dir: Path) -> None:
	"""The timeout/abandon path is leak-clean: the VT abandons at the deadline,
	and the stalled worker frees the abandoned snapshot at shutdown."""
	body = (
		"\tmatch vt.join() {\n"
		"\t\tOk(n) => { return 0; },\n"
		"\t\tErr(e) => { return 2; }\n"
		"\t}\n"
	)
	binary = _outcome_binary(tmp_path, fixture_dir, 200, body, "timeout_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
		             "--errors-for-leak-kinds=definite", "-q", str(binary)),
		capture_output=True, text=True, timeout=120,
		env={**os.environ, "DRIFT_FS_TEST_STALL_MS": "500"},
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:800]}"
	# The program joins the timed-out VT and exits cleanly; a crash must not pass.
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"


def test_read_dir_saturation(tmp_path: Path, fixture_dir: Path) -> None:
	"""Work admission is bounded to EXACTLY 4 workers + 64 queue = 68 (Finding 3:
	pinned deterministically, not scheduling-dependent).

	A worker-entry BARRIER removes the dependence on worker dequeue timing: first
	occupy all 4 pool workers and wait (via the test-only walk-entry counter) until
	all 4 are confirmed in the walk; only then submit 64 queue-fillers + 12 overflow.
	With all 4 workers busy on a long stall, the queue holds exactly 64 and the 12
	overflow are rejected with the distinct `saturated` backpressure error."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.io as io;
import std.concurrent as conc;
import lang.thread as thread;
import std.console as cons;
import std.format as fmt;

// Long deadline: occupier stays parked on the worker for the whole test.
fn _occupy() nothrow -> Int {
\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 60000)) {
\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t}
}
// Short deadline: a queued filler abandons promptly (it never runs — the 4
// workers are busy on the long stall — so saturation is decided purely at submit).
fn _try() nothrow -> Int {
\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 500)) {
\t\tOk(e) => { return 0; },
\t\tErr(e) => { if e.kind == io.IO_ERROR_KIND_SATURATED { return 1; } return 0; }
\t}
}

pub fn main() nothrow -> Int {
\t// 1. Occupy all 4 pool workers.  Keep the handles in scope so they stay parked.
\tvar o0 = conc.spawn<type Int>(core.callback0(| | => { return _occupy(); }));
\tvar o1 = conc.spawn<type Int>(core.callback0(| | => { return _occupy(); }));
\tvar o2 = conc.spawn<type Int>(core.callback0(| | => { return _occupy(); }));
\tvar o3 = conc.spawn<type Int>(core.callback0(| | => { return _occupy(); }));
\t// 2. Barrier: wait until all 4 occupier walks have entered a worker.
\twhile thread.fs_test_walk_entries() < 4 {
\t\tmatch conc.sleep(conc.Duration(millis = 5)) { Ok(_) => { }, Err(e) => { } }
\t}
\t// 3. Submit 64 queue-fillers + 12 overflow.  All 4 workers are busy, so the
\t//    queue fills to exactly 64 and the last 12 are rejected.
\tvar vts: Array<conc.VirtualThread<Int>> = [];
\tvar i = 0;
\twhile i < 76 {
\t\tvts.push(conc.spawn<type Int>(core.callback0(| | => { return _try(); })));
\t\ti = i + 1;
\t}
\tvar saturated = 0;
\tvar j = 0;
\twhile j < 76 {
\t\tmatch vts[j].join() {
\t\t\tOk(v) => { saturated = saturated + v; },
\t\t\tErr(e) => { }
\t\t}
\t\tj = j + 1;
\t}
\tcons.println("saturated:" + fmt.format_int(saturated));
\t// (o0..o3 drop here; process exits without joining the still-stalled workers.)
\tif saturated == 12 { return 0; }
\treturn 1;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "saturate_bin")
	# Stall (5s) >> admission, so the 4 workers never free a queue slot during the
	# fill: exactly 76 - 64 == 12 of the fillers are rejected.
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "5000"})
	assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "saturated:12\n", res.stdout


@_VALGRIND_SKIP
def test_read_dir_memcheck(tmp_path: Path, fixture_dir: Path) -> None:
	"""Leak-clean across success (from a VT) and error paths under valgrind."""
	# Success path from a VT (offload + result table + refcount free).
	vt_bin = _compile(tmp_path, _VT_SOURCE.replace("__PATH__", str(fixture_dir)), "vt_bin")
	# Error path (ENOENT) on the inline path.
	err_bin = _compile(tmp_path, _list_source(tmp_path / "nope_xyz"), "err_bin")
	# The success binary exits 0; the intentional-ENOENT binary exits 1.  Asserting
	# the expected app exit code (not just != 99) means a startup crash/abort cannot
	# pass silently as "no valgrind error".
	for binary, expected_rc in ((vt_bin, 0), (err_bin, 1)):
		res = subprocess.run(
			valgrind_cmd("--error-exitcode=99", "--leak-check=full",
			             "--errors-for-leak-kinds=definite", "-q", str(binary)),
			capture_output=True, text=True, timeout=120,
		)
		assert res.returncode != 99, f"valgrind found leaks/errors in {binary.name}:\n{res.stderr[:800]}"
		assert res.returncode == expected_rc, (
			f"{binary.name}: unexpected exit rc={res.returncode} (want {expected_rc}) "
			f"stdout={res.stdout!r} stderr={res.stderr[:300]}")


# ---------------------------------------------------------------------------
# Central scheduler regressions (round 7): the park/unpark wake protocol must
# have no lost wake and no stale token even when cancel, the deadline timer, and
# worker completion all race to resume a parked VT.
# ---------------------------------------------------------------------------

def test_park_unpark_no_stale_token_no_lost_wake(tmp_path: Path, fixture_dir: Path) -> None:
	"""Durable regression for the wake protocol: each iteration parks a VT on
	read_dir with deadline ~= worker stall (so the deadline timer and the worker
	completion RACE to unpark it), then does a SECOND timed park (conc.sleep).
	The second park must take ~its full duration — proving it neither returns
	immediately (a stale token left by the racing resumers) nor hangs (a lost
	wake).  200 iterations to hit the race window."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import lang.thread as thread;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar bad = 0;
\tvar i = 0;
\twhile i < 120 {
\t\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\t\t// First park: deadline (40ms) ~= worker stall (40ms) -> the deadline
\t\t\t// timer and the worker completion race to unpark this VT.
\t\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 40)) {
\t\t\t\tOk(e) => { }, Err(er) => { }
\t\t\t}
\t\t\t// Second timed park: must sleep ~the full 120ms (no stale token) and
\t\t\t// must not hang (no lost wake).
\t\t\tval t0 = thread.now_ms();
\t\t\tmatch conc.sleep(conc.Duration(millis = 120)) { Ok(_) => { }, Err(e) => { } }
\t\t\treturn thread.now_ms() - t0;
\t\t}));
\t\tmatch vt.join() {
\t\t\tOk(slept) => { if slept < 95 { bad = bad + 1; } },
\t\t\tErr(e) => { bad = bad + 1; }
\t\t}
\t\ti = i + 1;
\t}
\tcons.println("bad=" + fmt.format_int(bad));
\tif bad == 0 { return 0; }
\treturn 1;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "durable_bin")
	# stall ~= the 40ms deadline maximizes the timer/worker unpark race.
	# Own (long) timeout: ~120 iters * ~160ms is well under it but over _run's 30s.
	full_env = dict(os.environ)
	full_env["DRIFT_FS_TEST_STALL_MS"] = "40"
	res = subprocess.run([str(binary)], capture_output=True, text=True, timeout=90, env=full_env)
	assert res.returncode == 0, (
		f"stale token or lost wake in the park/unpark protocol: {res.stdout!r} {res.stderr[:300]}"
	)
	assert res.stdout == "bad=0\n", res.stdout


@_VALGRIND_SKIP
def test_cancel_timer_worker_storm_no_uaf(tmp_path: Path, fixture_dir: Path) -> None:
	"""Cancel + deadline timer + worker completion all race to resume the same
	parked VT, repeatedly.  A double-enqueue (the VT run twice) or UAF would crash
	or trip valgrind.  Runs the storm under valgrind."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar n = 0;
\twhile n < 250 {
\t\t// Short deadline (40ms) ~= no stall here, so the worker completes almost
\t\t// immediately while the cancel and the deadline timer also race the park.
\t\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 40)) {
\t\t\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t\t\t}
\t\t}));
\t\tvt.cancel();
\t\tval _j = vt.join();
\t\tn = n + 1;
\t}
\tcons.println("ok:" + fmt.format_int(n));
\treturn 0;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "storm_bin")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "-q", str(binary)),
		capture_output=True, text=True, timeout=300,
		env={**os.environ, "DRIFT_FS_TEST_STALL_MS": "10"},
	)
	assert res.returncode != 99, f"valgrind found a UAF/error in the resume storm:\n{res.stderr[:800]}"
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "ok:250\n", res.stdout


def test_process_exit_with_active_blocking_job(tmp_path: Path, fixture_dir: Path) -> None:
	"""Finding 2: process exit while a VT is parked on an ACTIVE (not timed-out)
	blocking job.  Main returns while a spawned reader is still mid-read_dir (30s
	stall); the worker finishing during atexit teardown must NOT unpark the (being
	destroyed) VT, and shutdown must be prompt (bounded join), not 30s."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;

pub fn main() nothrow -> Int {
\t// Spawn a reader with a LONG deadline so it does not time out; it is still
\t// actively parked on the blocking job when main returns.
\tvar _vt = conc.spawn<type Int>(core.callback0(| | => {
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 60000)) {
\t\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t\t}
\t}));
\t// Give the reader a moment to submit + park, then exit WITHOUT joining it.
\tmatch conc.sleep(conc.Duration(millis = 100)) { Ok(_) => { }, Err(e) => { } }
\tcons.println("exiting");
\treturn 0;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "activejob_bin")
	import time as _time
	start = _time.monotonic()
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "30000"})
	elapsed = _time.monotonic() - start
	assert res.returncode == 0, f"exit with active blocking job failed: rc={res.returncode} {res.stderr[:300]}"
	assert res.stdout == "exiting\n", res.stdout
	# The stuck (30s) worker must not block exit; bounded shutdown caps it (~2s).
	assert elapsed < 8.0, f"process exit blocked on the active worker: {elapsed:.1f}s"


# ---------------------------------------------------------------------------
# Round-8 deterministic scheduler regressions, using runtime test hooks that
# widen the critical windows so the races are pinned, not left to timing luck.
# ---------------------------------------------------------------------------

def test_multicarrier_no_reentrant_execution(tmp_path: Path, fixture_dir: Path) -> None:
	"""Finding 1: on a MULTI-carrier executor, a parked VT can be unpark-enqueued
	while its previous carrier is still between publishing PARKED and the
	swapcontext that saves its context.  The DRIFT_TEST_PARK_PAUSE_US hook widens
	that window to milliseconds (>> the 1ms sleep deadline that unparks it), so the
	re-entrancy guard is exercised on essentially every park.  Without the guard a
	second carrier would swap into an unsaved context and corrupt the fiber stack
	(crash / wrong result); with it, every VT runs exactly once."""
	source = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar pb = conc.executor_policy_builder();
\tval _a = pb.max_threads(4);
\tval _b = pb.min_threads(4);
\tconc.set_default_executor(pb.build_executor());
\tvar vts: Array<conc.VirtualThread<Int>> = [];
\tvar i = 0;
\twhile i < 48 {
\t\tvts.push(conc.spawn<type Int>(core.callback0(| | => {
\t\t\tvar k = 0;
\t\t\twhile k < 10 {
\t\t\t\tmatch conc.sleep(conc.Duration(millis = 1)) { Ok(_) => { }, Err(e) => { } }
\t\t\t\tk = k + 1;
\t\t\t}
\t\t\treturn k;
\t\t})));
\t\ti = i + 1;
\t}
\tvar done = 0;
\tvar j = 0;
\twhile j < 48 { match vts[j].join() { Ok(v) => { done = done + v; }, Err(e) => { } } j = j + 1; }
\tcons.println("done:" + fmt.format_int(done));
\tif done == 480 { return 0; }
\treturn 1;
}
"""
	binary = _compile(tmp_path, source, "reentr_bin")
	# Window (3ms) >> the 1ms sleep deadline => the timer unpark + another carrier
	# race the premature-suspension window on nearly every park.
	res = _run(binary, env={"DRIFT_TEST_PARK_PAUSE_US": "3000"})
	assert res.returncode == 0, f"re-entrant execution / lost work: {res.stdout!r} {res.stderr[:300]}"
	assert res.stdout == "done:480\n", res.stdout


@_VALGRIND_SKIP
def test_multicarrier_no_reentrant_execution_memcheck(tmp_path: Path, fixture_dir: Path) -> None:
	"""The same multi-carrier re-entrancy window under valgrind — a swap into an
	unsaved/aliased context would trip the memory checker."""
	source = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar pb = conc.executor_policy_builder();
\tval _a = pb.max_threads(4);
\tval _b = pb.min_threads(4);
\tconc.set_default_executor(pb.build_executor());
\tvar vts: Array<conc.VirtualThread<Int>> = [];
\tvar i = 0;
\twhile i < 24 {
\t\tvts.push(conc.spawn<type Int>(core.callback0(| | => {
\t\t\tvar k = 0;
\t\t\twhile k < 6 {
\t\t\t\tmatch conc.sleep(conc.Duration(millis = 1)) { Ok(_) => { }, Err(e) => { } }
\t\t\t\tk = k + 1;
\t\t\t}
\t\t\treturn k;
\t\t})));
\t\ti = i + 1;
\t}
\tvar done = 0;
\tvar j = 0;
\twhile j < 24 { match vts[j].join() { Ok(v) => { done = done + v; }, Err(e) => { } } j = j + 1; }
\tif done == 144 { return 0; }
\treturn 1;
}
"""
	binary = _compile(tmp_path, source, "reentr_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "-q", str(binary)),
		capture_output=True, text=True, timeout=300,
		env={**os.environ, "DRIFT_TEST_PARK_PAUSE_US": "2000"},
	)
	assert res.returncode != 99, f"valgrind found a re-entrancy/UAF:\n{res.stderr[:800]}"
	# All 24 VTs must complete (done==144 -> rc 0); a crash/abort must not pass.
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"


def test_shutdown_drains_inflight_unpark(tmp_path: Path, fixture_dir: Path) -> None:
	"""Finding 2: shutdown must wait for an already-authorized worker unpark to
	finish before teardown.  The DRIFT_TEST_WORKER_UNPARK_PAUSE_MS hook pauses a
	worker AFTER it takes the in-flight stake (passes the stopping check) but
	BEFORE the unpark; main then exits (initiating shutdown) during that window.
	Shutdown must drain the in-flight unpark — so the process exit is delayed by
	~the pause — instead of tearing down VTs under the in-flight notification."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import std.console as cons;

pub fn main() nothrow -> Int {
\tvar _vt = conc.spawn<type Int>(core.callback0(| | => {
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 60000)) {
\t\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t\t}
\t}));
\tmatch conc.sleep(conc.Duration(millis = 100)) { Ok(_) => { }, Err(e) => { } }
\tcons.println("exiting");
\treturn 0;
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "drain_bin")
	import time as _time
	start = _time.monotonic()
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "10",
	                        "DRIFT_TEST_WORKER_UNPARK_PAUSE_MS": "400"})
	elapsed = _time.monotonic() - start
	assert res.returncode == 0, f"shutdown raced the in-flight unpark: rc={res.returncode} {res.stderr[:300]}"
	assert res.stdout == "exiting\n", res.stdout
	# Shutdown blocked on the 400ms-paused worker before teardown: main returned at
	# ~100ms but the process should not exit until the unpark drained (~400ms+).
	assert elapsed >= 0.35, f"shutdown did not wait for the in-flight unpark to drain: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Round-9 regressions: blocking-pool teardown ordering + reactor single-claim.
# ---------------------------------------------------------------------------

def test_worker_unpark_resumes_parked_vt(tmp_path: Path, fixture_dir: Path) -> None:
	"""Finding 1: confirm the pool WORKER actually drives drift_thread_unpark to
	resume a parked VT (not a degenerate path where a local handle's destruction
	claims vt_resumed first).  Main JOINS the reader (so it is never dropped /
	cancelled) and the deadline is long (so no timeout), making the worker the SOLE
	resumer; the DRIFT_TEST_WORKER_UNPARK_PAUSE_MS hook makes the worker pause
	before its unpark, so the join can only return after the worker's unpark."""
	source = """\
module main;
import std.core as core;
import std.fs as fs;
import std.concurrent as conc;
import lang.thread as thread;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar vt = conc.spawn<type Int>(core.callback0(| | => {
\t\tmatch fs.read_dir("__PATH__", conc.Duration(millis = 60000)) {
\t\t\tOk(e) => { return e.len; }, Err(er) => { return 0 - er.code; }
\t\t}
\t}));
\tval t0 = thread.now_ms();
\tmatch vt.join() {
\t\tOk(n) => {
\t\t\tcons.println("count:" + fmt.format_int(n) + " waited:" + fmt.format_int(thread.now_ms() - t0));
\t\t\tif n == 4 { return 0; }
\t\t\treturn 1;
\t\t},
\t\tErr(e) => { cons.println("joinerr"); return 2; }
\t}
}
""".replace("__PATH__", str(fixture_dir))
	binary = _compile(tmp_path, source, "wunpark_bin")
	res = _run(binary, env={"DRIFT_FS_TEST_STALL_MS": "10",
	                        "DRIFT_TEST_WORKER_UNPARK_PAUSE_MS": "300"})
	assert res.returncode == 0, f"worker did not resume the parked VT: {res.stdout!r} {res.stderr[:300]}"
	# count=4 (the fixture dir) and the join only returned after the worker's 300ms
	# unpark pause -> the worker's drift_thread_unpark was the resumer.
	import re as _re
	m = _re.match(r"count:4 waited:(\d+)\n", res.stdout)
	assert m, res.stdout
	assert int(m.group(1)) >= 250, f"resumed too early (not via the paused worker): {res.stdout!r}"


@_VALGRIND_SKIP
def test_timer_cancel_single_claim_race(tmp_path: Path, fixture_dir: Path) -> None:
	"""Finding 2: every resume path CASes the PARKED transition so exactly one
	claims a parked VT.  Here the reactor deadline-timer wake and the cancellation
	unpark race for each parked VT (both go through drift_thread_unpark's CAS), on a
	multi-carrier executor with the park window widened.  A duplicate claim (resume
	while also enqueuing, or a double enqueue) would double-run a fiber and corrupt
	its stack; the CAS makes exactly one claim win.  (The reactor fd-EVENT claim
	sites — std.net/std.io read/write/connect with deadlines — are exercised by the
	std_net timeout e2e sweep, which the CAS change keeps green.)"""
	source = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.console as cons;
import std.format as fmt;

pub fn main() nothrow -> Int {
\tvar pb = conc.executor_policy_builder();
\tval _a = pb.max_threads(4);
\tval _b = pb.min_threads(4);
\tconc.set_default_executor(pb.build_executor());
\tvar vts: Array<conc.VirtualThread<Int>> = [];
\tvar i = 0;
\twhile i < 32 {
\t\tvts.push(conc.spawn<type Int>(core.callback0(| | => {
\t\t\tvar k = 0;
\t\t\twhile k < 8 {
\t\t\t\tmatch conc.sleep(conc.Duration(millis = 1)) { Ok(_) => { }, Err(e) => { } }
\t\t\t\tk = k + 1;
\t\t\t}
\t\t\treturn k;
\t\t})));
\t\ti = i + 1;
\t}
\t// Cancel half of them mid-flight: cancel unpark races the sleep-timer wake.
\tvar c = 0;
\twhile c < 32 {
\t\tif c < 16 { vts[c].cancel(); }
\t\tc = c + 1;
\t}
\tvar j = 0;
\twhile j < 32 { match vts[j].join() { Ok(v) => { }, Err(e) => { } } j = j + 1; }
\tcons.println("ok");
\treturn 0;
}
"""
	binary = _compile(tmp_path, source, "reactor_race")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "-q", str(binary)),
		capture_output=True, text=True, timeout=300,
		env={**os.environ, "DRIFT_TEST_PARK_PAUSE_US": "1500"},
	)
	assert res.returncode != 99, f"valgrind found a duplicate-claim/UAF:\n{res.stderr[:800]}"
	# Every VT must complete and main exit cleanly; a crash/abort must not pass.
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "ok\n", res.stdout


# ---------------------------------------------------------------------------
# Error-semantics + ordering pins (deterministic via fault injection).
# ---------------------------------------------------------------------------

def test_read_dir_fstatat_failure_degrades_to_unknown(tmp_path: Path, fixture_dir: Path) -> None:
	"""A per-entry fstatat failure degrades ONLY that entry to FileKind::Unknown;
	the snapshot still succeeds and every other entry keeps its real kind."""
	binary = _compile(tmp_path, _list_source(fixture_dir))
	res = _run(binary, env={"DRIFT_FS_TEST_FSTATAT_FAIL_NAME": "alpha.txt"})
	assert res.returncode == 0, f"snapshot failed: {res.stdout!r} {res.stderr[:200]}"
	# alpha.txt -> U (fstatat injected-failed); the rest keep their real kinds.
	assert res.stdout == "alpha.txt:U\nbravo.txt:F\nmlink:L\nzsub:D\n", res.stdout


def test_read_dir_read_error_wins_over_close_error(tmp_path: Path, fixture_dir: Path) -> None:
	"""When a readdir/validate error AND a close error both occur, the read error
	wins (and no partial snapshot is returned)."""
	binary = _compile(tmp_path, _list_source(fixture_dir))
	res = _run(binary, env={"DRIFT_FS_TEST_READDIR_FAIL_ERRNO": "5",   # EIO
	                        "DRIFT_FS_TEST_CLOSE_FAIL_ERRNO": "9"})    # EBADF
	assert res.returncode == 1
	# The readdir errno (5) wins over the close errno (9); no listing.
	assert res.stdout == "ERR:errno:5\n", res.stdout


def test_read_dir_close_only_failure_rejects_snapshot(tmp_path: Path, fixture_dir: Path) -> None:
	"""A close-only failure (the read phase succeeded) returns the close error and
	the snapshot is NOT handed back."""
	binary = _compile(tmp_path, _list_source(fixture_dir))
	res = _run(binary, env={"DRIFT_FS_TEST_CLOSE_FAIL_ERRNO": "9"})  # EBADF
	assert res.returncode == 1
	assert res.stdout == "ERR:errno:9\n", res.stdout


def test_read_dir_utf8_byte_order(tmp_path: Path) -> None:
	"""Sorting is deterministic UNSIGNED-BYTE lexicographic (not character/locale).
	Multibyte UTF-8 names (lead byte 0xC3) sort AFTER ascii 'z' (0x7A), which a
	naive alphabetical/locale collation would not do."""
	d = tmp_path / "u8"
	d.mkdir()
	names = [
		b"apple",                         # 'a' = 0x61
		b"zebra",                         # 'z' = 0x7A
		b"\xc3\x85ngstr\xc3\xb6m",        # "Ångström", lead 0xC3 0x85
		b"\xc3\xa9mile",                  # "émile",    lead 0xC3 0xA9
	]
	for n in names:
		fd = os.open(bytes(d) + b"/" + n, os.O_CREAT | os.O_WRONLY, 0o644)
		os.close(fd)
	binary = _compile(tmp_path, _list_source(d))  # _list prints "<name>:<kind>"
	res = _run(binary)
	assert res.returncode == 0, f"stderr: {res.stderr[:200]}"
	got = [line.rsplit(":", 1)[0] for line in res.stdout.splitlines()]
	# Byte order: apple (0x61) < zebra (0x7A) < Ångström (0xC3 0x85) < émile (0xC3 0xA9).
	expected = ["apple", "zebra", "\xc5ngstr\xf6m", "\xe9mile"]
	assert got == expected, f"got {got!r}"


def _compile_test_build(tmp_path: Path, source: str, name: str) -> Path:
	"""Compile with --test-build-only (needed for io.file_from_fd / eventfd hooks)."""
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev", "--test-build-only",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:600]}"
	return out


# Reactor direct-resume CLAIM path raced against a cancellation, made deterministic
# by two runtime test hooks (round 11):
#   - vt_test_direct_resume_claims(): advances ONLY when the reactor wins the
#     PARKED->READY direct-resume claim for an fd event (not a competing cancel/timer),
#   - DRIFT_TEST_DIRECT_RESUME_PAUSE_MS: holds the VT in READY before READY->RUNNING.
# The reader runs on a DEDICATED single-worker executor (its worker is the sole
# reactor poller, so the fd event is direct-resumed there).  main NEVER parks until
# all race work is done: a single-worker carrier that parks would enter the reactor
# poll branch and compete for poll ownership; busy-waiting keeps the reader's worker
# the sole poller, reliably in epoll_wait when the fd is written.  main makes the fd
# ready, busy-waits until the claim counter advances (PROVING the reactor won the
# claim — otherwise it times out and fails, so a too-early read that never parked
# cannot pass silently), THEN cancels into the READY window.  The cancel must observe
# READY ("already claimed"), deposit no token, and not re-enqueue; completion must be
# clean (no stale token, no duplicate run, no hang).
_FDRACE_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;
import std.io as io;
import std.sync as sync;
import lang.thread as thread;
import std.console as cons;
import std.format as fmt;

fn _so() nothrow -> sync.MemoryOrder { return sync.MemoryOrder::SeqCst(); }

// Bounded busy-wait (NO park): keeps main's single-worker carrier RUNNING so it
// never enters the reactor poll branch — the reader's dedicated worker stays the
// sole poll owner, reliably in epoll_wait when we write the fd.
fn _spin_ms(ms: Int) nothrow -> Void {
\tval end = thread.now_ms() + ms;
\twhile thread.now_ms() < end { }
}

pub fn main() nothrow -> Int {
\t// Dedicated single-worker executor for the reader (default policy is min=max=1):
\t// threads_count==1 -> its worker inline-polls the reactor and performs the
\t// direct-resume under test.  main stays on the (separate) root executor.
\tvar pb = conc.executor_policy_builder();
\tval exec = pb.build_executor();

\tvar n = 0;
\twhile n < __ITERS__ {
\t\tval fd = thread.test_eventfd_create();
\t\tif fd <= 0 { return 9; }
\t\tval ready: core.Arc<sync.AtomicInt> = core.arc<type sync.AtomicInt>(sync.atomic_int(0));
\t\tval ready_r = ready.clone();
\t\tmatch conc.spawn_on<type Int>(exec, core.callback0(| | captures(copy fd, move ready_r) => {
\t\t\tval f = io.file_from_fd(fd);
\t\t\tval c = io.configure_file(&f, conc.Duration(millis = 30000));
\t\t\tvar buf = io.buffer(8);
\t\t\tready_r.get().store(1, _so());
\t\t\tmatch c.read(&mut buf) { Ok(_) => { return 1; }, Err(e) => { return 0; }, default => { return 0; } }
\t\t})) {
\t\t\tOk(rd) => {
\t\t\t\tvar v = move rd;   // mutable handle (cancel/join take &mut self)
\t\t\t\t// Busy-wait (never park) until the reader signals it is entering read.
\t\t\t\twhile ready.get().load(_so()) == 0 { }
\t\t\t\t// Let the reader's worker EAGAIN, register the fd, and park in epoll_wait
\t\t\t\t// BEFORE we write — else the first nonblocking read would succeed with no
\t\t\t\t// direct-resume (liveness only; the counter wait below is the correctness
\t\t\t\t// guard and fails the test if no direct-resume actually happened).  The
\t\t\t\t// settle is generous under valgrind, where setup is far slower.
\t\t\t\t_spin_ms(__SETTLE__);
\t\t\t\tval base = thread.vt_test_direct_resume_claims();
\t\t\t\tthread.test_eventfd_write(fd, 1);
\t\t\t\t// Busy-spin until the reactor WINS the PARKED->READY direct-resume claim.
\t\t\t\tval deadline = thread.now_ms() + __DEADLINE__;
\t\t\t\tvar timed_out = 0;
\t\t\t\twhile thread.vt_test_direct_resume_claims() <= base {
\t\t\t\t\tif thread.now_ms() >= deadline { timed_out = 1; break; }
\t\t\t\t}
\t\t\t\tif timed_out == 1 { return 7; }
\t\t\t\t// The reactor now holds the VT in READY, paused before READY->RUNNING.
\t\t\t\t// Cancel into that window; it must be a no-op (READY = already claimed).
\t\t\t\tv.cancel();
\t\t\t\tmatch v.join() { Ok(_) => { }, Err(e) => { } }
\t\t\t},
\t\t\tErr(e) => { return 8; }
\t\t}
\t\tn = n + 1;
\t}
\tcons.println("done:" + fmt.format_int(n));
\treturn 0;
}
"""


def test_reactor_fd_event_vs_cancel_direct_resume(tmp_path: Path, fixture_dir: Path) -> None:
	"""Deterministic reactor FD-event-vs-cancellation race on the direct-resume CLAIM
	path: the reactor (reader's dedicated single-worker exec) WINS the PARKED->READY
	claim — proven by the vt_test_direct_resume_claims counter — and is held in the
	READY window (DRIFT_TEST_DIRECT_RESUME_PAUSE_MS) while a cancellation races in.
	The cancel must observe READY ('already claimed'), deposit no token the VT would
	consume instead of suspending, and not re-enqueue; the program completes cleanly."""
	source = (_FDRACE_SOURCE.replace("__ITERS__", "12")
	          .replace("__SETTLE__", "60").replace("__DEADLINE__", "5000"))
	binary = _compile_test_build(tmp_path, source, "fdrace_bin")
	# Suppress the dedicated reactor thread so the reader's single-worker executor
	# is the sole poller and the fd event takes the worker-inline DIRECT-resume path
	# (otherwise the reactor thread services it via the queued path and the
	# direct-resume claim window — the round-10 fix — is never exercised).
	res = _run(binary, env={"DRIFT_TEST_DIRECT_RESUME_PAUSE_MS": "200",
	                        "DRIFT_TEST_NO_REACTOR_THREAD": "1"})
	assert res.returncode == 0, (
		f"reactor direct-resume vs cancel did not complete cleanly (7=claim never won): "
		f"rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"
	)
	assert res.stdout == "done:12\n", res.stdout


@_VALGRIND_SKIP
def test_reactor_fd_event_vs_cancel_direct_resume_memcheck(tmp_path: Path, fixture_dir: Path) -> None:
	"""The reactor-claim-wins-vs-cancel window is UAF-clean: a duplicate enqueue
	(cancel re-running an already-claimed VT) or a stale token would surface as a
	use-after-free / double-run under valgrind."""
	source = (_FDRACE_SOURCE.replace("__ITERS__", "4")
	          .replace("__SETTLE__", "800").replace("__DEADLINE__", "20000"))
	binary = _compile_test_build(tmp_path, source, "fdrace_mc")
	# valgrind_cmd() supplies --fair-sched=yes: main's busy-spin (kept hot so its
	# carrier never polls) would otherwise monopolize valgrind's serial scheduler and
	# starve the reader's worker thread, which must run to poll the fd and direct-resume.
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--leak-check=full",
		             "--errors-for-leak-kinds=definite", "-q", str(binary)),
		capture_output=True, text=True, timeout=300,
		env={**os.environ, "DRIFT_TEST_DIRECT_RESUME_PAUSE_MS": "120",
		     "DRIFT_TEST_NO_REACTOR_THREAD": "1"},
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:800]}"
	assert res.returncode == 0, f"unexpected exit: rc={res.returncode} stdout={res.stdout!r} stderr={res.stderr[:300]}"
	assert res.stdout == "done:4\n", res.stdout
