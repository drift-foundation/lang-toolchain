# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: `require T is I` must work when `I` is an interface and
`implement I for T` exists.

Today (before fix), Drift's trait solver only recognizes `require T is X`
where `X` is a `pub trait`.  Interfaces live in a separate registry that
`prove_is_trait` does not consult; a `require T is SomeInterface` clause
is refuted at `lang/driftc/traits/solver.py:378-381` with
`reasons=["unknown trait"]`, even when `implement SomeInterface for T`
exists.

That asymmetry makes interfaces second-class citizens in generic
constraints.  The fix extends the solver to recognize interface impls
as satisfying interface requirements.  This test pins both the positive
case (impl exists → requirement satisfied) and the negative case (no
impl → normal E_REQUIREMENT_NOT_SATISFIED diagnostic).

This is a LANGUAGE-level regression, not an Arc-view regression — the
feature is general, and `Arc<T>.as_interface<I>() require T is I` uses
the same machinery as any other generic function requiring an interface
impl.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(
	tmp_path: Path, source: str, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict]:
	mod_root = tmp_path / "mods"
	_write_file(mod_root / "main" / "main.drift", source)
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		str(mod_root / "main" / "main.drift"),
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


_POSITIVE_IMPL_SATISFIES_REQUIRE = """
module main;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Dog {
	pub n: Int
}

implement Speaker for Dog {
	pub fn speak(self: &Dog) nothrow -> Int {
		return self.n;
	}
}

fn f<T>(x: T) nothrow -> Int require T is Speaker {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val d = Dog(n = 7);
	return f<type Dog>(d);
}
""".lstrip()


def test_interface_impl_satisfies_require_clause(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive case: `implement Speaker for Dog` must satisfy
	`require T is Speaker` — the solver is extended to consult the
	interface-impl index for interface-typed requirements."""
	rc, payload = _run_driftc_json(tmp_path, _POSITIVE_IMPL_SATISFIES_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"expected clean compile — Dog implements Speaker, `require T is Speaker` "
		f"must be satisfied.  Got rc={rc}; errors: {errors}"
	)


_NEGATIVE_MISSING_IMPL_REJECTED = """
module main;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub struct Cat {
	pub n: Int
}

// No `implement Speaker for Cat` — the require clause must fail.

fn f<T>(x: T) nothrow -> Int require T is Speaker {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val c = Cat(n = 7);
	return f<type Cat>(c);
}
""".lstrip()


def test_missing_interface_impl_rejected_by_require_clause(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative case: `Cat` has no `implement Speaker for Cat`, so
	`require T is Speaker` must be rejected through the normal
	require-diagnostic path (E_REQUIREMENT_NOT_SATISFIED), not with
	'unknown trait' or a one-off interface-specific diagnostic."""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_MISSING_IMPL_REJECTED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Cat does not implement Speaker; "
		f"but compilation succeeded (rc={rc}, diagnostics={diagnostics})"
	)
	# The correct diagnostic is E_REQUIREMENT_NOT_SATISFIED with the
	# subject=Cat, trait=Speaker.  Anything else (most importantly
	# "unknown trait") indicates the solver hasn't been extended.
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Speaker" in (e.get("message") or "")
		and "Cat" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED with Cat/Speaker; got: {errors}"
	)
	# Specifically: must NOT come back as an 'unknown trait' refutation —
	# that was the pre-fix failure mode.
	for e in req_errors:
		notes = " ".join(e.get("notes") or [])
		assert "unknown trait" not in notes, (
			f"diagnostic still says 'unknown trait' for interface requirement — "
			f"solver was not extended correctly: {e}"
		)


_NEGATIVE_CROSS_IMPL_REJECTED = """
module main;

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}

pub interface Listener {
	fn listen(self: &Self) nothrow -> Int;
}

pub struct Dog {
	pub n: Int
}

// Dog implements Speaker but NOT Listener.
implement Speaker for Dog {
	pub fn speak(self: &Dog) nothrow -> Int {
		return self.n;
	}
}

fn needs_listener<T>(x: T) nothrow -> Int require T is Listener {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val d = Dog(n = 7);
	return needs_listener<type Dog>(d);
}
""".lstrip()


def test_wrong_interface_impl_does_not_satisfy_require(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Sanity: implementing one interface must not accidentally satisfy
	a require for a different interface.  The solver must match the
	specific interface named in the require clause, not just 'some
	interface impl exists for T'."""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_CROSS_IMPL_REJECTED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Dog implements Speaker but not Listener; "
		f"the require clause for Listener must not be satisfied by the Speaker "
		f"impl.  Got rc={rc}, diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Listener" in (e.get("message") or "")
		and "Dog" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED with Dog/Listener; got: {errors}"
	)


def _run_driftc_json_multi(
	tmp_path: Path, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict]:
	"""Compile a multi-file module root.  `files` maps relative paths
	(e.g. "iface/iface.drift", "main/main.drift") to source.  All
	files are passed to the driver so its module-discovery step sees
	every module in the workspace."""
	mod_root = tmp_path / "mods"
	all_paths: list[Path] = []
	for rel, text in files.items():
		path = mod_root / rel
		_write_file(path, text)
		all_paths.append(path)
	root = stdlib_root()
	args = [
		"-M", str(mod_root),
		*[str(p) for p in all_paths],
		"--dev",
		"--json",
	]
	if root:
		args += ["--stdlib-root", str(root)]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_imported_interface_impl_satisfies_require_clause(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Cross-module: interface `Speaker` declared in module `iface`;
	struct `Dog` in module `main` imports `iface` and implements
	`iface.Speaker` for Dog.  A generic `fn f<T>(x: T) require T is
	iface.Speaker` in main must accept `f(Dog(...))`.

	This is the production shape for `Arc<T>.as_interface<I>()`: the
	interface (log.ContextResolver, metrics.Emitter, …) always lives
	in a different module than the concrete impl.  Without cross-
	module interface-impl registration, per-module world building
	classifies the impl as a trait-impl (because `iface.Speaker` isn't
	in main's local interface names), and the solver fails to prove
	the requirement."""
	files = {
		"iface/iface.drift": """
module iface;

export { Speaker };

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}
""".lstrip(),
		"main/main.drift": """
module main;

import iface as iface;

pub struct Dog {
	pub n: Int
}

implement iface.Speaker for Dog {
	pub fn speak(self: &Dog) nothrow -> Int {
		return self.n;
	}
}

fn f<T>(x: T) nothrow -> Int require T is iface.Speaker {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val d = Dog(n = 7);
	return f<type Dog>(d);
}
""".lstrip(),
	}
	rc, payload = _run_driftc_json_multi(tmp_path, files, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"expected clean compile — Dog implements iface.Speaker in main; "
		f"`require T is iface.Speaker` must be satisfied by the cross-module "
		f"impl.  Got rc={rc}; errors: {errors}"
	)


_HEAD_MATCH_ALONE_NOT_ENOUGH = """
module main;

pub interface Carrier {
	fn size(self: &Self) nothrow -> Int;
}

pub struct Box<T> {
	pub v: T
}

// Concrete-instantiation impl: Box<Int> only — NOT Box<T> generically.
implement Carrier for Box<Int> {
	pub fn size(self: &Box<Int>) nothrow -> Int {
		return self.v;
	}
}

fn f<T>(x: T) nothrow -> Int require T is Carrier {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val s = Box<type String>(v = "hi");
	return f<type Box<String>>(s);
}
""".lstrip()


def test_head_match_alone_insufficient_for_interface_require(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""`implement Carrier for Box<Int>` must NOT satisfy
	`require T is Carrier` when T = Box<String>.  Head (Box) matches
	both, but the concrete instantiation does not — the solver must
	match the full target type key, not just the head.  A false proof
	here would route `size()` through a vtable built for Box<Int>
	against a Box<String> value, producing type-confused dispatch at
	runtime."""
	rc, payload = _run_driftc_json(tmp_path, _HEAD_MATCH_ALONE_NOT_ENOUGH, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Box<String> does not have Carrier impl "
		f"(only Box<Int> does).  Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Carrier" in (e.get("message") or "")
		and "Box" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED with Box/Carrier; got: {errors}"
	)


_GENERIC_IMPL_DEFERRED = """
module main;

pub interface Carrier {
	fn size(self: &Self) nothrow -> Int;
}

pub struct Box<T> {
	pub v: T
}

// Generic impl — Phase 1 defers applicability checking; a `require
// T is Carrier` call site must NOT treat this as an automatic proof.
implement<T> Carrier for Box<T> {
	pub fn size(self: &Box<T>) nothrow -> Int {
		val _ = self;
		return 1;
	}
}

fn f<T>(x: T) nothrow -> Int require T is Carrier {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val s = Box<type Int>(v = 42);
	return f<type Box<Int>>(s);
}
""".lstrip()


def test_generic_interface_impl_does_not_silently_prove_require(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Phase 1: generic interface impls (`implement<T> I for Box<T>`)
	do NOT automatically satisfy `require U is I`.  Phase 1 only
	proves via non-generic, non-conditional impls where the impl's
	target equals the subject exactly.

	If a future phase adds full applicability checking this test can
	be flipped — but the current behavior must refute, not over-prove.
	Over-proving is the soundness hole: without an applicability check
	the solver cannot verify that the impl's own require clause
	(e.g. `implement<T> I for Box<T> require T is Debug`) would be
	satisfied at the use site."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_IMPL_DEFERRED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — generic `implement<T> Carrier for Box<T>` "
		f"must not silently satisfy `require T is Carrier` in Phase 1.  "
		f"Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Carrier" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED for generic-impl require; "
		f"got: {errors}"
	)


_CONDITIONAL_IMPL_DEFERRED = """
module main;

import std.core as core;

pub interface Carrier {
	fn size(self: &Self) nothrow -> Int;
}

pub struct Box<T> {
	pub v: T
}

// Conditional impl with its own require clause.  Phase 1 does not
// check whether that inner require is satisfiable at a use site —
// it simply defers the whole generic/conditional case.
implement<T> Carrier for Box<T> require T is core.Copy {
	pub fn size(self: &Box<T>) nothrow -> Int {
		val _ = self;
		return 1;
	}
}

fn f<T>(x: T) nothrow -> Int require T is Carrier {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val s = Box<type Int>(v = 42);
	return f<type Box<Int>>(s);
}
""".lstrip()


def test_conditional_interface_impl_does_not_silently_prove_require(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Phase 1: conditional interface impls (`implement<T> I for Box<T>
	require T is Copy`) must not satisfy `require U is I` either.  Even
	if the inner requirement happens to hold at the use site, Phase 1
	doesn't check it — there is no impl-applicability solver for
	interfaces yet.  Accepting this would create a dangerous precedent
	where the compiler appears to prove something it hasn't actually
	verified."""
	rc, payload = _run_driftc_json(tmp_path, _CONDITIONAL_IMPL_DEFERRED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — conditional `implement<T> Carrier for Box<T> "
		f"require T is Copy` must not silently satisfy a downstream "
		f"`require U is Carrier` in Phase 1.  Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Carrier" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED for conditional-impl require; "
		f"got: {errors}"
	)


def test_imported_interface_unsatisfied_rejected(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Cross-module negative: Cat has no impl for the imported
	`iface.Speaker`.  `f(Cat(...))` must be rejected through the
	normal E_REQUIREMENT_NOT_SATISFIED path — not 'unknown trait'."""
	files = {
		"iface/iface.drift": """
module iface;

export { Speaker };

pub interface Speaker {
	fn speak(self: &Self) nothrow -> Int;
}
""".lstrip(),
		"main/main.drift": """
module main;

import iface as iface;

pub struct Cat {
	pub n: Int
}

// No `implement iface.Speaker for Cat` — require clause must fail.

fn f<T>(x: T) nothrow -> Int require T is iface.Speaker {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val c = Cat(n = 7);
	return f<type Cat>(c);
}
""".lstrip(),
	}
	rc, payload = _run_driftc_json_multi(tmp_path, files, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Cat does not implement iface.Speaker; "
		f"got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "Speaker" in (e.get("message") or "")
		and "Cat" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED with Cat/Speaker; got: {errors}"
	)
