# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG regression: VT-spawned closure that consumes a
non-Copy move-captured local via an EXPLICIT `move <cap>` must emit
an env-slot zero-back so the callback's drop thunk no-ops on that
field at exit.  Without the zero-back, the callee's by-value param
drops the captured value once at end-of-callee scope and the
callback drop thunk drops it AGAIN from the still-live env bit
pattern; the second drop hits refcount 0 and frees an allocation
that other Arc clones (e.g. the global-registry clone) still
expect to share.  Surfaces as an atexit use-after-free.

Reported by the PushCoin bookkeeper team against 0.33.5 in
`~/src/pushcoin/work/drift-vt-drop-atexit-use-after-free.md`.
Valgrind shape:
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

**Language contract (made explicit in 0.33.6).**  Drift requires
explicit ownership transfer at by-value call args: `f(x)` MUST NOT
silently consume `x`.  Users MUST write `f(move x)`.  The
0.31.70 MIR validator (`validate_mir_call_byvalue_moves`) is the
gate for function-frame locals — bare non-Copy HVar at a
statement-form by-value call arg fires the friendly
`cannot copy 'x': type 'T' is not Copy (use move x)` diagnostic.
Pinned by `test_use_move_call_arg_friendly_diag.py`.

This regression test exercises ONLY the explicit-`move` form
(`consume_gw(move gw_for_worker)`).  That path routes through
`_visit_expr_HMove` in `hir_to_mir.py`, which emits
`LoadRef → tmp-local → MoveOut → StoreRef(zero)` on the env slot.
Without the zero-back, the callback drop thunk's per-field drop
loads the original live Arc bit pattern and re-drops it — the
UAF.

The fix surface in 0.33.6 is `_visit_expr_HMove`'s pre-existing
env-slot zero-back logic; this test pins that it stays active and
that the bookkeeper UAF shape stays closed against explicit
`move` source.

Bare-HVar implicit-move at by-value call args remains an
EXPLICIT LANGUAGE ERROR (per the contract above) — the bookkeeper
team's original source `_worker_body(..., gw, ...)` was invalid
Drift; the correct form is `_worker_body(..., move gw, ...)`.

**ABI implication.**  Codegen-internal — no runtime ABI surface
changes, no `.zdmp` schema change, no signing path change.  Per
`docs/design/drift-lang-abi.md` this lands without an ABI bump;
existing ABI-14 dependency artifacts remain consumable unchanged.
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
\t\t// EXPLICIT `move` — the only valid form per Drift's
\t\t// explicit-ownership-transfer contract.  Routes through
\t\t// `_visit_expr_HMove` which emits the env-slot zero-back
\t\t// so the callback drop thunk no-ops on this field at
\t\t// exit.  Without that zero-back, the callee's
\t\t// by-value param drop + callback drop thunk double-drop
\t\t// the captured Arc → atexit UAF against the
\t\t// global-registry clone.
\t\tconsume_gw(move gw_for_worker);
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


def test_vt_capture_explicit_move_no_atexit_uaf(tmp_path: Path) -> None:
	"""`Arc<Gateway>` stored in the global registry; cloned into a
	`conc.spawn` move-captured local; consumed inside the closure
	via explicit `move gw_for_worker` at a by-value call arg.
	Asserts compile rc=0, binary rc=0, valgrind
	`ERROR SUMMARY: 0 errors`.

	Pre-fix the env-slot zero-back was absent and valgrind
	reported the bookkeeper UAF (three errors, one context).
	Post-fix `_visit_expr_HMove`'s capture-aware path emits the
	zero-back and valgrind is clean.

	Skips when valgrind is unavailable.
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
	if res.returncode == 97:
		raise AssertionError(
			"valgrind reported errors — VT capture explicit-move "
			"atexit UAF reproduces (env-slot zero-back is missing "
			"in `_visit_expr_HMove`'s callback-capture path).\n"
			f"valgrind output (last 2000 chars):\n{res.stderr[-2000:]}"
		)
	assert res.returncode == 0, (
		f"binary returned {res.returncode}, expected 0 (success).\n"
		f"valgrind stderr (last 1000 chars):\n{res.stderr[-1000:]}"
	)
	assert "ERROR SUMMARY: 0 errors" in res.stderr, (
		"valgrind ran clean exit but error-summary tally is "
		f"non-zero.\nvalgrind stderr (last 1000 chars):\n"
		f"{res.stderr[-1000:]}"
	)


_BARE_HVAR_REJECT_SOURCE = """\
module main;

import std.core as core;
import std.core.arc as arc;
import std.concurrent as conc;
import std.runtime as rt;

pub struct Gateway {
\tpub name: String
}

fn consume_gw(gw: arc.Arc<Gateway>) nothrow -> Void {
\tval _ = gw;
\treturn;
}

fn _run() -> Int {
\tval gw = arc.arc(Gateway(name = "test"));
\tvar vt = conc.spawn(| | captures(move gw) nothrow => {
\t\t// BARE HVar at by-value call arg — Drift's explicit-
\t\t// ownership-transfer contract REJECTS this.  Compiler
\t\t// must emit the friendly `cannot copy ... use move`
\t\t// diagnostic.  Pinned to prevent a future regression
\t\t// where implicit move at call args is silently accepted
\t\t// for callback captures (the 0.33.6-first-iteration shape
\t\t// the user explicitly rejected — `f(x)` must not silently
\t\t// consume `x`).
\t\tconsume_gw(gw);
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


def test_bare_hvar_at_callback_capture_call_arg_is_rejected(tmp_path: Path) -> None:
	"""Negative regression pinning the language contract: even
	for callback captures, a bare HVar at a by-value call arg is
	an EXPLICIT LANGUAGE ERROR.  Users must write
	`f(move <cap>)`, never `f(<cap>)`.

	Asserts the compiler emits the friendly `cannot copy ...`
	`use move <name>` diagnostic — same shape as for
	function-frame locals (pinned by
	`test_use_move_call_arg_friendly_diag.py`).  No silent
	implicit move at codegen.
	"""
	src = tmp_path / "main.drift"
	src.write_text(_BARE_HVAR_REJECT_SOURCE)
	out = tmp_path / "repro_bare"
	res = subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=120,
	)
	assert res.returncode != 0, (
		"bare HVar at callback-capture by-value call arg was "
		"accepted — this VIOLATES Drift's explicit-ownership-"
		"transfer contract.  `f(x)` must not silently consume "
		"a non-Copy `x`.\n"
		f"stderr: {res.stderr[:800]}"
	)
	# The friendly diag's exact phrasing comes from
	# `type_checker.py:3161` / the MIR validator's friendly
	# format.  We just pin that SOME `use move`-shaped error
	# fires and rejects the program.
	assert "use move" in res.stderr or "cannot copy" in res.stderr, (
		"compile failed but without the expected friendly diag.\n"
		f"stderr: {res.stderr[:800]}"
	)
