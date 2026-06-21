# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: `conc.yield_now()` and single-fd `io.poll()` (Phase 1).

`yield_now`:
- A VT that loops `yield_now()` lets a co-located peer VT make progress on the
  same single cooperative worker, and does so FAST — proving it is the scheduler
  relinquish (`thread.vt_yield`), not the `sleep(1ms)` floor (which would make N
  iterations take ~N ms).

`io.poll(fd, interest, timeout)` (single fd, single direction, over the reactor):
- readiness: a peer writes → the poller wakes `Ok(Read)` promptly.
- timeout: nothing arrives → `Err(kind=timeout)` at ~the deadline.
- pending-edge replay: the edge arrives BEFORE the poll registers (no waiter →
  reactor sets the pending flag) → poll returns `Ok` immediately via
  `reactor_check_pending`.

All over existing intrinsics — no new runtime symbols, ABI 17.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import sanitizer_timeout, asan_active, valgrind_cmd

_VALGRIND_SKIP = pytest.mark.skipif(
	shutil.which("valgrind") is None or asan_active(),
	reason="valgrind requires a non-ASan binary (ASan shadow memory collides)",
)

ROOT = Path(__file__).resolve().parents[3]


def _compile(tmp_path: Path, source: str, name: str = "bin") -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(150),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:900]}"
	assert out.exists()
	return out


# ── yield_now ────────────────────────────────────────────────────────────────
# Worker increments a shared atomic N times, yielding each step. Main yields in a
# loop until the worker finishes, then asserts (in-program) it completed in well
# under the sleep(1ms) floor (N ms). Returns 0 on success.
_YIELD_SOURCE = """\
module main;
import std.core as core;
import std.concurrent as conc;
import lang.atomic as atomic;
import lang.thread as thread;

const N: Int = 2000;

pub fn main() nothrow -> Int {
\tvar shared = conc.arc(conc.atomic_int(0));
\tval start = thread.now_ms();
\tvar vt = conc.spawn(| | captures(share shared) => {
\t\tvar i = 0;
\t\twhile i < N {
\t\t\tval _ = shared.get().fetch_add(1, atomic.MemoryOrder::SeqCst());
\t\t\tconc.yield_now();
\t\t\ti = i + 1;
\t\t}
\t\treturn 0;
\t});
\tvar spins = 0;
\twhile shared.get().load(atomic.MemoryOrder::SeqCst()) < N {
\t\tconc.yield_now();
\t\tspins = spins + 1;
\t\tif spins > 100000000 { return 1; }
\t}
\tval _j = vt.join();
\tval elapsed = thread.now_ms() - start;
\t// Real relinquish completes N iterations in milliseconds; the sleep(1ms)
\t// floor would need >= N ms (~2s). Fail if it looks like the sleep path.
\tif elapsed > 500 { return 3; }
\treturn shared.get().load(atomic.MemoryOrder::SeqCst()) == N ? 0 : 2;
}
"""


# ── poll: shared scaffolding ─────────────────────────────────────────────────
# `{body}` is the server side run after `accept` yields `ss` (a TcpStream); it
# must set `r` to 0 on success. The connector behaviour is templated by `{conn}`.
_POLL_TEMPLATE = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;
import lang.thread as thr;

fn is_timeout(e: &io.IoError) nothrow -> Int {
\treturn (e.kind + "") == "timeout" ? 0 : 4;
}

fn connector(port: Int) nothrow -> Int {
\tval t = conc.Duration(millis = 5000);
\tmatch net.connect(&net.socket_addr("127.0.0.1", port), t) {
\t\tcore.Result::Err(e) => { return 1; },
\t\tcore.Result::Ok(cs) => {
{conn}
\t\t\tval _c = cs.close(t);
\t\t\treturn 0;
\t\t}
\t}
}

pub fn main() nothrow -> Int {
\tval t = conc.Duration(millis = 5000);
\tmatch net.listen(&net.socket_addr("127.0.0.1", 0), t) {
\t\tcore.Result::Err(e) => { return 100; },
\t\tcore.Result::Ok(lis) => {
\t\t\tval port = lis.local_port();
\t\t\tvar cvt = conc.spawn(| | captures(move port) => { return connector(port); });
\t\t\tvar r = 99;
\t\t\tmatch net.accept(&lis, t) {
\t\t\t\tcore.Result::Err(e) => { r = 50; },
\t\t\t\tcore.Result::Ok(ss) => {
{body}
\t\t\t\t\tval _cl = ss.close(t);
\t\t\t\t}
\t\t\t}
\t\t\tval _j = cvt.join();
\t\t\treturn r;
\t\t}
\t}
}
"""

_CONN_WRITE_AFTER_DELAY = """\
\t\t\tval _slp = conc.sleep(conc.Duration(millis = 100));
\t\t\tvar buf = io.buffer(8);
\t\t\tio.buffer_write_string(&mut buf, &"x");
\t\t\tio.buffer_set_len(&mut buf, 1);
\t\t\tval _w = cs.write(&buf, t);
\t\t\tval _s = conc.sleep(conc.Duration(millis = 500));"""

_CONN_WRITE_IMMEDIATE = """\
\t\t\tvar buf = io.buffer(8);
\t\t\tio.buffer_write_string(&mut buf, &"x");
\t\t\tio.buffer_set_len(&mut buf, 1);
\t\t\tval _w = cs.write(&buf, t);
\t\t\tval _s = conc.sleep(conc.Duration(millis = 600));"""

_CONN_NEVER_WRITE = """\
\t\t\tval _s = conc.sleep(conc.Duration(millis = 800));"""

# readiness: peer writes ~100ms in; poll(Read, 3s) wakes Ok promptly (<2s).
_BODY_READINESS = """\
\t\t\t\t\tval start = thr.now_ms();
\t\t\t\t\tmatch io.poll(ss.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 3000)) {
\t\t\t\t\t\tcore.Result::Ok(_w) => { r = thr.now_ms() - start < 2000 ? 0 : 7; },
\t\t\t\t\t\tcore.Result::Err(e) => { r = 5; }
\t\t\t\t\t}"""

# timeout: peer never writes; poll(Read, 200ms) → Err(kind=timeout).
_BODY_TIMEOUT = """\
\t\t\t\t\tmatch io.poll(ss.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 200)) {
\t\t\t\t\t\tcore.Result::Ok(_w) => { r = 2; },
\t\t\t\t\t\tcore.Result::Err(e) => { r = is_timeout(&e); }
\t\t\t\t\t}"""

# no-deadline (Duration(0)) park: peer writes ~100ms in; poll(Read, Duration(0))
# must PARK until that write (not return Ok immediately). Asserts it both waited
# (>= 50ms — caught the bug where vt_park_until(0) returns instantly) and woke Ok.
_BODY_NO_DEADLINE = """\
\t\t\t\t\tval start = thr.now_ms();
\t\t\t\t\tmatch io.poll(ss.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 0)) {
\t\t\t\t\t\tcore.Result::Ok(_w) => { val el = thr.now_ms() - start; r = (el >= 50 and el < 3000) ? 0 : 7; },
\t\t\t\t\t\tcore.Result::Err(e) => { r = 5; }
\t\t\t\t\t}"""

# pending-edge replay: peer writes immediately; main waits so the edge arrives
# with no waiter (reactor sets pending), THEN polls → Ok via check_pending.
_BODY_REPLAY = """\
\t\t\t\t\tval _w8 = conc.sleep(conc.Duration(millis = 250));
\t\t\t\t\tval start = thr.now_ms();
\t\t\t\t\tmatch io.poll(ss.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 3000)) {
\t\t\t\t\t\tcore.Result::Ok(_w) => { r = thr.now_ms() - start < 1000 ? 0 : 7; },
\t\t\t\t\t\tcore.Result::Err(e) => { r = 5; }
\t\t\t\t\t}"""


def _poll_source(conn: str, body: str) -> str:
	# Two-phase format: fill conn/body (which contain no braces) first, then the
	# template's literal `{{ }}` collapse on a second pass is avoided by using
	# .replace for our two slots.
	return _POLL_TEMPLATE.replace("{conn}", conn).replace("{body}", body)


def _run(binary: Path, timeout_s: int = 30) -> tuple[int, float]:
	t0 = time.monotonic()
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(timeout_s))
	return res.returncode, time.monotonic() - t0


def test_yield_now_hands_off_and_is_not_sleep(tmp_path: Path) -> None:
	rc, wall = _run(_compile(tmp_path, _YIELD_SOURCE, "yld"))
	assert rc == 0, f"yield_now handoff failed (rc={rc}; 3=looked like sleep path)"
	# 2000 iterations: the sleep(1ms) path would be ~2s wall; the relinquish path
	# is well under a second even with process startup.
	assert wall < 1.5, f"yield_now too slow ({wall:.2f}s) — likely a sleep path"


def test_poll_readiness(tmp_path: Path) -> None:
	src = _poll_source(_CONN_WRITE_AFTER_DELAY, _BODY_READINESS)
	rc, _ = _run(_compile(tmp_path, src, "rd"))
	assert rc == 0, f"poll readiness failed (rc={rc}; 5=Err,7=too slow,50=accept)"


def test_poll_timeout_distinct(tmp_path: Path) -> None:
	src = _poll_source(_CONN_NEVER_WRITE, _BODY_TIMEOUT)
	rc, _ = _run(_compile(tmp_path, src, "to"))
	assert rc == 0, f"poll timeout failed (rc={rc}; 2=spurious-ready,4=wrong-kind)"


def test_poll_pending_edge_replay(tmp_path: Path) -> None:
	src = _poll_source(_CONN_WRITE_IMMEDIATE, _BODY_REPLAY)
	rc, _ = _run(_compile(tmp_path, src, "rp"))
	assert rc == 0, f"poll pending-edge replay failed (rc={rc})"


# Listener accept-readiness — the documented server use case for TcpListener.raw_fd():
# poll the LISTENER fd for Read; a connecting peer makes it readable; then accept()
# yields the connection. Returns 0 iff poll woke Ok AND the subsequent accept worked.
_LISTENER_POLL_SOURCE = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;
import lang.thread as thr;

fn connector(port: Int) nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	val _slp = conc.sleep(conc.Duration(millis = 150));   // let the server enter poll first
	match net.connect(&net.socket_addr("127.0.0.1", port), t) {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(cs) => {
			val _s = conc.sleep(conc.Duration(millis = 500));
			val _c = cs.close(t);
			return 0;
		}
	}
}

pub fn main() nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.listen(&net.socket_addr("127.0.0.1", 0), t) {
		core.Result::Err(e) => { return 100; },
		core.Result::Ok(lis) => {
			val port = lis.local_port();
			var cvt = conc.spawn(| | captures(move port) => { return connector(port); });
			var r = 99;
			// Wait for accept-readiness on the LISTENER fd, then accept.
			val start = thr.now_ms();
			match io.poll(lis.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 3000)) {
				core.Result::Ok(_w) => {
					val woke_fast = thr.now_ms() - start < 2500;
					match net.accept(&lis, t) {
						core.Result::Ok(ss) => {
							r = woke_fast ? 0 : 7;
							val _cl = ss.close(t);
						},
						core.Result::Err(e) => { r = 6; }
					}
				},
				core.Result::Err(e) => { r = 5; }
			}
			val _j = cvt.join();
			return r;
		}
	}
}
"""


def test_poll_listener_accept_readiness(tmp_path: Path) -> None:
	# Pins TcpListener.raw_fd() + poll() for the documented accept-readiness path.
	rc, _ = _run(_compile(tmp_path, _LISTENER_POLL_SOURCE, "lis"))
	assert rc == 0, (
		f"listener poll/accept failed (rc={rc}; 5=poll Err,7=woke too slow,"
		f"6=accept Err,100=listen)"
	)


def test_poll_no_deadline_parks_until_ready(tmp_path: Path) -> None:
	# Duration(0) means "no deadline — park until ready", NOT "return now".
	src = _poll_source(_CONN_WRITE_AFTER_DELAY, _BODY_NO_DEADLINE)
	rc, _ = _run(_compile(tmp_path, src, "nd"))
	assert rc == 0, (
		f"poll(Duration(0)) did not park until ready (rc={rc}; 7=returned too "
		f"early — vt_park_until(0) returns immediately; 5=Err)"
	)


# Stale-waiter cleanup (finding 2): poll fd1 (times out, leaving — without the
# fix — a stale reactor read_vt back-pointer to this VT), then poll fd2 while an
# edge lands on fd1. fd2's poll must NOT wake early on fd1's stale registration.
# Returns 0 iff BOTH polls time out (kind=timeout). Two connectors: c1 writes
# ~300ms in (after fd1's 200ms poll has timed out); c2 stays silent.
#
# NOTE: on the current single cooperative worker the exact interleaving that would
# turn the stale back-pointer into a spurious wake is hard to force — this test
# passes with OR without the post-park clear here. It is kept as a functional guard
# (sequential polls on distinct fds behave) and as the harness for the cleanup. The
# clear itself is a correct, no-ABI C-level defense: `drift_vt_claim_for_resume`
# does a bare PARKED->READY CAS with no wait_id guard, so a stale read_vt CAN claim
# a VT parked elsewhere — and that race opens up under multi-worker scheduling (F4).
_STALE_WAITER_SOURCE = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;
import lang.thread as thr;

fn is_timeout(e: &io.IoError) nothrow -> Int {
	return (e.kind + "") == "timeout" ? 0 : 4;
}

fn writer(port: Int) nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.connect(&net.socket_addr("127.0.0.1", port), t) {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(cs) => {
			val _s1 = conc.sleep(conc.Duration(millis = 300));   // write after fd1's 200ms poll timed out
			var buf = io.buffer(8);
			io.buffer_write_string(&mut buf, &"x");
			io.buffer_set_len(&mut buf, 1);
			val _w = cs.write(&buf, t);
			val _s2 = conc.sleep(conc.Duration(millis = 700));
			val _c = cs.close(t);
			return 0;
		}
	}
}

fn silent(port: Int) nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.connect(&net.socket_addr("127.0.0.1", port), t) {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(cs) => {
			val _s = conc.sleep(conc.Duration(millis = 1200));   // never write; hold open
			val _c = cs.close(t);
			return 0;
		}
	}
}

pub fn main() nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.listen(&net.socket_addr("127.0.0.1", 0), t) {
		core.Result::Err(e) => { return 100; },
		core.Result::Ok(lis) => {
			val port = lis.local_port();
			val pw = port;
			val ps = port;
			var w = conc.spawn(| | captures(move pw) => { return writer(pw); });
			var s = conc.spawn(| | captures(move ps) => { return silent(ps); });
			var r = 99;
			match net.accept(&lis, t) {
				core.Result::Err(e) => { r = 50; },
				core.Result::Ok(ss1) => {
					match net.accept(&lis, t) {
						core.Result::Err(e) => { r = 60; },
						core.Result::Ok(ss2) => {
							// fd1 poll: writer hasn't written yet -> timeout. Without
							// the cleanup fix this leaves ss1's read_vt = main.
							var a = 70;
							match io.poll(ss1.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 200)) {
								core.Result::Ok(_w) => { a = 2; },
								core.Result::Err(e) => { a = is_timeout(&e); }
							}
							// fd2 poll: ss2 is never written. The writer's ~300ms edge
							// on ss1 lands DURING this poll. A stale ss1 waiter would
							// wake main here -> spurious early Ok. With cleanup, fd2
							// times out.
							var b = 70;
							match io.poll(ss2.raw_fd(), io.IoInterest::Read(), conc.Duration(millis = 800)) {
								core.Result::Ok(_w) => { b = 8; },
								core.Result::Err(e) => { b = is_timeout(&e); }
							}
							r = a + b;
							val _c1 = ss1.close(t);
							val _c2 = ss2.close(t);
						}
					}
				}
			}
			val _jw = w.join();
			val _js = s.join();
			return r;
		}
	}
}
"""


def test_poll_timeout_clears_stale_waiter(tmp_path: Path) -> None:
	# Regression for the stale-wake class: a timed-out poll must clear its reactor
	# registration, so a later edge on that fd does not wake an unrelated poll.
	rc, _ = _run(_compile(tmp_path, _STALE_WAITER_SOURCE, "stale"))
	assert rc == 0, (
		f"stale-waiter cleanup failed (rc={rc}; 8=fd2 poll woke early on fd1's "
		f"stale waiter; 2/4=fd1 poll wrong; 50/60=accept)"
	)


@_VALGRIND_SKIP
def test_yield_now_memcheck(tmp_path: Path) -> None:
	binary = _compile(tmp_path, _YIELD_SOURCE, "yld_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--fair-sched=yes", "--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:900]}"
	assert res.returncode == 0, f"program failed under valgrind: {res.stderr[:500]}"


@_VALGRIND_SKIP
def test_poll_readiness_memcheck(tmp_path: Path) -> None:
	src = _poll_source(_CONN_WRITE_AFTER_DELAY, _BODY_READINESS)
	binary = _compile(tmp_path, src, "rd_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--fair-sched=yes", "--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert res.returncode != 99, f"valgrind found leaks/errors:\n{res.stderr[:900]}"
	assert res.returncode == 0, f"program failed under valgrind: {res.stderr[:500]}"
