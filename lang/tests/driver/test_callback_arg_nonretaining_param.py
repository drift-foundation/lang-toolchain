# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Boxed callback with ref-valued captures passed as a call ARGUMENT to a
proven non-retaining parameter (DriftQuery regression, 2026-07-08).

0.33.74's use-aware escape scan (`lambda_validate.py::
_check_boxed_capture_escapes`) treated EVERY call-argument position as
escaping, rejecting the pervasive higher-order resource pattern:

    fn with_handle<R>(h: &Handle, var body: core.Callback1<&Handle, R>) -> R {
        return body.call(h);   // called once, synchronously, never stored
    }
    with_handle(h, core.callback1(|hh| captures(copy h, ...) => ...))

certified 0.33.72 accepted this (it is sound: the callback never outlives
`use_it`'s frame — `with_handle` only `.call()`s it). The compiler already
proves that: `analyze_non_retaining_params()` runs before
`validate_lambdas_non_retaining()` and marks `body` param_escape_level=LOCAL
(only ever used as a direct-call receiver, never stored/returned/forwarded
to an unproven param). The fix teaches the escape scan to exempt wrap
arguments (and let-bound wrap uses) at parameters PROVEN LOCAL/IMMEDIATE by
that existing signature metadata — resolved via signatures_by_id +
call_resolutions, no one-off callee body scan.

Negatives pin that the proof is load-bearing: a helper that STORES, RETURNS,
or FORWARDS-to-unproven keeps rejecting, as do the original returned/stored
shapes (the 0.33.74 nested-captures file).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lang.codegen.llvm.test_utils import sanitizer_timeout

ROOT = Path(__file__).resolve().parents[3]

# The DriftQuery report's 18-line repro, verbatim shape: generic helper,
# callback param only ever `.call()`ed.
_WITH_HANDLE_LOCAL_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub fn with_handle<R>(h: &Handle, var body: core.Callback1<&Handle, R>) nothrow -> R {
\treturn body.call(h);
}

fn use_it(h: &Handle, extra: Int) nothrow -> Int {
\treturn with_handle(h, core.callback1(| hh: &Handle | captures(copy h, move extra) => {
\t\treturn hh.tag + extra;
\t}));
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\tval r = use_it(h, 1);
\tif r == 42 { return 0; }
\treturn 1;
}
"""

# Same call shape, but the helper RETAINS: stores the callback into a struct
# field that outlives the call. The non-retaining analysis must NOT prove the
# param LOCAL, so the wrap argument stays rejected.
_RETAINING_HELPER_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub struct Stash {
\tcb: core.Callback1<&Handle, Int>,
}

pub fn keep_it(var body: core.Callback1<&Handle, Int>) nothrow -> Stash {
\treturn Stash(cb = move body);
}

fn use_it(h: &Handle) nothrow -> Int {
\tval s = keep_it(core.callback1(| hh: &Handle | captures(copy h) => {
\t\treturn hh.tag;
\t}));
\tval hh = Handle(tag = 7);
\treturn s.cb.call(hh);
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\treturn use_it(h);
}
"""

# Forwarding chain: outer helper forwards its callback param to a RETAINING
# helper. The forward edge resolves to an unproven param, so the outer param
# must not be proven LOCAL either — wrap argument stays rejected.
_FORWARD_TO_RETAINING_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub struct Stash {
\tcb: core.Callback1<&Handle, Int>,
}

pub fn keep_it(var body: core.Callback1<&Handle, Int>) nothrow -> Stash {
\treturn Stash(cb = move body);
}

pub fn looks_innocent(var body: core.Callback1<&Handle, Int>) nothrow -> Stash {
\treturn keep_it(move body);
}

fn use_it(h: &Handle) nothrow -> Int {
\tval s = looks_innocent(core.callback1(| hh: &Handle | captures(copy h) => {
\t\treturn hh.tag;
\t}));
\tval hh = Handle(tag = 7);
\treturn s.cb.call(hh);
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\treturn use_it(h);
}
"""

# Forwarding chain that stays clean: outer helper forwards to the LOCAL
# with_handle helper. Both params prove non-retaining; the wrap argument
# compiles and runs.
_FORWARD_TO_LOCAL_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub fn with_handle<R>(h: &Handle, var body: core.Callback1<&Handle, R>) nothrow -> R {
\treturn body.call(h);
}

pub fn delegate<R>(h: &Handle, var body: core.Callback1<&Handle, R>) nothrow -> R {
\treturn with_handle<type R>(h, move body);
}

fn use_it(h: &Handle, extra: Int) nothrow -> Int {
\treturn delegate<type Int>(h, core.callback1(| hh: &Handle | captures(copy h, move extra) => {
\t\treturn hh.tag + extra;
\t}));
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\tif use_it(h, 1) == 42 { return 0; }
\treturn 1;
}
"""

# Let-bound wrap whose only non-receiver use is the proven-LOCAL argument
# position — the DriftQuery call sites bind first in some places.
_LET_BOUND_LOCAL_ARG_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub fn with_handle<R>(h: &Handle, var body: core.Callback1<&Handle, R>) nothrow -> R {
\treturn body.call(h);
}

fn use_it(h: &Handle, extra: Int) nothrow -> Int {
\tval cb = core.callback1(| hh: &Handle | captures(copy h, move extra) => {
\t\treturn hh.tag + extra;
\t});
\treturn with_handle(h, move cb);
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\tif use_it(h, 1) == 42 { return 0; }
\treturn 1;
}
"""


def _compile(tmp_path: Path, source: str, name: str = "test_bin") -> subprocess.CompletedProcess[str]:
	src = tmp_path / "main.drift"
	src.write_text(source)
	out = tmp_path / name
	return subprocess.run(
		[sys.executable, "-m", "lang.driftc.driftc", "--dev",
		 "--stdlib-root", str(ROOT / "stdlib"),
		 str(src), "--entry", "main::main", "-o", str(out)],
		cwd=ROOT, capture_output=True, text=True, timeout=sanitizer_timeout(240),
	)


def _run_ok(tmp_path: Path, source: str) -> None:
	res = _compile(tmp_path, source)
	assert res.returncode == 0, f"compile failed:\n{res.stderr[-1500:]}"
	out = tmp_path / "test_bin"
	assert out.exists()
	run = subprocess.run([str(out)], capture_output=True, text=True, timeout=sanitizer_timeout(10))
	assert run.returncode == 0, f"expected exit 0, got {run.returncode}; stderr:\n{run.stderr[-500:]}"


def test_callback_arg_to_proven_local_param_compiles_and_runs(tmp_path: Path) -> None:
	"""The DriftQuery repro: wrap argument at a proven-LOCAL param is sound
	and must compile (accepted on certified 0.33.72; regressed in 0.33.74)."""
	_run_ok(tmp_path, _WITH_HANDLE_LOCAL_SOURCE)


def test_callback_arg_forwarded_to_local_param_compiles_and_runs(tmp_path: Path) -> None:
	"""A forward chain of proven non-retaining params is still proven."""
	_run_ok(tmp_path, _FORWARD_TO_LOCAL_SOURCE)


def test_let_bound_callback_arg_to_proven_local_param_compiles_and_runs(tmp_path: Path) -> None:
	"""Let-bound wrap whose only non-receiver use is a proven-LOCAL argument."""
	_run_ok(tmp_path, _LET_BOUND_LOCAL_ARG_SOURCE)


def test_callback_arg_to_retaining_param_still_rejected(tmp_path: Path) -> None:
	"""The helper stores the callback → param not proven → still rejected."""
	res = _compile(tmp_path, _RETAINING_HELPER_SOURCE)
	assert res.returncode != 0, "wrap arg at a retaining param must stay rejected"
	assert "E_ESCAPE_REF_CAPTURE" in (res.stderr + res.stdout), res.stderr[-1200:]


def test_callback_arg_forwarded_to_retaining_param_still_rejected(tmp_path: Path) -> None:
	"""Forwarding to an unproven/retaining param poisons the chain → rejected."""
	res = _compile(tmp_path, _FORWARD_TO_RETAINING_SOURCE)
	assert res.returncode != 0, "wrap arg forwarded to a retaining param must stay rejected"
	assert "E_ESCAPE_REF_CAPTURE" in (res.stderr + res.stdout), res.stderr[-1200:]


# The same sound pattern ONE NESTING LEVEL DEEPER (review finding on the
# 0.33.76 exemption): the outer callback is invoked in place; the inner
# callback is passed to with_handle, whose param is proven LOCAL. The inner
# wrap is only visible to the hidden-lambda revalidation pass, which must
# reach the same non-retaining proofs as the top-level pass.
_NESTED_LOCAL_ARG_SOURCE = """\
module main;

import std.core as core;

pub struct Handle { pub tag: Int }

pub fn with_handle<R>(h: &Handle, var body: core.Callback1<&Handle, R>) nothrow -> R {
\treturn body.call(h);
}

fn use_it(h: &Handle, extra: Int) nothrow -> Int {
\treturn core.callback0(| | captures(copy h, move extra) => {
\t\treturn with_handle(h, core.callback1(| hh: &Handle | captures(copy h, move extra) => {
\t\t\treturn hh.tag + extra;
\t\t}));
\t}).call();
}

pub fn main() nothrow -> Int {
\tval h = Handle(tag = 41);
\tif use_it(h, 1) == 42 { return 0; }
\treturn 1;
}
"""


def test_nested_callback_arg_to_proven_local_param_compiles_and_runs(tmp_path: Path) -> None:
	"""Inner wrap at a proven-LOCAL param inside an in-place-invoked outer
	callback must compile — the hidden-lambda revalidation pass needs the
	same proof access as the user-fn pass."""
	_run_ok(tmp_path, _NESTED_LOCAL_ARG_SOURCE)
