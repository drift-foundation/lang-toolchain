# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""LANGUAGE_BUG carrier: bare-untyped lambda passed to a concrete
`CallbackN` parameter must get its parameter types inferred from the
interface's instantiated type-args.

Surfaced 2026-04-29 by the bookkeeper app team against driftc 0.31.28 /
web-rest 0.4.0 on the middleware shape:

	rest.add_middleware(&mut web_app, |req, ctx, next| captures(share app) => {
		val req_tag = req.method.clone() + " " + req.path.clone();
		...
		val result = next.call(req, ctx);
		...
	});

with `add_middleware` declared as

	pub fn add_middleware(a: &mut App,
	    mw: core.Callback3<&Request, &mut Context, NextFn,
	                       core.Result<Response, RestError>>)
	    nothrow -> Void;

The lambda `|req, ctx, next|` (no annotations) reported

	error: field access requires a struct value
	error: no matching method 'clone' for receiver Ref<Unknown>
	error: no matching method 'call' for receiver Unknown
	error: lambda can throw but is expected to be nothrow for
	    Fn(Ref<...Request<...>>, RefMut<...Context<...>>,
	       std.core.Callback2) nothrow -> Result<...>
	error: callback3 expects a function value
	error: no matching overload for function 'add_middleware'
	    with args [1632, 3]

i.e. all three lambda parameters resolved to `Unknown` / `Ref<Unknown>`
inside the body — the lambda's expected param types were not propagated
from the concrete `Callback3` parameter.  Same-shape arity-2
`add_throws_route(..., |req, _ctx| ...)` calls (no annotations) compile
fine in the same file, so the gap is specific to arity ≥ 3 in the
candidate-driven pre-typing path.

Expected fix area: `lang/driftc/checker/call_resolver.py` ~lines
5883-5969 — "Candidate-driven concretization of bare HLambda args".
The path uses `_callback_param_kind` against
`_pc.signature.param_types[idx]` from
`callable_registry.get_free_candidates_unscoped(name=...)`.  Per-arity
behavior must match arity 2 — the surface user-facing behavior is the
load-bearing pin here, not the implementation detail.

Pins:

  P1. Arity-2 control: bare untyped `|a, b|` against
      `Callback2<Req, Req, Int>` — must compile, body uses field
      access on `a` / `b`.  Today this works (web-rest's
      `add_throws_route` callers prove it).
  P2. Arity-3 carrier: bare untyped `|a, b, c|` against
      `Callback3<Req, Req, Req, Int>` — must compile.  Pre-fix
      this fails because the lambda's three params resolve to
      `Unknown`, breaking field access in the body.
  P3. Arity-3 with nested-Callback ret-position type (the actual
      web-rest middleware shape): the third param is itself a
      `Callback2<...>`, the return is `Result<...>`.  Pins that
      param-type propagation works when the iface's own type-args
      include other Callback types (no `_callback_param_kind`
      false-negative on nested concrete ifaces).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_with_stdlib(
	tmp_path: Path,
	capsys: pytest.CaptureFixture[str],
	source: str,
) -> tuple[int, dict]:
	main_path = tmp_path / "main.drift"
	_write_file(main_path, source)
	argv = ["--stdlib-root", "stdlib", "--test-build-only", str(main_path)]
	return _run_driftc_json(argv, capsys)


# P1 — arity-2 control. Bare untyped `|a, b|`; body uses `a.n` / `b.n`
# field access. If lambda param inference works for arity 2, this
# compiles. (web-rest's `add_throws_route` pattern.)
_P1_SOURCE = """
module main;

import std.core as core;

struct Req { pub n: Int }

fn register2(slot: &mut Array<core.Callback2<Req, Req, Int>>,
             cb: core.Callback2<Req, Req, Int>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback2<Req, Req, Int>> = [];
    register2(slot, |a, b| => {
        return a.n + b.n;
    });
    return 0;
}
"""


# P2 — arity-3 carrier. Bare untyped `|a, b, c|`. Same body shape as P1
# extended to three params. Pre-fix this fails with "field access
# requires a struct value" because `a` / `b` / `c` are Unknown.
_P2_SOURCE = """
module main;

import std.core as core;

struct Req { pub n: Int }

fn register3(slot: &mut Array<core.Callback3<Req, Req, Req, Int>>,
             cb: core.Callback3<Req, Req, Req, Int>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback3<Req, Req, Req, Int>> = [];
    register3(slot, |a, b, c| => {
        return a.n + b.n + c.n;
    });
    return 0;
}
"""


# P3 — arity-3 with a nested-Callback parameter. Mirrors the web-rest
# middleware shape: third param is itself a `Callback2<...>`. Body
# invokes `next.call(...)` so a `next: Unknown` regression breaks here
# even if param-1/2 inference were partially working.
_P3_SOURCE = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register_mw(slot: &mut Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>>,
               cb: core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> = [];
    register_mw(slot, |a, b, next| => {
        val inner = next.call(a, b);
        return core.Result::Ok(Resp(status = a.n + b.k));
    });
    return 0;
}
"""


# P4 — exact web-rest middleware shape: ref types in iface args.
# `Callback3<&Req, &mut Ctx, Callback2<&Req, &mut Ctx, Result<...>>, Result<...>>`.
# Closer to the user's failing case at app.drift:38-50.
_P4_SOURCE = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register_mw_ref(slot: &mut Array<core.Callback3<&Req, &mut Ctx, core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>>,
                   cb: core.Callback3<&Req, &mut Ctx, core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback3<&Req, &mut Ctx, core.Callback2<&Req, &mut Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> = [];
    register_mw_ref(slot, |req, ctx, next| => {
        val inner = next.call(req, ctx);
        return core.Result::Ok(Resp(status = req.n + ctx.k));
    });
    return 0;
}
"""


def test_p1_callback2_bare_untyped_lambda_param_inference(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P1 — control. Bare untyped `|a, b|` against
	`Callback2<Req, Req, Int>`; body accesses `a.n` / `b.n`. If this
	regresses too, the bug is broader than arity-3."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _P1_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"arity-2 bare untyped lambda param inference must work; got "
		f"rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_p2_callback3_bare_untyped_lambda_param_inference(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P2 — carrier. Bare untyped `|a, b, c|` against
	`Callback3<Req, Req, Req, Int>`; body accesses fields on each
	param. Pre-fix: `a` / `b` / `c` resolve to Unknown, body fails to
	type-check."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _P2_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"arity-3 bare untyped lambda param inference must work — "
		f"all three params must resolve from Callback3 type-args; got "
		f"rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_p3_callback3_bare_untyped_with_nested_callback_param(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P3 — Callback3 with nested Callback2 param. Third param is
	itself a `Callback2<...>`; body calls `next.call(...)` and
	constructs `Result::Ok(Resp(...))` on return. Pre-fix this fails
	additionally with `no matching method 'call' for receiver Unknown`.
	Pins that param-type propagation handles a nested concrete iface
	in the parent iface's type-args."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _P3_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"Callback3 with nested Callback2 param must propagate all "
		f"three param types into the bare lambda body; got "
		f"rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_p4_callback3_bare_untyped_ref_args_exact_middleware_shape(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P4 — exact web-rest middleware shape with ref types in iface
	args: `Callback3<&Req, &mut Ctx, Callback2<&Req, &mut Ctx,
	Result<Resp, AppErr>>, Result<Resp, AppErr>>`. The user's failing
	site at app.drift:38-50 uses this exact shape (`&Request`,
	`&mut Context`, `next: Callback2<...>`, `Result<Response,
	RestError>` return). Body uses `req.n` (auto-deref field access
	on `&Req`) and calls `next.call(req, ctx)`. Pre-fix: req/ctx/next
	all resolve to Unknown / Ref<Unknown>."""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, _P4_SOURCE)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"Callback3 with ref-typed iface args must propagate all "
		f"three param types into the bare lambda body; got "
		f"rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_p5_cross_module_callback3_bare_untyped(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P5 — cross-module shape. `add_middleware`-equivalent and the
	types live in module `mw`; caller is in `app`. The user's actual
	failing case is cross-package (web-rest is consumed as a separate
	package), and cross-module is the closest in-tree analogue. Pre-
	fix-hypothesis: candidate signature loaded from a different
	module's callable_registry entry has a Callback3 param whose
	`get_interface_schema().name` is missing or differently spelled,
	breaking `_callback_param_kind` lookup."""
	mw_src = tmp_path / "mw.drift"
	app_src = tmp_path / "app.drift"
	mw_src.write_text("""
module mw;

import std.core as core;

pub struct Req { pub n: Int }
pub struct Ctx { pub k: Int }
pub struct Resp { pub status: Int }
pub struct AppErr { pub code: Int }

pub struct App { pub slot: Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> }

pub fn new_app() nothrow -> App {
    var s: Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> = [];
    return App(slot = move s);
}

pub fn ok(s: Int) nothrow -> core.Result<Resp, AppErr> {
    return core.Result::Ok(Resp(status = s));
}

pub fn add_middleware(a: &mut App,
    mw: core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>) nothrow -> Void {
    a.slot.push(move mw);
    return core.void_value();
}
""")
	app_src.write_text("""
module app;

import mw;

pub fn main() nothrow -> Int {
    var a = mw.new_app();
    mw.add_middleware(a, |req, ctx, next| => {
        val inner = next.call(req, ctx);
        return mw.ok(req.n + ctx.k);
    });
    return 0;
}
""")
	argv = [
		"--stdlib-root", "stdlib",
		"--test-build-only",
		str(mw_src), str(app_src),
	]
	rc, payload = _run_driftc_json(argv, capsys)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"cross-module Callback3 bare lambda must propagate types; "
		f"got rc={rc} diagnostics={[d.get('message') for d in errors]}"
	)


def test_p7_throws_mismatch_does_not_cascade_unknown_param_diagnostics(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P7 — LANGUAGE_BUG carrier (2026-04-29). When a bare untyped
	lambda's body legitimately throws (e.g. user has an `or_throw()`
	call) but the expected `Callback3<...>` is nothrow, the throws
	mismatch is a real user-facing error. The diagnostic must report
	*only* that throws mismatch — it must NOT also flood the user with
	cascading 'field access requires a struct value' / 'Ref<Unknown>' /
	'no matching method ... receiver Unknown' errors that look like
	param-inference failure.

	The cascade comes from a retry at `call_resolver.py` ~line 5982
	(`if ty == ctx.unknown_ty: arg_types[idx] = type_expr(arg)`) that
	re-types the lambda with `expected_type=None` after the first pass
	(with the candidate-driven Callback3 expected_type) returned Unknown
	from the throws-mismatch path. The retry-without-expected rebinds
	all params to Unknown and the body re-type-checks against Unknown
	receivers, generating the spurious 'param inference broke' look.

	The user's bookkeeper report on web-rest 0.4.0 / driftc 0.31.28
	(2026-04-29) saw the cascade and concluded Callback3 param
	inference was broken; it was actually a clean throws mismatch in
	the lambda body buried under 5+ unrelated cascade errors.

	Pin: the diagnostic stream must NOT contain
	'field access requires a struct value' or 'receiver Unknown' or
	'Ref<Unknown>' for a lambda whose first-pass expected_type was
	correctly inferred. The throws diagnostic itself is allowed."""
	source = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn maybe_resp(n: Int) -> core.Result<Resp, AppErr> {
    return core.Result::Ok(Resp(status = n));
}

fn register_mw(slot: &mut Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>>,
               cb: core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> = [];
    // `maybe_resp(req.n)` returns `Result<Resp, AppErr>`; binding via
    // `val r = ...` (no annotation) triggers eager auto-unwrap which
    // injects `or_throw()`. The lambda body becomes can-throw, but
    // the Callback3 expected type is nothrow → throws mismatch.
    // Pre-fix: cascade with Ref<Unknown> / Unknown receivers; post-
    // fix: a single, clean throws diagnostic.
    register_mw(slot, |req, ctx, next| => {
        val r = maybe_resp(req.n);
        return next.call(req, ctx);
    });
    return 0;
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	all_msgs = [d.get("message", "") for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	cascade_markers = [
		"field access requires a struct value",
		"receiver Unknown",
		"Ref<Unknown>",
		"receiver Ref<Unknown>",
	]
	cascade_hits = [m for m in all_msgs if any(mk in m for mk in cascade_markers)]
	assert not cascade_hits, (
		f"throws-mismatch in a bare lambda must NOT cascade through "
		f"Unknown-receiver diagnostics — that's a separate compiler "
		f"bug (cascading retry-without-expected at "
		f"call_resolver.py:5982). Found cascade: {cascade_hits}"
	)


def test_p6_callback3_with_explicit_captures_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""P6 — `captures(copy x)` clause on the bare lambda. The user's
	failing site uses `captures(share app) => { ... }`; this test
	uses the simpler `copy` form on an `Int` to isolate the captures-
	clause-changes-pre-typing-flow hypothesis from any Arc-API
	noise. Pin that the explicit-captures form does not disrupt
	candidate-driven param-type propagation for arity 3."""
	source = """
module main;

import std.core as core;

struct Req { pub n: Int }
struct Ctx { pub k: Int }
struct Resp { pub status: Int }
struct AppErr { pub code: Int }

fn register_mw(slot: &mut Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>>,
               cb: core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>) nothrow -> Void {
    slot.push(move cb);
    return core.void_value();
}

pub fn main() nothrow -> Int {
    var slot: Array<core.Callback3<Req, Ctx, core.Callback2<Req, Ctx, core.Result<Resp, AppErr>>, core.Result<Resp, AppErr>>> = [];
    val bias = 7;
    register_mw(slot, |req, ctx, next| captures(copy bias) => {
        val inner = next.call(req, ctx);
        return core.Result::Ok(Resp(status = req.n + ctx.k + bias));
    });
    return 0;
}
"""
	rc, payload = _compile_with_stdlib(tmp_path, capsys, source)
	errors = [d for d in payload.get("diagnostics", []) if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"Callback3 bare lambda with `captures(copy x)` must "
		f"propagate param types; got rc={rc} "
		f"diagnostics={[d.get('message') for d in errors]}"
	)
