# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""CB-DROP LIVENESS FLAG pins (LANGUAGE_BUG
issues/channel-receiver-destroy-bounds-check-crash-at-exit, fixed
2026-07-10, folded into the 0.33.79 candidate).

The callback env drop thunk used to run user `Destructible::destroy`
unconditionally on every env slot — including slots the body MOVED OUT
and zero-backed. That violated the spec's destroy contract
(drift-lang-spec §5.11: "exactly once"; §4: destroy "expects every
field to be in a fully-formed state ... destructors must remain simple
and total"): refcount releases are zero-safe, but user destroy is
arbitrary code — `Receiver::destroy` dereferences its inner Arc and
aborted (`drift_bounds_check_fail`) on the moved-from zero sentinel at
process exit; quieter Destructible types got a silent phantom
zero-value destroy on the NORMAL completion path.

Fix shape pinned here: MOVE-kind captures whose drop can invoke a user
destructor get a trailing `__live<slot>` Int flag in the env struct
(init 1; the capture move-out stores 0 alongside the value zero-back);
`_emit_callback_drop_thunk` guards those slots' drops on the flag.
Moved-out slots receive NO destroy; live slots receive it EXACTLY once.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lang.codegen.llvm.test_utils import asan_active, sanitizer_timeout, valgrind_cmd

ROOT = Path(__file__).resolve().parents[3]

# (1) The phantom-destroy probe: destroy must print EXACTLY once (the
# body-local drop of the moved-out value), never on the zeroed slot.
_TOKEN_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct Token { tag: Int }

implement core.Destructible for Token {
	pub fn destroy(var self: Token) nothrow -> Void {
		match self.tag == 42 {
			true => { console.println("destroy-live"); },
			false => { console.println("destroy-zeroed"); },
		}
	}
}

pub fn main() nothrow -> Int {
	val t = Token(tag = 42);
	var vt = conc.spawn_cb(|| captures(move t) => {
		var mine = move t;
		match mine.tag == 42 { true => { console.println("fiber-got-live"); }, false => { console.println("fiber-got-other"); } }
		return 0;
	});
	match vt.join() {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	return 0;
}
"""

# (2) The reported Receiver repro: spawned VT captures/moves a
# Receiver, main returns without joining. Was SIGABRT at exit.
_RECEIVER_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
	var halves = conc.channel<type String>();
	val sender = halves.take_sender();
	val receiver = halves.take_receiver();
	val _vt = conc.spawn(core.callback0(|| captures(move receiver) => {
		var r = move receiver;
		match r.recv() { Ok(_) => {}, Err(_) => {} }
		return 0;
	}));
	val _sendResult = sender.send("hello" + "");
	console.println("ok");
	return 0;
}
"""

# (3) Fiber still BLOCKED in recv() at process exit (no send at all).
_BLOCKED_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
	var halves = conc.channel<type String>();
	val sender = halves.take_sender();
	val receiver = halves.take_receiver();
	val _vt = conc.spawn(core.callback0(|| captures(move receiver) => {
		var r = move receiver;
		match r.recv() { Ok(_) => {}, Err(_) => {} }
		return 0;
	}));
	console.println("ok");
	return 0;
}
"""

# (4) Join-before-return control (was already green; must stay green).
_JOINED_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

pub fn main() nothrow -> Int {
	var halves = conc.channel<type String>();
	val sender = halves.take_sender();
	val receiver = halves.take_receiver();
	var vt = conc.spawn(core.callback0(|| captures(move receiver) => {
		var r = move receiver;
		match r.recv() { Ok(_) => {}, Err(_) => {} }
		return 0;
	}));
	val _sendResult = sender.send("hello" + "");
	match vt.join() {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	console.println("ok");
	return 0;
}
"""

# (5) NON-moved Destructible capture: the env slot stays live and the
# flag-guarded drop must still run destroy EXACTLY once.
_NON_MOVED_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct Token { tag: Int }

implement core.Destructible for Token {
	pub fn destroy(var self: Token) nothrow -> Void {
		match self.tag == 42 {
			true => { console.println("destroy-live"); },
			false => { console.println("destroy-zeroed"); },
		}
	}
}

fn peek(t: &Token) nothrow -> Int {
	return t.tag;
}

pub fn main() nothrow -> Int {
	val t = Token(tag = 42);
	var vt = conc.spawn_cb(|| captures(move t) => {
		// Reads only — never moves the capture out.  The env slot
		// remains the owner; the flag stays 1; the env drop runs
		// destroy exactly once.
		return peek(&t);
	});
	match vt.join() {
		core.Result::Ok(v) => { match v == 42 { true => {}, false => { return 2; } } },
		core.Result::Err(_) => { return 1; },
	}
	return 0;
}
"""

# (6) CONDITIONAL move — the reason the flag is a RUNTIME value, not a
# static property: the same lambda moves the capture out on one branch
# only.  Both runtime outcomes must destroy exactly once.
_CONDITIONAL_SOURCE_TMPL = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct Token {{ tag: Int }}

implement core.Destructible for Token {{
	pub fn destroy(var self: Token) nothrow -> Void {{
		match self.tag == 42 {{
			true => {{ console.println("destroy-live"); }},
			false => {{ console.println("destroy-zeroed"); }},
		}}
	}}
}}

fn consume(var t: Token) nothrow -> Int {{
	return t.tag;
}}

pub fn main() nothrow -> Int {{
	val flag = {flag};
	val t = Token(tag = 42);
	var vt = conc.spawn_cb(|| captures(copy flag, move t) => {{
		if flag {{
			val v = consume(move t);
			return v;
		}}
		return 0;
	}});
	match vt.join() {{
		core.Result::Ok(_) => {{}},
		core.Result::Err(_) => {{ return 1; }},
	}}
	return 0;
}}
"""


# (7) GENERIC user Destructible moved out inside a BOXED callback
# (core.callback0), instantiated at Wrap<String>.  Generic
# instantiations are the shape where the trait prover
# (`is_destructible`) can lag `destructor_fns` (types_core.has_drop
# documents the divergence for cross-package builds); the flag
# predicate mirrors codegen's full destructor authority (exact
# destructor_fns -> trait prover -> (name, module_id) nominal
# fallback) so this shape must flag and stay exactly-once.
_GENERIC_BOXED_SOURCE = """\
module main;

import std.core as core;
import std.concurrent as conc;
import std.console as console;

struct Wrap<T> { tag: Int, payload: T }

implement<T> core.Destructible for Wrap<T> {
	pub fn destroy(var self: Wrap<T>) nothrow -> Void {
		match self.tag == 42 {
			true => { console.println("destroy-live"); },
			false => { console.println("destroy-zeroed"); },
		}
	}
}

fn consume(var w: Wrap<String>) nothrow -> Int {
	return w.tag;
}

pub fn main() nothrow -> Int {
	val w = Wrap(tag = 42, payload = "p" + "");
	var vt = conc.spawn(core.callback0(|| captures(move w) => {
		val v = consume(move w);
		return v;
	}));
	match vt.join() {
		core.Result::Ok(_) => {},
		core.Result::Err(_) => { return 1; },
	}
	return 0;
}
"""


def _compile(tmp_path: Path, source: str, *extra: str) -> Path:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / "test_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 *extra, str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True,
		timeout=sanitizer_timeout(240), env=os.environ.copy(),
	)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1800:]}"
	return out

def _run(out: Path) -> subprocess.CompletedProcess[str]:
	return subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(20))


def _assert_destroy_exactly_once(run: subprocess.CompletedProcess[str]) -> None:
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	live = run.stdout.count("destroy-live")
	zeroed = run.stdout.count("destroy-zeroed")
	assert live == 1, f"destroy-live ran {live} times (want exactly 1):\n{run.stdout}"
	assert zeroed == 0, f"phantom zero-value destroy fired {zeroed} times:\n{run.stdout}"


def test_moved_out_capture_no_phantom_destroy(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _TOKEN_SOURCE))
	assert "fiber-got-live" in run.stdout, run.stdout
	_assert_destroy_exactly_once(run)


def test_receiver_unjoined_exit_clean(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _RECEIVER_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode} (SIGABRT=134 was the bug); stderr:\n{run.stderr[-800:]}"
	assert "ok" in run.stdout


def test_receiver_unjoined_exit_clean_asan(tmp_path: Path) -> None:
	out = _compile(tmp_path, _RECEIVER_SOURCE, "--sanitize=address,undefined")
	run = _run(out)
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-1200:]}"
	assert "ERROR: AddressSanitizer" not in run.stderr, run.stderr[-1200:]


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_receiver_unjoined_exit_clean_valgrind(tmp_path: Path) -> None:
	out = _compile(tmp_path, _RECEIVER_SOURCE)
	vg_log = tmp_path / "valgrind.log"
	vg = subprocess.run(
		valgrind_cmd("--error-exitcode=97", f"--log-file={vg_log}", str(out)),
		capture_output=True, text=True, timeout=sanitizer_timeout(120),
	)
	assert vg.returncode == 0, f"valgrind errors:\n{(vg_log.read_text() if vg_log.exists() else '')[-1500:]}"


def test_receiver_blocked_in_recv_exit_clean(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _BLOCKED_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"


def test_receiver_join_before_return_control(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _JOINED_SOURCE))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ok" in run.stdout


def test_non_moved_capture_destroys_exactly_once(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _NON_MOVED_SOURCE))
	_assert_destroy_exactly_once(run)


def test_conditional_move_taken_destroys_exactly_once(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _CONDITIONAL_SOURCE_TMPL.format(flag="true")))
	_assert_destroy_exactly_once(run)


def test_conditional_move_not_taken_destroys_exactly_once(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _CONDITIONAL_SOURCE_TMPL.format(flag="false")))
	_assert_destroy_exactly_once(run)


@pytest.mark.skipif(shutil.which("valgrind") is None, reason="valgrind required")
@pytest.mark.skipif(asan_active(), reason="valgrind cannot run ASan-instrumented binaries")
def test_conditional_move_valgrind_both_paths(tmp_path: Path) -> None:
	"""The leak direction on both runtime outcomes: the flag must not
	cause a MISSED drop (live slot skipped) either."""
	for i, flag in enumerate(("true", "false")):
		d = tmp_path / f"v{i}"
		d.mkdir()
		out = _compile(d, _CONDITIONAL_SOURCE_TMPL.format(flag=flag))
		vg_log = d / "valgrind.log"
		vg = subprocess.run(
			valgrind_cmd("--leak-check=full",
				"--errors-for-leak-kinds=definite,indirect",
				"--error-exitcode=97", f"--log-file={vg_log}", str(out)),
			capture_output=True, text=True, timeout=sanitizer_timeout(120),
		)
		log = vg_log.read_text() if vg_log.exists() else ""
		assert vg.returncode == 0, f"[flag={flag}] valgrind errors:\n{log[-1500:]}"


def test_pkg_mode_boxed_callback_env_coherent(tmp_path: Path) -> None:
	"""Package-mode check: the Receiver repro compiled against the
	SIGNED std package — the callback env (with its flag fields) is
	created by consumer lowering and flows through the packaged
	std.concurrent spawn/executor machinery that ultimately drops it.
	Proves env layout + drop-thunk codegen stay coherent across
	package lowering/serialization."""
	from lang.tests.driver.pkg_test_helpers import _build_signed_stdlib, STD_VERSION

	pkg_root, trust_path, core_trust_path, empty_stdlib = _build_signed_stdlib(tmp_path)
	consumer_dir = tmp_path / "consumer_src"
	consumer_dir.mkdir(exist_ok=True)
	(consumer_dir / "consumer.drift").write_text(_RECEIVER_SOURCE.replace("module main;", "module consumer;"))
	out_bin = tmp_path / "consumer_bin"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 str(consumer_dir / "consumer.drift"),
		 "--stdlib-root", str(empty_stdlib),
		 "--package-root", str(pkg_root),
		 "--dep", f"std@{STD_VERSION}",
		 "--trust-store", str(trust_path),
		 "--dev-core-trust-store", str(core_trust_path),
		 "--target-word-bits", "64",
		 "--entry", "consumer::main",
		 "-o", str(out_bin)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(300),
	)
	assert res.returncode == 0, f"pkg compile failed:\n{res.stderr[-2000:]}"
	run = subprocess.run([str(out_bin)], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	assert run.returncode == 0, f"exit {run.returncode}; stderr:\n{run.stderr[-800:]}"
	assert "ok" in run.stdout


def test_generic_destructible_boxed_callback_move_out(tmp_path: Path) -> None:
	run = _run(_compile(tmp_path, _GENERIC_BOXED_SOURCE))
	_assert_destroy_exactly_once(run)
