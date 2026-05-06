# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Auto-try contract tests.

Auto-try is compiler-owned: inside a `throws` function or `try {}` block,
Result<T, E> expressions eagerly auto-unwrap to T via or_throw() with no
trait import required.  Explicit `Result<T, E>` type annotation is the
opt-out — a binding annotated as Result preserves the Result object so
the user can call `.or_throw()` / pattern match.

Tests here pin:
  - `throws` auto-unwraps inferred `val r = fallible();` to T (zero ceremony)
  - explicit `Result<T, E>` annotation opts OUT of auto-unwrap
  - `try {}` blocks also auto-unwrap without any trait import
  - explicit `.or_throw()` is the supported explicit form (no trait import)
  - explicit `.into_try()` is rejected (method removed)
  - error types must implement Throw for or_throw to work
  - borrowed &Result cannot use or_throw (ownership required)
"""
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_source(src: str, tmp_path: Path):
	path = tmp_path / "main.drift"
	_write_file(path, src)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked.diagnostics


# ---------------------------------------------------------------------------
# Positive: auto-try works without any trait import
# ---------------------------------------------------------------------------


def test_throws_auto_try_without_use_trait(tmp_path: Path) -> None:
	"""A `throws` function auto-converts Result<T, E> without requiring
	`use trait core.Try`.  The implicit propagation is compiler-owned."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;
import std.net as net;

fn do_work() throws -> Int {
	val addr = net.socket_addr("127.0.0.1", 9999);
	return 0;
}

fn main() nothrow -> Int {
	return try do_work() catch { 99 };
}
""",
		tmp_path,
	)
	assert diagnostics == [], (
		f"throws auto-try should work without 'use trait core.Try'; "
		f"got {[d.message for d in diagnostics]}"
	)


def test_throws_inferred_local_auto_unwraps(tmp_path: Path) -> None:
	"""In a `throws` function, an unannotated `val r = fallible();` binding
	eagerly auto-unwraps to T.  This is the primary ergonomic contract of
	`throws` — zero ceremony for the common case.

	Regression: downstream uses of `r` must see `T`, not `Result<T, E>`.

	Slice 5 (pub-error track): or_throw requires Err to be `pub error`."""
	src = """
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn fallible() -> core.Result<Int, MyErr> {
	return core.Result::Ok(42);
}

fn take_int(x: Int) nothrow -> Int {
	return x;
}

fn do_work() throws -> Int {
	val r = fallible();          // eager auto-unwrap → r: Int
	return take_int(r);           // passes Int to a fn expecting Int
}

fn main() nothrow -> Int {
	return try do_work() catch { 99 };
}
"""
	diags = _compile_source(src, tmp_path)
	assert diags == [], (
		f"unannotated val binding in a throws fn should auto-unwrap Result; "
		f"got {[d.message for d in diags]}"
	)


def test_throws_annotated_result_preserves_result(tmp_path: Path) -> None:
	"""Explicit `Result<T, E>` type annotation is the auto-try OPT-OUT —
	the binding preserves the Result object so the user can call
	`.or_throw()` / pattern-match explicitly.

	Regression for the K28 package-boundary local_binding shape:
	`val r: Result<T, E> = producer_fn(); return (move r).or_throw();`
	must compile and resolve `.or_throw()` on the Result local.

	Slice 5 (pub-error track): or_throw requires Err to be `pub error`."""
	src = """
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn fallible() -> core.Result<Int, MyErr> {
	return core.Result::Ok(42);
}

fn do_work() throws -> Int {
	val r: core.Result<Int, MyErr> = fallible();
	return (move r).or_throw();
}

fn main() nothrow -> Int {
	return try do_work() catch { 99 };
}
"""
	diags = _compile_source(src, tmp_path)
	assert diags == [], (
		f"explicit Result<T, E> annotation should opt out of auto-try; "
		f"got {[d.message for d in diags]}"
	)


def test_throws_annotated_non_result_auto_unwraps(tmp_path: Path) -> None:
	"""With a non-Result type annotation, auto-try still fires — this is
	the same as inferred (eager) but with an explicit expected type.

	Slice 5 (pub-error track): or_throw requires Err to be `pub error`."""
	src = """
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn fallible() -> core.Result<Int, MyErr> {
	return core.Result::Ok(42);
}

fn do_work() throws -> Int {
	val x: Int = fallible();
	return x;
}

fn main() nothrow -> Int {
	return try do_work() catch { 99 };
}
"""
	diags = _compile_source(src, tmp_path)
	assert diags == [], (
		f"annotated val binding with non-Result type should auto-unwrap; "
		f"got {[d.message for d in diags]}"
	)


def test_try_block_auto_try_without_use_trait(tmp_path: Path) -> None:
	"""A bare `try {}` block (outside a throws function) auto-propagates a
	discarded Result<T, E> expression statement via or_throw() — no trait
	import required.  Auto-try is compiler-owned in all auto-try contexts.

	Slice 5 (pub-error track): or_throw requires Err to be `pub error`."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn fallible() -> core.Result<Int, MyErr> {
	return core.Result::Ok(42);
}

fn main() nothrow -> Int {
	var out = 0;
	try {
		fallible();      // discarded Result auto-propagates via or_throw
		out = 1;
	} catch {
		out = 99;
	}
	return out;
}
""",
		tmp_path,
	)
	assert diagnostics == [], (
		f"try-block auto-try should work without 'use trait core.Try'; "
		f"got {[d.message for d in diagnostics]}"
	)


# ---------------------------------------------------------------------------
# Positive: explicit .or_throw() is the supported explicit form
# ---------------------------------------------------------------------------


def test_explicit_or_throw_works_without_use_trait(tmp_path: Path) -> None:
	"""Explicit `.or_throw()` is an inherent method on Result and must work
	without any trait import.  This is the supported explicit form.

	Slice 5 (pub-error track): or_throw requires Err to be `pub error`."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn main() -> Int {
	val r: core.Result<Int, MyErr> = core.Result::Ok(42);
	val v = r.or_throw();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics == [], (
		f"explicit .or_throw() should work without 'use trait core.Try'; "
		f"got {[d.message for d in diagnostics]}"
	)


def test_or_throw_with_stdlib_pub_error(tmp_path: Path) -> None:
	"""or_throw works with stdlib `pub error` types — the auto-gen
	`implement core.Throw for E` provides the Throw contract, no manual
	impl required.

	Slice 5 (pub-error track): replaces the old test that pinned
	`Result<T, NetError>` (NetError is a `pub variant` and is no longer
	a valid `or_throw` Err under Phase 5a strict enforcement)."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;
import std.err as err;

fn main() -> Int {
	val r: core.Result<Int, err.IndexError> = core.Result::Ok(0);
	val _v = r.or_throw();
	return 0;
}
""",
		tmp_path,
	)
	assert diagnostics == []


# ---------------------------------------------------------------------------
# Negative: .into_try() is removed
# ---------------------------------------------------------------------------


def test_into_try_rejected(tmp_path: Path) -> None:
	"""`.into_try()` is no longer a valid method — the Try trait has been
	removed.  This pins the removal: a later change that accidentally
	reintroduces Try/into_try would fail this test."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

fn main() -> Int {
	val r: core.Result<Int, Int> = core.Result::Ok(1);
	val v = r.into_try();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics, (
		"explicit .into_try() should be rejected — the Try trait has been removed"
	)
	assert any("into_try" in d.message or "method" in d.message.lower() for d in diagnostics)


# ---------------------------------------------------------------------------
# Negative: error type constraints still enforced
# ---------------------------------------------------------------------------


def test_or_throw_on_ref_rejected(tmp_path: Path) -> None:
	"""Borrowed &Result cannot use or_throw — users must own the Result.
	The owned or_throw impl calls Throw::throw_self which consumes the
	error value.

	Slice 5 (pub-error track): Err is a `pub error` so the rejection
	is unambiguously about receiver shape (not E_OR_THROW_NOT_ERROR_TYPE),
	pinning the &T-receiver dispatch failure on its own."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub error MyErr {
	code: Int,
}

fn main() -> Int {
	val r: core.Result<Int, MyErr> = core.Result::Ok(1);
	val v = (&r).or_throw();
	return v;
}
""",
		tmp_path,
	)
	assert len(diagnostics) > 0, "borrowed &Result should not have an or_throw impl"
	# Phase 5a: with `pub error` Err, the rejection MUST come from the
	# receiver-shape / no-matching-method dispatch — NOT from
	# E_OR_THROW_NOT_ERROR_TYPE.
	codes = {d.code for d in diagnostics}
	assert "E_OR_THROW_NOT_ERROR_TYPE" not in codes, (
		f"`(&r).or_throw()` rejection should be about borrowed receiver "
		f"shape, not Err type; got codes: {codes}"
	)
	assert any("or_throw" in d.message or "method" in d.message.lower() for d in diagnostics)


def test_or_throw_requires_pub_error_err(tmp_path: Path) -> None:
	"""Slice 5 (pub-error track) Phase 5a strict enforcement: `or_throw()`
	requires the Err type to be a `pub error`.  A `pub variant` Err
	(even one that implements Diagnostic / Throw) is rejected at compile
	time with `E_OR_THROW_NOT_ERROR_TYPE`.

	Replaces the legacy `test_or_throw_requires_throw_impl` framing —
	under Slice 5, the rejection is `or_throw requires pub error`, not
	`Throw impl missing`."""
	diagnostics = _compile_source(
		"""
module main;

import std.core as core;

pub variant MyErr {
	Msg(m: String),
	@tombstone None
}

	implement core.Diagnostic for MyErr {
		pub fn to_json_text(self: &MyErr) nothrow -> String {
			return match self {
				Msg(m) => {
					core.diagnostic_json_string(m)
				},
				default => { core.diagnostic_json_int(0) }
			};
		}
	}

	fn main() -> Int {
	val r: core.Result<Int, MyErr> = core.Result::Ok(1);
	val v = r.or_throw();
	return v;
}
""",
		tmp_path,
	)
	assert diagnostics, "non-pub-error Err type should be rejected by or_throw"
	codes = {d.code for d in diagnostics}
	assert "E_OR_THROW_NOT_ERROR_TYPE" in codes, (
		f"expected E_OR_THROW_NOT_ERROR_TYPE for `Result<T, MyVariant>.or_throw()`; "
		f"got codes: {codes}"
	)
