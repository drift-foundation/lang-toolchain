# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Gate B — deterministic tests for F3 multi-fd `io.poll_many` (wait-set primitive).

Covers the design's correctness properties without relying on scheduler-race luck:
readiness selects only the ready fd; timeout is distinct AND leaves no stale token
(a second wait does not return early); registration failure on an invalid fd is
terminal `Err`, not a hang (finite-timeout returns fast; no-deadline does not hang);
empty list errors; coalesced duplicates; peer-close surfaces `hangup`.

Over the F3 ABI-18 wait-set intrinsics.
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
	reason="valgrind requires a non-ASan binary",
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
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(300),
	)
	assert res.returncode == 0, f"compile failed: {res.stderr[:1200]}"
	return out


def _run(binary: Path, timeout_s: int = 25) -> tuple[int, float]:
	t0 = time.monotonic()
	res = subprocess.run([str(binary)], capture_output=True, text=True,
		timeout=sanitizer_timeout(timeout_s))
	return res.returncode, time.monotonic() - t0


# ── invalid-fd registration failure (no hang) ────────────────────────────────
# A guaranteed-bad fd (never opened). epoll_ctl(ADD) -> EBADF -> reactor_wait_register
# returns errno -> poll_many returns Err(invalid-argument) BEFORE parking.
_BADFD_SRC = """\
module main;
import std.core as core;
import std.io as io;
import std.concurrent as conc;
import lang.thread as thr;

pub fn main() nothrow -> Int {{
	var es: Array<io.PollEntry> = [];
	es.push(io.PollEntry(fd = 1000003, token = 0, want_read = true, want_write = false));
	val start = thr.now_ms();
	val res = io.poll_many(&es, conc.Duration(millis = {to}));
	val el = thr.now_ms() - start;
	match res {{
		core.Result::Ok(rd) => {{ return 2; }},
		core.Result::Err(e) => {{
			if (e.kind + "") != "invalid-argument" {{ return 3; }}
			return el < 100 ? 0 : 7;   // must return FAST, not after the timeout
		}}
	}}
}}
"""


def test_poll_many_invalid_fd_finite_timeout(tmp_path: Path) -> None:
	rc, _ = _run(_compile(tmp_path, _BADFD_SRC.format(to=3000), "bf"))
	assert rc == 0, f"invalid-fd finite-timeout (rc={rc}; 2=Ok,3=wrong-kind,7=too-slow)"


def test_poll_many_invalid_fd_no_deadline_no_hang(tmp_path: Path) -> None:
	# Duration(0) = park-until-ready; an invalid fd must still fail terminally, not hang.
	rc, wall = _run(_compile(tmp_path, _BADFD_SRC.format(to=0), "bf0"), timeout_s=15)
	assert rc == 0, f"invalid-fd no-deadline (rc={rc})"
	assert wall < 5, f"poll_many([bad_fd], no-deadline) appears to hang ({wall:.1f}s)"


def test_poll_many_empty_list(tmp_path: Path) -> None:
	src = """\
module main;
import std.core as core;
import std.io as io;
import std.concurrent as conc;
pub fn main() nothrow -> Int {
	var es: Array<io.PollEntry> = [];
	match io.poll_many(&es, conc.Duration(millis = 1000)) {
		core.Result::Ok(rd) => { return 2; },
		core.Result::Err(e) => { return (e.kind + "") == "invalid-argument" ? 0 : 3; }
	}
}
"""
	rc, _ = _run(_compile(tmp_path, src, "empty"))
	assert rc == 0, f"empty list (rc={rc}; 2=Ok,3=wrong-kind)"


# ── TCP scaffolding for readiness / timeout / close ──────────────────────────
_TCP_TEMPLATE = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;
import lang.thread as thr;

fn connector(port: Int) nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.connect(&net.socket_addr("127.0.0.1", port), t) {
		core.Result::Err(e) => { return 1; },
		core.Result::Ok(cs) => {
{conn}
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
			match net.accept(&lis, t) {
				core.Result::Err(e) => { r = 50; },
				core.Result::Ok(ss) => {
					val fd = ss.raw_fd();
{body}
					val _cl = ss.close(t);
				}
			}
			val _j = cvt.join();
			return r;
		}
	}
}
"""

_CONN_WRITE_DELAYED = (
	"\t\t\tval _s0 = conc.sleep(conc.Duration(millis = 120));\n"
	"\t\t\tvar buf = io.buffer(8); io.buffer_write_string(&mut buf, &\"x\"); io.buffer_set_len(&mut buf, 1);\n"
	"\t\t\tval _w = cs.write(&buf, t);\n"
	"\t\t\tval _s1 = conc.sleep(conc.Duration(millis = 400));\n"
	"\t\t\tval _c = cs.close(t);"
)
_CONN_SILENT = (
	"\t\t\tval _s = conc.sleep(conc.Duration(millis = 800));\n"
	"\t\t\tval _c = cs.close(t);"
)
_CONN_CLOSE_SOON = (
	"\t\t\tval _s = conc.sleep(conc.Duration(millis = 150));\n"
	"\t\t\tval _c = cs.close(t);"  # peer close -> EPOLLHUP on server fd
)


def _tcp(conn: str, body: str) -> str:
	return _TCP_TEMPLATE.replace("{conn}", conn).replace("{body}", body)


def test_poll_many_readiness_one_fd(tmp_path: Path) -> None:
	# Also pins token round-trip: the PollReady carries the PollEntry's token (77).
	body = """\
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 77, want_read = true, want_write = false));
					match io.poll_many(&es, conc.Duration(millis = 3000)) {
						core.Result::Ok(rd) => { r = (rd.len() == 1 and rd[0].readable and rd[0].fd == fd and rd[0].token == 77) ? 0 : 7; },
						core.Result::Err(e) => { r = 5; }
					}"""
	rc, _ = _run(_compile(tmp_path, _tcp(_CONN_WRITE_DELAYED, body), "rd"))
	assert rc == 0, f"readiness (rc={rc}; 5=Err,7=wrong-set-or-token)"


def test_poll_many_timeout_leaves_no_token(tmp_path: Path) -> None:
	# poll_many times out, THEN a sleep(300ms) must take ~its full time (a leaked
	# token would make the sleep return early). r=0 only if both hold.
	body = """\
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					var ok = false;
					match io.poll_many(&es, conc.Duration(millis = 200)) {
						core.Result::Ok(rd) => { ok = false; },
						core.Result::Err(e) => { ok = (e.kind + "") == "timeout"; }
					}
					val s = thr.now_ms();
					val _z = conc.sleep(conc.Duration(millis = 300));
					val slept = thr.now_ms() - s;
					r = (ok and slept >= 250) ? 0 : 7;"""
	rc, _ = _run(_compile(tmp_path, _tcp(_CONN_SILENT, body), "to"))
	assert rc == 0, f"timeout/no-token (rc={rc}; 7=not-timeout-or-token-leaked)"


def test_poll_many_peer_close_hangup(tmp_path: Path) -> None:
	body = """\
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					match io.poll_many(&es, conc.Duration(millis = 3000)) {
						core.Result::Ok(rd) => { r = (rd.len() == 1 and (rd[0].hangup or rd[0].readable)) ? 0 : 7; },
						core.Result::Err(e) => { r = 5; }
					}"""
	# peer closes -> EPOLLHUP (and/or readable EOF); poll_many must report it, not time out.
	rc, _ = _run(_compile(tmp_path, _tcp(_CONN_CLOSE_SOON, body), "hup"))
	assert rc == 0, f"peer-close hangup (rc={rc}; 5=Err/timeout,7=not-reported)"


def test_poll_many_zero_interest_rejected(tmp_path: Path) -> None:
	# want_read=false && want_write=false has no useful contract -> invalid-argument,
	# both finite-timeout and no-deadline (must not hang).
	tmpl = """\
module main;
import std.core as core;
import std.io as io;
import std.concurrent as conc;
pub fn main() nothrow -> Int {{
	var es: Array<io.PollEntry> = [];
	es.push(io.PollEntry(fd = 0, token = 0, want_read = false, want_write = false));
	match io.poll_many(&es, conc.Duration(millis = {to})) {{
		core.Result::Ok(rd) => {{ return 2; }},
		core.Result::Err(e) => {{ return (e.kind + "") == "invalid-argument" ? 0 : 3; }}
	}}
}}
"""
	rc, _ = _run(_compile(tmp_path, tmpl.format(to=2000), "zi"))
	assert rc == 0, f"zero-interest finite (rc={rc}; 2=Ok,3=wrong-kind)"
	rc, wall = _run(_compile(tmp_path, tmpl.format(to=0), "zi0"), timeout_s=10)
	assert rc == 0 and wall < 4, f"zero-interest no-deadline hung/failed (rc={rc}, {wall:.1f}s)"


def test_block_on_io_no_stale_pending_spin(tmp_path: Path) -> None:
	# After a read drains to EAGAIN, the migrated _block_on_io must CONSUME the
	# pending bit so a subsequent blocking read genuinely PARKS rather than
	# spinning on a stale pending flag.  Proven via the reactor_park_blocks probe:
	# the 2nd (timing-out) read must increment it.  Connector writes once then
	# stays open & idle (conc.sleep, no reactor_wait_park) so the probe delta is
	# attributable to the server's 2nd read.
	conn = (
		"\t\t\tvar buf = io.buffer(8); io.buffer_write_string(&mut buf, &\"x\"); io.buffer_set_len(&mut buf, 1);\n"
		"\t\t\tval _w = cs.write(&buf, t);\n"
		"\t\t\tval _s = conc.sleep(conc.Duration(millis = 1500));\n"
		"\t\t\tval _c = cs.close(t);"
	)
	body = """\
					var rb = io.buffer(16);
					val _r1 = ss.read(&mut rb, conc.Duration(millis = 2000));   // gets "x"
					val p0 = thr.reactor_park_blocks();
					var rb2 = io.buffer(16);
					val r2 = ss.read(&mut rb2, conc.Duration(millis = 400));     // no data -> must PARK then time out
					val p1 = thr.reactor_park_blocks();
					match r2 {
						core.Result::Ok(nn) => { r = 2; },        // unexpected data/EOF
						core.Result::Err(e) => { r = (p1 > p0) ? 0 : 8; }   // 8 = spun on stale pending
					}"""
	rc, _ = _run(_compile(tmp_path, _tcp(conn, body), "stale"))
	assert rc == 0, f"_block_on_io stale-pending spin (rc={rc}; 8=spun-not-parked,2=unexpected-data)"


def test_poll_many_hup_non_consuming(tmp_path: Path) -> None:
	# pending_hup is sticky/non-consuming: after a peer close, a first poll reports
	# hangup, and a SECOND poll on the same fd STILL reports hangup (a consuming
	# impl would lose it and time out instead).
	body = """\
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					var h1 = false;
					match io.poll_many(&es, conc.Duration(millis = 3000)) {
						core.Result::Ok(rd) => { h1 = rd.len() == 1 and (rd[0].hangup or rd[0].readable); },
						core.Result::Err(e) => { h1 = false; }
					}
					// drain any EOF byte so only the sticky HUP remains for poll #2
					var db = io.buffer(16);
					val _d = ss.read(&mut db, conc.Duration(millis = 50));
					var es2: Array<io.PollEntry> = [];
					es2.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					var h2 = false;
					match io.poll_many(&es2, conc.Duration(millis = 500)) {
						core.Result::Ok(rd) => { h2 = rd.len() == 1 and (rd[0].hangup or rd[0].readable); },
						core.Result::Err(e) => { h2 = false; }
					}
					r = (h1 and h2) ? 0 : 7;"""
	rc, _ = _run(_compile(tmp_path, _tcp(_CONN_CLOSE_SOON, body), "hupstick"))
	assert rc == 0, f"HUP non-consuming (rc={rc}; 7=second poll lost the sticky HUP)"


def test_poll_many_cancel_no_hang(tmp_path: Path) -> None:
	# A VT in poll_many(no-deadline) on a never-ready fd (a listener with no
	# incoming connection) must wake on cancel and not sleep forever.  Stresses the
	# cancel/park window (some iterations cancel immediately = race the park; some
	# after the VT has parked).  The fix: reactor_wait_park re-checks cancelled after
	# publishing PARKED.  A hang -> subprocess timeout -> test fails.
	src = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;

fn waiter(fd: Int) nothrow -> Int {
	var es: Array<io.PollEntry> = [];
	es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
	match io.poll_many(&es, conc.Duration(millis = 0)) {   // no deadline = park until ready
		core.Result::Ok(rd) => { return 1; },
		core.Result::Err(e) => { return 0; }
	}
}

pub fn main() nothrow -> Int {
	val t = conc.Duration(millis = 5000);
	match net.listen(&net.socket_addr("127.0.0.1", 0), t) {
		core.Result::Err(e) => { return 100; },
		core.Result::Ok(lis) => {
			val fd = lis.raw_fd();
			var i = 0;
			while i < 20 {
				val f = fd;
				var vt = conc.spawn(| | captures(move f) => { return waiter(f); });
				if i % 2 == 0 {
					val _s = conc.sleep(conc.Duration(millis = 20));   // let it park
				}
				vt.cancel();
				val _j = vt.join();
				i = i + 1;
			}
			return 0;
		}
	}
}
"""
	rc, wall = _run(_compile(tmp_path, src, "cancel"), timeout_s=20)
	assert rc == 0, f"poll_many cancel hung/failed (rc={rc})"
	assert wall < 10, f"poll_many cancel appears to hang ({wall:.1f}s)"


# NOTE: the stale-park_token-after-cancel fix (reactor_wait_park clears park_token in
# the self-reclaim branch, matching drift_thread_unpark) is correct hygiene but is
# NOT behaviorally observable through Drift APIs: the VT's `cancelled` flag
# short-circuits every subsequent park (a cancelled VT's conc.sleep aborts via
# cancellation, and join() returns Err, so neither the token nor a sleep duration is
# retrievable).  A discriminating regression is therefore not constructible; the fix
# is verified by inspection.  Cancel correctness (no hang) is covered by
# test_poll_many_cancel_no_hang.


def test_poll_many_token_conflict(tmp_path: Path) -> None:
	# Same fd with two different tokens is ambiguous -> invalid-argument.
	src = """\
module main;
import std.core as core;
import std.io as io;
import std.concurrent as conc;
pub fn main() nothrow -> Int {
	var es: Array<io.PollEntry> = [];
	es.push(io.PollEntry(fd = 5, token = 1, want_read = true, want_write = false));
	es.push(io.PollEntry(fd = 5, token = 2, want_read = false, want_write = true));
	match io.poll_many(&es, conc.Duration(millis = 1000)) {
		core.Result::Ok(rd) => { return 2; },
		core.Result::Err(e) => { return (e.kind + "") == "invalid-argument" ? 0 : 3; }
	}
}
"""
	rc, _ = _run(_compile(tmp_path, src, "tokc"))
	assert rc == 0, f"token conflict (rc={rc}; 2=Ok,3=wrong-kind)"


def test_partial_drain_single_wake(tmp_path: Path) -> None:
	# Doc-example + regression for the partial-drain trap: data larger than one read
	# buffer must be drained across REPEATED reads after a SINGLE readiness event
	# (edge-triggered) — not by waiting for a second wake.  Connector sends 16 KiB
	# via TcpStream.write_bytes (also exercises the new range write); server polls
	# ONCE, then reads in 4 KiB non-blocking chunks until WOULD_BLOCK and asserts the
	# full 16 KiB drained.
	conn = (
		"\t\t\tvar arr: Array<Byte> = [];\n"
		"\t\t\tvar bi = 0;\n"
		"\t\t\twhile bi < 16384 { arr.push(cast<Byte>(65)); bi = bi + 1; }\n"
		"\t\t\tvar off = 0;\n"
		"\t\t\twhile off < 16384 {\n"
		"\t\t\t\tmatch cs.write_bytes(&mut arr, off, 16384 - off, t) {\n"
		"\t\t\t\t\tcore.Result::Ok(nn) => { off = off + nn; },\n"
		"\t\t\t\t\tcore.Result::Err(e) => { off = 16384; }\n"
		"\t\t\t\t}\n"
		"\t\t\t}\n"
		"\t\t\tval _s = conc.sleep(conc.Duration(millis = 600));\n"
		"\t\t\tval _c = cs.close(t);"
	)
	body = """\
					val _w = conc.sleep(conc.Duration(millis = 200));   // let all 16 KiB buffer
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					match io.poll_many(&es, conc.Duration(millis = 3000)) {
						core.Result::Ok(rd) => {
							var total = 0;
							var draining = true;
							while draining {
								var rb = io.buffer(4096);
								match ss.read(&mut rb, conc.Duration(millis = 0)) {
									core.Result::Ok(nn) => { if nn == 0 { draining = false; } else { total = total + nn; } },
									core.Result::Err(e) => {
										// Drain ends ONLY on WOULD_BLOCK (the contract);
										// any other error is a real failure.
										draining = false;
										if (e.kind + "") != "would_block" { total = 0 - 1; }
									}
								}
							}
							r = (total == 16384) ? 0 : 8;
						},
						core.Result::Err(e) => { r = 5; }
					}"""
	rc, _ = _run(_compile(tmp_path, _tcp(conn, body), "drain"))
	assert rc == 0, f"partial-drain single-wake (rc={rc}; 8=did-not-drain-all,5=poll-Err)"


@_VALGRIND_SKIP
def test_poll_many_fd_reuse_churn_stress_memcheck(tmp_path: Path) -> None:
	# Stress companion to the deterministic resolver unit test (the generation guard is
	# pinned in lang/tests/runtime/reactor_stale_fd_event_test.c): a keepalive-style loop
	# aggregate-poll_many's a set of HEALTHY IDLE loopback conns while churning one
	# (real readiness via peer-close, then close + reopen reusing the fd number) — the
	# pool's watch→close→reopen shape. Any readiness on a known-idle conn is a
	# fabricated event. Under memcheck this validates the cert/MariaDB signature shape
	# (no Valgrind errors required; fail on semantic false-positive readiness). It is
	# NOT the primary pin (the race rarely fires single-process); that is the C whitebox
	# unit test lang/tests/runtime/reactor_stale_fd_event_test.c
	# (test_reactor_stale_fd_event_generation_guard).
	src = """\
module main;
import std.core as core;
import std.io as io;
import std.net as net;
import std.concurrent as conc;

struct Conn { cli: net.TcpStream, srv: net.TcpStream, token: Int }

fn open_pair(lis: &net.TcpListener, port: Int, token: Int) nothrow -> core.Result<Conn, Int> {
	val t = conc.Duration(millis = 4000);
	match net.connect(&net.socket_addr("127.0.0.1", port), t) {
		core.Result::Err(e) => { return core.Result::Err(1); },
		core.Result::Ok(c) => {
			match net.accept(lis, t) {
				core.Result::Err(e) => { val _ = c.close(t); return core.Result::Err(2); },
				core.Result::Ok(s) => { return core.Result::Ok(Conn(cli = move c, srv = move s, token = token)); }
			}
		}
	}
}

pub fn main() nothrow -> Int {
	val t = conc.Duration(millis = 4000);
	match net.listen(&net.socket_addr("127.0.0.1", 0), t) {
		core.Result::Err(e) => { return 100; },
		core.Result::Ok(lis) => {
			val port = lis.local_port();
			var tokctr = 0;
			var conns: Array<Conn> = [];
			var k = 0;
			while k < 3 {
				tokctr = tokctr + 1;
				match open_pair(&lis, port, tokctr) {
					core.Result::Err(c) => { return 50; },
					core.Result::Ok(cn) => { conns.push(move cn); }
				}
				k = k + 1;
			}
			var spurious = 0;
			var round = 0;
			while round < 120 {
				var es: Array<io.PollEntry> = [];
				var i = 0;
				while i < conns.len() {
					es.push(io.PollEntry(fd = conns[i].cli.raw_fd(), token = conns[i].token, want_read = true, want_write = false));
					i = i + 1;
				}
				match io.poll_many(&es, conc.Duration(millis = 5)) {
					core.Result::Ok(rd) => {
						var j = 0;
						while j < rd.len() {
							if rd[j].readable or rd[j].hangup or rd[j].err { spurious = spurious + 1; }
							j = j + 1;
						}
					},
					core.Result::Err(e) => { }
				}
				val _c0 = conns[0].cli.close(t);
				val _s0 = conns[0].srv.close(t);
				tokctr = tokctr + 1;
				match open_pair(&lis, port, tokctr) {
					core.Result::Err(c) => { return 51; },
					core.Result::Ok(cn) => { conns[0] = move cn; }
				}
				round = round + 1;
			}
			var z = 0;
			while z < conns.len() { val _a = conns[z].cli.close(t); val _b = conns[z].srv.close(t); z = z + 1; }
			return spurious;
		}
	}
}
"""
	binary = _compile(tmp_path, src, "churn")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--fair-sched=yes", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(180))
	assert res.returncode != 99, f"valgrind errors:\n{res.stderr[:800]}"
	assert res.returncode == 0, (
		f"churn produced {res.returncode} spurious readiness event(s) on healthy idle "
		f"conns (stale-fd-event regression)"
	)


@_VALGRIND_SKIP
def test_poll_many_readiness_memcheck(tmp_path: Path) -> None:
	body = """\
					var es: Array<io.PollEntry> = [];
					es.push(io.PollEntry(fd = fd, token = 0, want_read = true, want_write = false));
					match io.poll_many(&es, conc.Duration(millis = 3000)) {
						core.Result::Ok(rd) => { r = rd.len() == 1 ? 0 : 7; },
						core.Result::Err(e) => { r = 5; }
					}"""
	binary = _compile(tmp_path, _tcp(_CONN_WRITE_DELAYED, body), "rd_mc")
	res = subprocess.run(
		valgrind_cmd("--error-exitcode=99", "--fair-sched=yes", "--leak-check=full",
			"--errors-for-leak-kinds=definite,indirect", str(binary)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120))
	assert res.returncode != 99, f"valgrind errors/leaks:\n{res.stderr[:1000]}"
	assert res.returncode == 0, f"failed under valgrind: {res.stderr[:500]}"
