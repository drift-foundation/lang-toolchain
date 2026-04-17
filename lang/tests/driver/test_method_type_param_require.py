# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression (general, not Arc-specific): a method-level
type parameter used as the trait/interface side of a `require T is I`
clause must be substituted with the caller-supplied type argument
before the require clause is proven.

Today the compiler correctly substitutes impl-level type parameters
(so `T` from `implement<T> Holder<T>` resolves to the caller's type
at the call site), but method-level type parameters — the `<I>` in
`fn check<I>(...) require T is I` — are NOT threaded into the
require-substitution map.  At enforcement time the solver sees an
unresolved trait reference like `std.concurrent.I` or
`main.I` instead of the caller's actual trait/interface argument.

Consequences:

- Positive calls never prove.  Every `fn f<T, I>(x: T) require T is I`
  call site refutes, no matter whether T actually implements I.
- Error diagnostics misleadingly name the declaration-local `I`
  instead of the caller-supplied trait, making this look like
  "no impl" when the real issue is substitution.

Surfaced while implementing `Arc<T>.as_interface<I>() require T is I`
(Stage 1 of fat-Arc interface views), but this pin deliberately does
NOT mention Arc — it's the general constraint-system shape that other
generic methods can hit.  Fix sits in the enforcement path; once
landed, Arc's intrinsic can rely on the require clause as the
soundness gate rather than owning a parallel `T implements I` check.
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


_POSITIVE_METHOD_TYPE_PARAM_SATISFIES_REQUIRE = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

pub struct Holder<T> {
	pub v: T
}

implement<T> Holder<T> {
	pub fn check<I>(self: &Holder<T>) nothrow -> Int require T is I {
		val _ = self;
		return 1;
	}
}

fn main() nothrow -> Int {
	val h = Holder<type Thing>(v = Thing(n = 7));
	return h.check<type Face>();
}
""".lstrip()


def test_method_type_param_substitutes_into_require_positive(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive case: `Holder<Thing>.check<Face>()` with `require T is I`
	must prove because Thing implements Face.  The compiler must
	substitute T → Thing (impl-level; already works) AND I → Face
	(method-level; the fix point) before the require solver is
	consulted."""
	rc, payload = _run_driftc_json(tmp_path, _POSITIVE_METHOD_TYPE_PARAM_SATISFIES_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"expected clean compile — Thing implements Face, so "
		f"`Holder<Thing>.check<Face>()` with `require T is I` must prove.  "
		f"Got rc={rc} errors={errors}"
	)


_NEGATIVE_METHOD_TYPE_PARAM_UNSATISFIED = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub interface OtherFace {
	fn other(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

// Thing implements Face but NOT OtherFace.
implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

pub struct Holder<T> {
	pub v: T
}

implement<T> Holder<T> {
	pub fn check<I>(self: &Holder<T>) nothrow -> Int require T is I {
		val _ = self;
		return 1;
	}
}

fn main() nothrow -> Int {
	val h = Holder<type Thing>(v = Thing(n = 7));
	return h.check<type OtherFace>();
}
""".lstrip()


_POSITIVE_FREE_FN_TYPE_PARAM_SATISFIES_REQUIRE = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

fn check<T, I>(x: T) nothrow -> Int require T is I {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val t = Thing(n = 7);
	return check<type Thing, Face>(t);
}
""".lstrip()


def test_free_fn_type_param_substitutes_into_require_positive(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Free-function companion to the method test: `fn check<T, I>(x: T)
	require T is I`.  `check<type Thing, type Face>(thing)` must
	prove because Thing implements Face.  The method test already
	pins the same shape via a method; this test pins that the fix
	is truly general — any generic function with a type param on
	the trait side of `is` works, not just methods on generic
	impls."""
	rc, payload = _run_driftc_json(tmp_path, _POSITIVE_FREE_FN_TYPE_PARAM_SATISFIES_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"expected clean compile — Thing implements Face, so "
		f"`check<Thing, Face>(thing)` with `require T is I` must prove.  "
		f"Got rc={rc} errors={errors}"
	)


_NEGATIVE_FREE_FN_TYPE_PARAM_UNSATISFIED = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub interface OtherFace {
	fn other(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

fn check<T, I>(x: T) nothrow -> Int require T is I {
	val _ = x;
	return 1;
}

fn main() nothrow -> Int {
	val t = Thing(n = 7);
	return check<type Thing, OtherFace>(t);
}
""".lstrip()


_POSITIVE_STRUCT_TYPE_PARAM_SATISFIES_REQUIRE = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

pub struct Holder<T, I> require T is I {
	pub v: T
}

fn main() nothrow -> Int {
	val h = Holder<type Thing, Face>(v = Thing(n = 7));
	val _ = h;
	return 0;
}
""".lstrip()


def test_struct_type_param_substitutes_into_require_positive(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Struct-level companion: `struct Holder<T, I> require T is I`
	must proof-succeed for `Holder<Thing, Face>` when Thing
	implements Face.  Struct requires travel through a separate
	enforcement path (`enforce_struct_requires`) from function /
	method requires, so this pin proves the substitution story is
	unified across all three call paths, not just fn + method."""
	rc, payload = _run_driftc_json(tmp_path, _POSITIVE_STRUCT_TYPE_PARAM_SATISFIES_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc == 0 and not errors, (
		f"expected clean compile — Thing implements Face, so "
		f"`Holder<Thing, Face>` with struct-level `require T is I` must prove.  "
		f"Got rc={rc} errors={errors}"
	)


_NEGATIVE_STRUCT_TYPE_PARAM_UNSATISFIED = """
module main;

pub interface Face {
	fn value(self: &Self) nothrow -> Int;
}

pub interface OtherFace {
	fn other(self: &Self) nothrow -> Int;
}

pub struct Thing {
	pub n: Int
}

implement Face for Thing {
	pub fn value(self: &Thing) nothrow -> Int {
		return self.n;
	}
}

pub struct Holder<T, I> require T is I {
	pub v: T
}

fn main() nothrow -> Int {
	val h = Holder<type Thing, OtherFace>(v = Thing(n = 7));
	val _ = h;
	return 0;
}
""".lstrip()


def test_struct_type_param_substitutes_into_require_negative(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Struct-level negative: `Holder<Thing, OtherFace>` where Thing
	does not implement OtherFace must be rejected with
	`E_REQUIREMENT_NOT_SATISFIED` naming OtherFace (the substituted
	target), not the declaration-local `I`."""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_STRUCT_TYPE_PARAM_UNSATISFIED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Thing does not implement OtherFace.  "
		f"Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "OtherFace" in (e.get("message") or "")
		and "Thing" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED naming OtherFace (substituted "
		f"target, not declaration-local I); got: {errors}"
	)
	for e in req_errors:
		msg = e.get("message") or ""
		assert not msg.rstrip().endswith(" is I"), (
			f"diagnostic ends with bare 'is I' — struct-level type-param "
			f"substitution didn't happen: {msg}"
		)
		assert ".I (required" not in msg and ".I)" not in msg, (
			f"diagnostic renders trait as '<module>.I' — struct-level "
			f"type-param substitution regressed: {msg}"
		)


def test_free_fn_type_param_substitutes_into_require_negative(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Free-function negative: `check<Thing, OtherFace>(thing)` where
	Thing does not implement OtherFace must be rejected with
	`E_REQUIREMENT_NOT_SATISFIED` that names OtherFace (the
	substituted target), not the declaration-local `I`.  Same
	substitution-quality pin as the method negative test."""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_FREE_FN_TYPE_PARAM_UNSATISFIED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Thing does not implement OtherFace.  "
		f"Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "OtherFace" in (e.get("message") or "")
		and "Thing" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED naming OtherFace (the "
		f"substituted target, not declaration-local I); got: {errors}"
	)
	for e in req_errors:
		msg = e.get("message") or ""
		assert not msg.rstrip().endswith(" is I"), (
			f"diagnostic ends with bare 'is I' — free-function type-param "
			f"substitution did not happen: {msg}"
		)
		assert ".I (required" not in msg and ".I)" not in msg, (
			f"diagnostic renders trait as '<module>.I' — free-function "
			f"type-param substitution regressed: {msg}"
		)


def test_method_type_param_substitutes_into_require_negative(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative case: `check<OtherFace>()` where Thing does not
	implement OtherFace must be rejected via
	E_REQUIREMENT_NOT_SATISFIED.  Critically, the diagnostic must
	name the substituted target interface (OtherFace), not the
	declaration-local `I`.  If the diagnostic still says `...I` or
	`...module.I`, substitution hasn't happened — the solver is
	seeing an unresolved placeholder and the fix is incomplete."""
	rc, payload = _run_driftc_json(tmp_path, _NEGATIVE_METHOD_TYPE_PARAM_UNSATISFIED, capsys)
	diagnostics = payload.get("diagnostics", [])
	errors = [d for d in diagnostics if d.get("severity") == "error"]
	assert rc != 0, (
		f"expected compile failure — Thing does not implement OtherFace.  "
		f"Got rc={rc} diagnostics={diagnostics}"
	)
	req_errors = [
		e for e in errors
		if e.get("code") == "E_REQUIREMENT_NOT_SATISFIED"
		and "OtherFace" in (e.get("message") or "")
		and "Thing" in (e.get("message") or "")
	]
	assert req_errors, (
		f"expected E_REQUIREMENT_NOT_SATISFIED naming OtherFace (the "
		f"substituted target, not the declaration-local I); got: {errors}"
	)
	# Additionally: the unsubstituted-I shape must NOT appear.  If
	# the message still contains a literal "is I" trailing token or
	# the trait is rendered as "<module>.I", substitution didn't
	# happen on the fix path.
	for e in req_errors:
		msg = e.get("message") or ""
		# The declaration-local I rendering we want to make sure is gone.
		# Accept `Thing is OtherFace` but not `Thing is main.I` or similar.
		assert not msg.rstrip().endswith(" is I"), (
			f"diagnostic still ends with bare 'is I' — method-level type "
			f"param not substituted: {msg}"
		)
		assert ".I (required" not in msg and ".I)" not in msg, (
			f"diagnostic still renders trait as '<module>.I' — method-level "
			f"type param not substituted: {msg}"
		)
