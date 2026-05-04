# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Patch B regression surface for implicit `core.callback{N}` /
`core.callback_throw{N}` wrapping at call shapes that currently require
explicit boxing.

Contract: when a parameter / field / let initializer / return slot is
statically typed as Callback0/1/2 or CallbackThrow0/1/2, a bare
capturing lambda must be implicitly wrapped — same shape as the explicit
`core.callbackN(...)` form, recognised by the borrow checker by
module/name (not by `_is_implicit_wrap`).
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_to_llvm_ir_for_tests
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _compile(tmp_path: Path, source: str, *, entry: str = "m::main"):
	src = tmp_path / "main.drift"
	src.write_text(source)
	modules, type_table, exception_catalog, module_exports, module_deps, parse_diags = parse_drift_workspace_to_hir(
		[src],
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert parse_diags == [], parse_diags
	func_hirs, signatures, _ = flatten_modules(modules)
	_ir, checked = compile_to_llvm_ir_for_tests(
		func_hirs=func_hirs,
		signatures=signatures,
		exc_env=exception_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		enforce_entrypoint=True,
		entry=entry,
	)
	return checked


# ── Site 1: associated / static call (Type::method) — NOT WRAPPED ────
#
# IMPORTANT: these tests document the *current* behavior of `Type::method`
# dispatch with `Callback*`-typed params. The two tests below pass on
# Patch A (no wrap helper), and they continue to pass on Patch B because
# Patch B did NOT touch this path.
#
# The reason `S::take_cb(|x| => …)` compiles clean today is NOT that an
# implicit wrap fires — `_implicit_callback_wrap` is never called for
# this shape. The lambda flows through `_args_match_params` /
# `_coerce_args_for_params` in `type_checker.py` (~line 2144), which
# silently retypes a non-INTERFACE arg to the INTERFACE param without
# inserting a wrap node. Downstream codegen handles the lambda through
# whatever path it does for direct fn-typed values into iface slots.
#
# Pre-existing leniency surfaced by these tests: arity-1 lambda fed to
# a `Callback2`-typed associated-function param ALSO compiles without
# diagnostic on this same path (verified empirically — the same is true
# for free-fn calls). That is a separate issue from implicit-wrap, and
# was explicitly out of Patch B scope.
#
# Bottom line: keeping these tests as a behavior pin for the silent
# coercion path; they do not exercise the new wrap helper.


def test_site1_static_assoc_fn_bare_lambda_to_callback1(tmp_path: Path) -> None:
	"""`S::take_cb(|x| => x + 1)` compiles clean today via the silent
	INTERFACE-coercion path in `_args_match_params`, NOT via the wrap
	helper. This test pins that path's behavior; it does not assert
	anything about Patch B's wrap insertion."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct S {}

implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(41);
	}
}

fn main() nothrow -> Int {
	return S::take_cb(|x: Int| nothrow => x + 1);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


# NOTE: arity-mismatch through Type::method does NOT error today. The
# same is true through a free function (Site E path), so this is a
# pre-existing leniency in `_args_match_params` /
# `_coerce_args_for_params` (type_checker.py:2144-2147 replaces the arg
# type with the param type when param is INTERFACE and arg is not).
# Out of Patch B scope; tracked as separate.


# ── Site 2: struct ctor field init ────────────────────────────────────


def test_site2_struct_ctor_named_bare_lambda_to_callback1(tmp_path: Path) -> None:
	"""`Holder(cb = |x| => ...)` where field `cb: Callback1<Int, Int>`.
	Today this lands in `resolve_struct_ctor` iface_coercion fallback
	which lowers via `M.ConstructIfaceValue` — broken for lambdas."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder {
	cb: core.Callback1<Int, Int>
}

fn main() nothrow -> Int {
	val h = Holder(cb = |x: Int| nothrow => x + 1);
	return h.cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site2_struct_ctor_positional_bare_lambda_to_callback1(tmp_path: Path) -> None:
	"""Same as above but positional ctor."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder {
	cb: core.Callback1<Int, Int>
}

fn main() nothrow -> Int {
	val h = Holder(|x: Int| nothrow => x + 1);
	return h.cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site2_struct_ctor_explicit_wrap_no_double_wrap(tmp_path: Path) -> None:
	"""Already-explicit `core.callback1(fn_ref)` must not be re-wrapped."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder {
	cb: core.Callback1<Int, Int>
}

fn add1(x: Int) nothrow -> Int { return x + 1; }

fn main() nothrow -> Int {
	val h = Holder(cb = core.callback1(add1));
	return h.cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site2_struct_ctor_capture_copy_lambda(tmp_path: Path) -> None:
	"""Capturing-copy lambda into a `Callback*` field — should pass and
	the wrap should be the same shape as the explicit form."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder {
	cb: core.Callback1<Int, Int>
}

fn main() nothrow -> Int {
	val bias = 7;
	val h = Holder(cb = |x: Int| captures(copy bias) nothrow => x + bias);
	return h.cb.call(34);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


# ── Site 5: typed `let` initializer ───────────────────────────────────


def test_site5_typed_let_bare_lambda_to_callback1(tmp_path: Path) -> None:
	"""`val cb: core.Callback1<Int, Int> = |x| => x + 1`. Today the let-init
	type-checker records iface_coercion which lowers via ConstructIfaceValue
	over the lambda — broken without the wrap."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn main() nothrow -> Int {
	val cb: core.Callback1<Int, Int> = |x: Int| nothrow => x + 1;
	return cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site5_typed_let_explicit_wrap_no_double(tmp_path: Path) -> None:
	"""Explicit `core.callback1(fn_ref)` initializer — no double wrap."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn add1(x: Int) nothrow -> Int { return x + 1; }

fn main() nothrow -> Int {
	val cb: core.Callback1<Int, Int> = core.callback1(add1);
	return cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


# ── Site 6: return position ───────────────────────────────────────────


def test_site6_return_bare_lambda_to_callback1(tmp_path: Path) -> None:
	"""`fn make_cb() -> Callback1<Int, Int> { return |x| => x + 1; }`
	— same iface_coercion fallback issue as let-init."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn make_cb() nothrow -> core.Callback1<Int, Int> {
	return |x: Int| nothrow => x + 1;
}

fn main() nothrow -> Int {
	val cb = make_cb();
	return cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site6_return_explicit_wrap_no_double(tmp_path: Path) -> None:
	"""Explicit `core.callback1(fn_ref)` returned — no double wrap."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn add1(x: Int) nothrow -> Int { return x + 1; }

fn make_cb() nothrow -> core.Callback1<Int, Int> {
	return core.callback1(add1);
}

fn main() nothrow -> Int {
	val cb = make_cb();
	return cb.call(41);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


# ── Site 4: trait method dispatch (UFCS) — BLOCKED, NOT PROBED ────────
#
# Probe attempted: declare `trait Runner { fn run(self: &Self,
# cb: core.Callback1<Int, Int>) nothrow -> Int }` and an
# `implement Runner for S { fn run(...) cb: core.Callback1<Int, Int>) ... }`
# with syntactically identical types. Trait-impl-signature validation
# reports
#   "trait impl method 'run' parameter 2 expects core.Callback1<Int, Int>
#    but got std.core.Callback1"
# i.e. the impl's `Callback1<Int, Int>` loses its type args during
# normalization while the trait's keeps them. This is a pre-existing
# trait-impl validation bug, separate from implicit-callback-wrap.
#
# Conclusion: Site 4 cannot be probed cleanly today. The instance-method
# dispatch shape (`recv.run(|x| => ...)`) is covered by Site D
# (inherent method primary, Patch A). UFCS trait dispatch with Callback*
# params is dropped from Patch B until the trait-impl-validation issue
# is addressed separately.





# ── Site 3: variant ctor — UNREACHABLE TODAY ──────────────────────────
#
# Generic interface types (e.g. `Callback1<...>`) cannot be declared as
# variant arm field types: `_lower_generic_expr` in call_resolver.py
# (~lines 985-1019) consults struct_bases / variant_schemas / aliases
# but does NOT consult interface_bases when resolving a generic type
# expression in variant-declaration context. Result: declaring
#   variant Holder { With(cb: core.Callback1<Int, Int>), ... }
# raises "unknown generic type 'Callback1'" at parse-time-after-resolve,
# before any ctor call exists.
#
# Non-generic interfaces (e.g. `interface MyIface { fn foo(self: &Self) ... }`)
# DO work as variant arm fields. So the limitation is generic-interfaces-only.
# The Callback*/CallbackThrow* family is uniformly generic, so no such
# variant arm exists today.
#
# Conclusion: there is no Patch B gap for variant ctor — the wrap site
# is unreachable. Site 3 is dropped from Patch B.


def test_site2_struct_ctor_borrowed_capture_rejected(tmp_path: Path) -> None:
	"""Borrowed capture into a `Callback*` struct field MUST be rejected
	with the same diagnostic the explicit `core.callbackN(...)` path
	emits — and ONLY that diagnostic, no cascade noise from the call
	resolver continuing past the rejected ctor (e.g. "keyword arguments
	are only supported for ctors", "no matching overload for function
	'Holder'")."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder { cb: core.Callback0<Int> }

fn main() nothrow -> Int {
	var x = 1;
	val h = Holder(cb = | | captures(&x) nothrow => x);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	messages = [d.message for d in errors]
	assert any("borrowed captures are non-escaping" in m for m in messages), (
		f"expected borrowed-capture rejection, got: {messages}"
	)
	# Cascade-noise guard: previously the rejected ctor returned None and
	# the outer caller fell through to free-fn / kwargs resolution, which
	# tacked on unrelated diagnostics. The _STRUCT_CTOR_ERRORED sentinel
	# fixed that; pin the absence of the noisy follow-ups.
	for noise in (
		"keyword arguments are only supported for constructors",
		"no matching overload for function 'Holder'",
	):
		assert not any(noise in m for m in messages), (
			f"unexpected cascade noise after struct-ctor rejection: {messages}"
		)


def test_site2_struct_ctor_borrowed_capture_positional_rejected(tmp_path: Path) -> None:
	"""Same as above but positional ctor — exercises the parallel
	REJECTED→sentinel path through the positional-args branch of
	`resolve_struct_ctor`."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder { cb: core.Callback0<Int> }

fn main() nothrow -> Int {
	var x = 1;
	val h = Holder(| | captures(&x) nothrow => x);
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	messages = [d.message for d in errors]
	assert any("borrowed captures are non-escaping" in m for m in messages), (
		f"expected borrowed-capture rejection, got: {messages}"
	)
	for noise in (
		"keyword arguments are only supported for constructors",
		"no matching overload for function 'Holder'",
	):
		assert not any(noise in m for m in messages), (
			f"unexpected cascade noise after struct-ctor rejection: {messages}"
		)


def test_site5_typed_let_borrowed_capture_rejected(tmp_path: Path) -> None:
	"""Borrowed capture into a typed-let `Callback*` slot MUST be rejected."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn main() nothrow -> Int {
	var x = 1;
	val cb: core.Callback0<Int> = | | captures(&x) nothrow => x;
	return 0;
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("borrowed captures are non-escaping" in d.message for d in errors), (
		f"expected borrowed-capture rejection, got: {[d.message for d in errors]}"
	)


def test_site6_return_borrowed_capture_rejected(tmp_path: Path) -> None:
	"""Borrowed capture in a return-position `Callback*` MUST be rejected."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn make_cb() nothrow -> core.Callback0<Int> {
	var y = 7;
	return | | captures(&y) nothrow => y;
}

fn main() nothrow -> Int { val _ = make_cb(); return 0; }
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert any("borrowed captures are non-escaping" in d.message for d in errors), (
		f"expected borrowed-capture rejection, got: {[d.message for d in errors]}"
	)


def test_site2_struct_ctor_throw_callback(tmp_path: Path) -> None:
	"""CallbackThrow1 field — lambda omits `nothrow` (defaults to may-throw)
	so the wrap picks `callback_throw1`."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Holder {
	cb: core.CallbackThrow1<Int, Int>
}

fn main() nothrow -> Int {
	val h = Holder(cb = |x: Int| => x + 1);
	return try h.cb.call(41) catch { 0 };
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


# ── Site 0: free-fn call with CallbackThrow* param ────────────────────
#
# Regression coverage for the 0.31.18 implicit-wrap throws-variant bug
# (LANGUAGE_BUG): when the parameter type was a concrete `CallbackThrow*`
# the fallback wrap path (`_wrap_explicit_capture_callbacks` in
# `call_resolver.py`) was hardcoding `is_throw=False`, so it built
# `core.callback{N}(throwing_lambda)` instead of
# `core.callback_throw{N}(throwing_lambda)`. The lambda body's
# throws-ness was never the dispatch authority — the parameter type is.
#
# Surfaced by pushcoin/bookkeeper @ 0.31.18 against
# `web.rest.add_throws_route(..., |req, ctx| captures(share app) => ...)`.
# Symptom: E-AUTO-fc123347 "lambda can throw but is expected to be
# nothrow for Fn(...) nothrow -> Unknown" + E-AUTO-d5fc8414 "callback2
# expects a function value".


def test_site0_free_fn_throws_param_bare_lambda(tmp_path: Path) -> None:
	"""Free-fn call with concrete `CallbackThrow2<...>` param + bare
	throwing lambda must wrap implicitly as `callback_throw2`, not
	`callback2`. This is the irreducible carrier for the 0.31.18
	throws-variant regression: no captures, no overload ambiguity, just
	a single concrete `CallbackThrow*` candidate at the lambda's
	parameter index."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

error Boom { message: String }
struct Req {}
struct Resp {}

fn handle(req: &Req, ctx: &mut Int) -> Resp {
	throw Boom(message = "x");
}

fn add_throws_route(handler: core.CallbackThrow2<&Req, &mut Int, Resp>) nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	return add_throws_route(|req, ctx| => {
		return handle(req, ctx);
	});
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], (
		f"bare-lambda + CallbackThrow2 param must implicitly wrap as "
		f"`callback_throw2`. Got errors: {[d.message for d in errors]}"
	)


def test_site0_free_fn_throws_param_share_capture(tmp_path: Path) -> None:
	"""App-shaped carrier: free-fn call with `CallbackThrow2<...>`
	param, lambda that does `captures(share arc)` and a throwing call
	on the captured value. Mirrors bookkeeper's `add_throws_route(...,
	|req, ctx| captures(share app) => app.get().handle(...))`. The
	throws-variant of fix #1 must compose with the share-capture
	pattern (fix #2)."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;
import std.concurrent as conc;

error Boom { message: String }
struct Req {}
struct Resp {}
struct App { tag: Int }

implement App {
	pub fn handle(self: &App, req: &Req, ctx: &mut Int) -> Resp {
		throw Boom(message = "x");
	}
}

fn add_throws_route(handler: core.CallbackThrow2<&Req, &mut Int, Resp>) nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	val app = conc.arc(App(tag = 7));
	return add_throws_route(|req, ctx| captures(share app) => {
		val a = app.get();
		return a.handle(req, ctx);
	});
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], (
		f"share-captured throwing lambda into CallbackThrow2 must wrap "
		f"as `callback_throw2`. Got errors: {[d.message for d in errors]}"
	)


def test_site0_free_fn_explicit_throw_wrap_no_double(tmp_path: Path) -> None:
	"""Explicit `core.callback_throw2(...)` argument must NOT be
	double-wrapped by either the pre-resolution scan, the
	post-resolution loop, or the fallback path. Mirrors the
	`callback2` no-double-wrap pin one floor up but for the throws
	variant — the post-resolution duplicate-wrap skip at
	`call_resolver.py:6110` previously listed only `callback0/1/2`,
	risking a re-wrap of an explicit `callback_throw2` arg."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

error Boom { message: String }
struct Req {}
struct Resp {}

fn make_handler(req: &Req, ctx: &mut Int) -> Resp {
	throw Boom(message = "x");
}

fn add_throws_route(handler: core.CallbackThrow2<&Req, &mut Int, Resp>) nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	return add_throws_route(core.callback_throw2(make_handler));
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], (
		f"explicit `core.callback_throw2(...)` arg must not be re-wrapped. "
		f"Got errors: {[d.message for d in errors]}"
	)


def test_site0_free_fn_nothrow_param_still_clean(tmp_path: Path) -> None:
	"""Regression guard: existing nothrow Callback2 positives must
	keep working after the throws-variant fix. Free-fn call with
	concrete `Callback2<...>` param + bare nothrow lambda continues
	to wrap as `callback2`, not `callback_throw2`. Catches a
	regression that flipped is_throw the other way.

	Lambda params are explicitly typed (`|req: &Req, ctx: &mut Int|`)
	to side-step a separate, pre-existing latent issue at this site
	where bare-untyped lambdas + reference-typed params do not get
	their inner-lambda params concretized through the
	`_wrap_explicit_capture_callbacks` fallback (no
	`expected_type=param_ty` propagation).  That issue is out of
	scope for this fix; pin only the throws-variant dispatch."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct Req {}

fn add_route(handler: core.Callback2<&Req, &mut Int, Int>) nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	return add_route(|req: &Req, ctx: &mut Int| nothrow => 0);
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], (
		f"nothrow Callback2 positive must keep working after the "
		f"throws-variant fix. Got errors: {[d.message for d in errors]}"
	)


def test_site0_free_fn_overloads_same_kind_diff_pty_no_concretize(tmp_path: Path) -> None:
	"""Two free-fn overloads of `take_cb` both take `CallbackThrow2<...>`
	but with different concrete arg/return types.  When the lambda is
	bare (no param annotations), the wrap kind `(arity, is_throw)` is
	shared so the wrap can still be selected as `callback_throw2`,
	but the EXACT `param_ty` differs across overloads — so the
	expected-type propagation must NOT lock the lambda to the
	first-seen overload's concrete types.

	This pins the kind-vs-param_ty uniqueness split.  Today's
	correct outcome: overload resolution fails (both candidates
	remain ambiguous against `Fn(Unknown,Unknown) throws -> Unknown`),
	and the user gets the standard "ambiguous call" / "no matching
	overload" diagnostic.  What MUST NOT happen is silent
	concretization toward the first overload's `&ReqA / &mut CtxA / RespA`
	types — that would let the wrong overload win without any
	diagnostic.
	"""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

error Boom { message: String }
struct ReqA {}
struct ReqB {}
struct CtxA {}
struct CtxB {}
struct RespA {}
struct RespB {}

fn take_cb(handler: core.CallbackThrow2<&ReqA, &mut CtxA, RespA>) nothrow -> Int {
	return 0;
}

fn take_cb(handler: core.CallbackThrow2<&ReqB, &mut CtxB, RespB>) nothrow -> Int {
	return 1;
}

fn main() nothrow -> Int {
	return take_cb(|req, ctx| => {
		throw Boom(message = "x");
	});
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	# An error is expected and required: the bare lambda cannot be
	# disambiguated against two same-kind overloads with different
	# concrete param types.  We require the error to be an overload-
	# resolution diagnostic on the call itself (not, say, a body
	# error from typing the lambda body against one overload's
	# concrete types).
	assert errors, (
		"expected an overload-ambiguity diagnostic when two same-kind "
		"CallbackThrow2 overloads differ in concrete arg/return types; "
		"got no errors — the wrap may have silently locked the lambda "
		"to one overload's concrete param_ty, which is the bug this "
		"test pins."
	)
	messages = [d.message for d in errors]
	# Must NOT contain a body-typing failure that would indicate the
	# lambda body was checked against one specific overload's concrete
	# types (e.g. an error referencing ReqA/ReqB/CtxA/CtxB/RespA/RespB
	# fields or methods).  The lambda body here is intentionally
	# concrete-type-free so that any leakage of overload-specific types
	# would have to come from the wrap pre-typing.
	assert any(
		"overload" in m or "ambiguous" in m or "matching" in m for m in messages
	), (
		f"expected an overload-resolution diagnostic, got: {messages}"
	)


def test_site1_static_assoc_fn_already_wrapped_no_double_wrap(tmp_path: Path) -> None:
	"""Explicit `core.callback1(fn_ref)` arg passing through the
	silent-coercion path. Pins behavior; not exercising Patch B's wrap
	helper or its dup-wrap guard (that guard's coverage is in Sites 2,
	5, 6 which DO route through the helper)."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

struct S {}

implement S {
	pub fn take_cb(cb: core.Callback1<Int, Int>) nothrow -> Int {
		return cb.call(41);
	}
}

fn add1(x: Int) nothrow -> Int { return x + 1; }

fn main() nothrow -> Int {
	return S::take_cb(core.callback1(add1));
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors



# ── Arity 3..6 — central-table coverage ───────────────────────────────
#
# Compiler arity-handling is table-driven via `_CALLBACK_ROWS` in
# `lang/driftc/checker/call_resolver.py`.  Adding a new arity is a
# one-line change to `_CALLBACK_ARITY_MAX` plus the matching
# `IntrinsicKind` enum + `call_contract` spec rows + stdlib decls.
# These carriers exercise every arity from 3 to 6 through the same
# implicit-wrap dispatch the arity-2 carriers above use, so a future
# regression that re-introduces a hardcoded `Callback0/1/2`-only
# branch surfaces here as a parametric failure, not silent breakage.
#
# Cap is 6 in v1.  For 7+ params, pack arguments into a struct.


def _arity_n_callback_source(n: int, *, throws: bool) -> str:
	"""Generate a free-fn-call carrier for `Callback{n}` /
	`CallbackThrow{n}` with a bare-lambda implicit wrap.  Lambda
	params are typed (`Int` each) and the body returns `Int`.  For
	the throws variant the lambda body throws so the dispatch must
	select `callback_throw{n}`."""
	cb_name = f"CallbackThrow{n}" if throws else f"Callback{n}"
	arg_uses = " + ".join(f"a{i}" for i in range(n)) if n > 0 else "0"
	type_args = ", ".join(["Int"] * (n + 1))  # N param types + ret type
	lambda_params = ", ".join(f"a{i}: Int" for i in range(n))
	if throws:
		exception = "error Boom { message: String }\n"
		body_block = "{ throw Boom(message = \"x\"); }"
		nothrow_kw = ""
	else:
		exception = ""
		body_block = f"{{ return {arg_uses}; }}"
		nothrow_kw = "nothrow "
	return f"""
module m;

import std.core as core;

{exception}fn take_cb(cb: core.{cb_name}<{type_args}>) nothrow -> Int {{
\treturn 0;
}}

fn main() nothrow -> Int {{
\treturn take_cb(|{lambda_params}| {nothrow_kw}=> {body_block});
}}
"""


def test_site0_arity3_callback_bare_lambda(tmp_path: Path) -> None:
	"""`Callback3<Int,Int,Int,Int>` + bare lambda → implicit `callback3`."""
	checked = _compile(tmp_path, _arity_n_callback_source(3, throws=False))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity4_callback_bare_lambda(tmp_path: Path) -> None:
	"""`Callback4<...>` + bare lambda → implicit `callback4`."""
	checked = _compile(tmp_path, _arity_n_callback_source(4, throws=False))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity5_callback_bare_lambda(tmp_path: Path) -> None:
	"""`Callback5<...>` + bare lambda → implicit `callback5`."""
	checked = _compile(tmp_path, _arity_n_callback_source(5, throws=False))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity6_callback_bare_lambda(tmp_path: Path) -> None:
	"""`Callback6<...>` + bare lambda → implicit `callback6`.

	Arity 6 is the v1 cap.  For 7+ params, pack arguments into a
	struct (documented in `effective-drift.md`)."""
	checked = _compile(tmp_path, _arity_n_callback_source(6, throws=False))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity3_callback_throw_bare_lambda(tmp_path: Path) -> None:
	"""`CallbackThrow3<...>` + bare throwing lambda → implicit
	`callback_throw3`.  Mirrors the 0.31.19 throws-variant fix at
	arity 3."""
	checked = _compile(tmp_path, _arity_n_callback_source(3, throws=True))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity4_callback_throw_bare_lambda(tmp_path: Path) -> None:
	"""`CallbackThrow4<...>` + bare throwing lambda."""
	checked = _compile(tmp_path, _arity_n_callback_source(4, throws=True))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity5_callback_throw_bare_lambda(tmp_path: Path) -> None:
	"""`CallbackThrow5<...>` + bare throwing lambda."""
	checked = _compile(tmp_path, _arity_n_callback_source(5, throws=True))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity6_callback_throw_bare_lambda(tmp_path: Path) -> None:
	"""`CallbackThrow6<...>` + bare throwing lambda — v1 arity cap."""
	checked = _compile(tmp_path, _arity_n_callback_source(6, throws=True))
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity3_explicit_wrap_named_fn(tmp_path: Path) -> None:
	"""Explicit `core.callback3(named_fn)` resolves to a `Callback3<...>`
	value.  Pins the explicit-wrap path through the central table —
	complementary to the implicit-wrap carriers above."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn handle(a: Int, b: Int, c: Int) nothrow -> Int { return a + b + c; }

fn use_cb(cb: core.Callback3<Int, Int, Int, Int>) nothrow -> Int {
\treturn cb.call(1, 2, 3);
}

fn main() nothrow -> Int {
\treturn use_cb(core.callback3(handle));
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors


def test_site0_arity6_explicit_wrap_named_fn(tmp_path: Path) -> None:
	"""Explicit `core.callback6(named_fn)` — exercises the v1 arity
	cap through the explicit-wrap path, same central-table dispatch
	as arity 3."""
	checked = _compile(
		tmp_path,
		"""
module m;

import std.core as core;

fn handle(a: Int, b: Int, c: Int, d: Int, e: Int, f: Int) nothrow -> Int {
\treturn a + b + c + d + e + f;
}

fn use_cb(cb: core.Callback6<Int, Int, Int, Int, Int, Int, Int>) nothrow -> Int {
\treturn cb.call(1, 2, 3, 4, 5, 6);
}

fn main() nothrow -> Int {
\treturn use_cb(core.callback6(handle));
}
""",
	)
	errors = [d for d in checked.diagnostics if d.severity == "error"]
	assert errors == [], errors
