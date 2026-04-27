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
