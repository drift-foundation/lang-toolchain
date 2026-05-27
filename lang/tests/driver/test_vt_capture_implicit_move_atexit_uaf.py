# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: VT-spawned closure that consumes a
non-Copy move-captured local via an IMPLICIT move (bare HVar in a
by-value call arg, no explicit `move` keyword) double-drops the
capture — once at the implicit consume site and again from the
callback's drop thunk.  Surfaces as a use-after-free at atexit when
the same allocation also lives in the global registry.

Reported by the PushCoin bookkeeper team (filed against 0.33.5 in
`~/src/pushcoin/work/drift-vt-drop-atexit-use-after-free.md`).
Original valgrind shape:
```
Invalid read of size 8
   at drift_atomic_fetch_sub_int
   by _typebox_drop_impl__inst__*
   by drift_runtime_registry_cleanup_atexit
   by drift_run_main_on_vt
Block was 0 bytes inside a block of size N free'd
   at free
   by __drift_cb_drop_*
   by __drift_iface_drop_helper
   by __drift_cb_drop_*
   by drift_vt_fiber_entry
```

**Root cause** (compiler, not runtime).  `_visit_expr_HMove` in
`lang/driftc/stage2/hir_to_mir.py` zeros the env slot for explicit
`move <cap>` reads (so the callback's drop thunk later loads a
zero / null value for that field and the per-field drop becomes a
no-op).  `_visit_expr_HVar`'s capture-load path emits no zero-back
— so an IMPLICIT consume (the borrow checker silently inserts a
move-in-consuming-position when the user wrote a bare HVar in an
owned-arg call site) leaves the env slot pointing at the original
Arc backing.  The closure body's consumed copy drops the Arc once
at end-of-callee scope, and the callback drop thunk drops the env
field again — the second drop hits refcount 0 and frees the
backing.  The global registry's atexit cleanup then dereferences
the freed block to decrement its own (independent) Arc-clone
refcount.

The user-side workaround is to write `move gw` explicitly at the
call site; same source compiles + runs clean.  That confirms the
defect is in implicit-move codegen for callback move-captures, not
in the application's ownership flow.

The fix mirrors `_visit_expr_HMove`'s slot-zero treatment in
`_visit_expr_HVar`'s capture-load path: when reading a non-Copy
MOVE-captured local, also store the type's zero value back to the
env slot so the callback's drop thunk no-ops on that field.

**ABI implication.**  Pure codegen fix — emitted IR only.  No
runtime ABI surface changes, no `.zdmp` schema change, no signing
path change.  Per
`docs/design/drift-lang-abi.md` §"When to bump" / §"Stable ABI
Artifact Rule" this lands without an ABI bump; existing ABI-14
dependency artifacts remain consumable unchanged.

This test compiles a minimal repro and runs it under valgrind
(memcheck).  Pre-fix valgrind reports the same invalid-read +
freed-by-cb_drop shape.  Post-fix valgrind is clean (0 errors).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.runtime as rt;

pub struct Gateway {
\tpub name: String
}

implement Gateway {
\tpub fn complete(self: &Gateway) nothrow -> Void {
\t\tval _ = self;
\t\treturn;
\t}
}

fn consume_gw(gw: arc.Arc<Gateway>) nothrow -> Void {
\tval a = gw.get();
\ta.complete();
\treturn;
}

fn install_gateway() nothrow -> Bool {
\tval gw = arc.arc(Gateway(name = "test"));
\treturn rt.global_registry().set<type arc.Arc<Gateway>>(move gw);
}

fn _run() -> Int {
\tif not install_gateway() {
\t\treturn 1;
\t}
\tval gw_for_worker = rt.expect<type arc.Arc<Gateway>>(rt.global_registry(), "missing-gw").clone();
\tvar vt = conc.spawn(| | captures(move gw_for_worker) nothrow => {
\t\t// IMPLICIT move (no `move` keyword) — bare HVar in a
\t\t// by-value call arg.  This is the failing shape from
\t\t// bookkeeper's customers_snapshot.drift worker spawn.
\t\tconsume_gw(gw_for_worker);
\t\treturn 0;
\t});
\tmatch vt.join() {
\t\tcore.Result::Ok(_) => { return 0; },
\t\tcore.Result::Err(_) => { return 2; }
\t}
}

fn main() nothrow -> Int {
\treturn try _run() catch { 99 };
}
"""


def _compile(tmp_path: Path) -> tuple[int, str, Path]:
	src = tmp_path / "main.drift"
	src.write_text(_SOURCE)
	out = tmp_path / "repro"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	return res.returncode, res.stderr, out


_NON_CONSUMING_THEN_CONSUMING_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;

pub struct Holder {
\tpub v: Int
}

fn consume_arc(a: arc.Arc<Holder>) nothrow -> Int {
\treturn a.get().v + 1;
}

fn main() nothrow -> Int {
\tval h = arc.arc(Holder(v = 7));
\tvar vt = conc.spawn(| | captures(move h) nothrow => {
\t\t// Non-consuming method-call receiver auto-borrow on a MOVE
\t\t// capture.  This must NOT zero the env slot — the subsequent
\t\t// `consume_arc(h)` needs to find a live Arc in the env.
\t\tval first = h.get();
\t\tval n = first.v;
\t\tval m = consume_arc(h);
\t\treturn n + m;
\t});
\tmatch vt.join() {
\t\tcore.Result::Ok(r) => { return r; },
\t\tcore.Result::Err(_) => { return 99; }
\t}
}
"""


def test_non_consuming_move_capture_read_does_not_zero_env_slot(tmp_path: Path) -> None:
	"""Positive regression: a MOVE-captured Arc<T> is read
	non-consumingly (auto-borrow method-call receiver) and THEN
	consumed by a by-value call.  This pattern was broken by the
	first iteration of the fix (which blanket-zeroed every HVar
	read of a destructible MOVE capture); pinned to ensure
	implicit-move zero-back fires ONLY at actual consuming
	positions (call-arg of a non-Copy-typed by-value param), not
	on every HVar load.

	Expected: compile rc=0, binary returns 15 (7 + 8), and
	(if valgrind is available) no errors at exit.
	"""
	src = tmp_path / "main.drift"
	src.write_text(_NON_CONSUMING_THEN_CONSUMING_SOURCE)
	out = tmp_path / "repro_non_consume"
	compile_res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert compile_res.returncode == 0, (
		f"compile failed (rc={compile_res.returncode}):\n"
		f"stderr: {compile_res.stderr[:1000]}"
	)
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=10)
	assert run.returncode == 15, (
		f"non-consuming-then-consuming MOVE-capture read returned "
		f"{run.returncode}, expected 15 (7 + 8).  Implicit-move "
		f"zero-back is firing on the first non-consuming read; "
		f"the consume sees a zeroed env slot.\n"
		f"stderr: {run.stderr[-400:]}"
	)
	valgrind = shutil.which("valgrind")
	if valgrind is None:
		return
	vg = subprocess.run(
		[valgrind, "--tool=memcheck", "--error-exitcode=97",
		 "--leak-check=full", "--errors-for-leak-kinds=all",
		 str(out)],
		capture_output=True, text=True, timeout=60,
	)
	# Valgrind passes the binary's exit code through on clean runs;
	# rc=97 is the error-exitcode escalation.  Binary's own success
	# code is 15 (the n+m result) which propagates here.
	assert vg.returncode != 97, (
		"non-consuming-then-consuming MOVE-capture read triggered "
		"valgrind errors — the env slot is being mishandled "
		"between the two reads.\n"
		f"valgrind output (last 1500 chars):\n{vg.stderr[-1500:]}"
	)
	assert "ERROR SUMMARY: 0 errors" in vg.stderr, (
		f"valgrind error tally non-zero.\n"
		f"valgrind output (last 1000 chars):\n{vg.stderr[-1000:]}"
	)


def test_vt_capture_implicit_move_no_atexit_uaf(tmp_path: Path) -> None:
	"""Compile the minimal Arc-in-registry / VT-captures-clone /
	closure-consumes-via-implicit-move repro and run it under
	valgrind.  Assert: compile rc=0, binary rc=0, valgrind
	error-count = 0.
	"""
	valgrind = shutil.which("valgrind")
	if valgrind is None:
		import pytest
		pytest.skip("valgrind not installed; cannot pin atexit UAF")

	rc, stderr, out = _compile(tmp_path)
	assert rc == 0, f"compile failed: rc={rc}\nstderr: {stderr[:800]}"
	assert out.exists()

	res = subprocess.run(
		[valgrind, "--tool=memcheck", "--error-exitcode=97",
		 "--leak-check=full", "--errors-for-leak-kinds=all",
		 str(out)],
		capture_output=True, text=True, timeout=60,
	)
	# Bare-program return code is in res.returncode; valgrind
	# substitutes 97 if it found errors.
	if res.returncode == 97:
		raise AssertionError(
			"valgrind reported errors — VT capture implicit-move "
			"atexit UAF still present.\n"
			f"valgrind output (last 2000 chars):\n{res.stderr[-2000:]}"
		)
	assert res.returncode == 0, (
		f"binary returned {res.returncode}, expected 0 (success).\n"
		f"valgrind stderr (last 1000 chars):\n{res.stderr[-1000:]}"
	)
	# Defensive: the `--error-exitcode` flag already turns errors
	# into rc=97, but pin the "ERROR SUMMARY: 0 errors" line in
	# case valgrind ever drops the exit-code escalation.
	assert "ERROR SUMMARY: 0 errors" in res.stderr, (
		"valgrind ran clean exit but error-summary tally is "
		f"non-zero.\nvalgrind stderr (last 1000 chars):\n"
		f"{res.stderr[-1000:]}"
	)
