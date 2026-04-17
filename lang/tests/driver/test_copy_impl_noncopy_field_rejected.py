# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression: `implement core.Copy for T` must be rejected
when T contains non-Copy fields, in both user code and stdlib.

Without this check, bit-copying a struct containing a non-Copy field
(e.g. `conc.Arc<Interface>`) creates an unretained duplicate whose
destructor decrements the refcount a second time — use-after-free.

The bug was exposed by the std.log resolver work (0.27.202) when a
stale `implement core.Copy for LoggerConfig` was left in place after
adding a non-Copy `conc.Arc<ContextResolver>` field.

Trigger shape: struct with an Arc<Interface> field + Copy impl.
Expected: compile-time rejection with E_COPY_IMPL_NONCOPY_TARGET.
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


def _run_driftc_json(tmp_path: Path, source: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
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


_COPY_WITH_ARC_FIELD = """
module main;

import std.core as core;
import std.concurrent as conc;

pub interface Svc {
	fn run(self: &Self) nothrow -> Int;
}

pub struct Holder {
	pub svc: conc.Arc<Svc>
}

implement core.Copy for Holder {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_copy_impl_rejected_for_struct_with_arc_interface_field(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""A struct containing a non-Copy field (Arc<Interface>) must not
	accept a core.Copy impl.  Validation applies uniformly — the
	checker no longer exempts std.* / lang.* modules; only a narrow
	allowlist of compiler-known Copy types (String, DiagnosticValue)
	bypasses the structural prover.  Generic Copy impls
	(`implement<T> Copy for X<T> require T is Copy`) carry their
	soundness in the require clause; concrete instantiations are
	checked when they appear as struct fields."""
	rc, payload = _run_driftc_json(tmp_path, _COPY_WITH_ARC_FIELD, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc != 0, (
		f"expected compile failure for Copy on struct with Arc<Interface> field, "
		f"but compilation succeeded (rc={rc} diags={diagnostics})"
	)
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	assert any("Copy" in m for m in error_msgs), (
		f"expected a Copy-related rejection diagnostic, got: {error_msgs}"
	)


_GENERIC_COPY_WITHOUT_REQUIRE = """
module main;

import std.core as core;

pub struct Box<T> {
	pub value: T
}

implement<T> core.Copy for Box<T> {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_rejected_without_require_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Pin the tightened generic-Copy gate: `implement<T> Copy for
	Box<T>` where T appears in a stored field must be rejected unless
	the impl carries `require T is Copy`.  Without the gate, a Box
	containing a non-Copy T (e.g. Arc<I>) would bit-copy and
	double-free, recreating the LoggerConfig UAF class through a
	generic door."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_WITHOUT_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc != 0, (
		f"expected compile failure for generic Copy<Box<T>> with stored T, "
		f"but compilation succeeded (rc={rc} diags={diagnostics})"
	)
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	assert any("Copy" in m for m in error_msgs), (
		f"expected a Copy-related rejection diagnostic, got: {error_msgs}"
	)


_GENERIC_COPY_WITH_REQUIRE = """
module main;

import std.core as core;

pub struct Pair<T> {
	pub a: T,
	pub b: T
}

implement<T> core.Copy for Pair<T> require T is core.Copy {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_accepted_with_require_copy(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive case: `implement<T> Copy for Pair<T> require T is Copy`
	must be accepted — the require clause is the soundness contract
	that concrete instantiations carry forward through the structural
	prover."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_WITH_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	copy_errors = [m for m in error_msgs if "Copy" in m]
	assert rc == 0 and not copy_errors, (
		f"expected clean compile for generic Copy<Pair<T>> with require T is Copy, "
		f"got rc={rc} errors={error_msgs}"
	)


_GENERIC_COPY_NESTED_WRAPPER = """
module main;

import std.core as core;

pub struct Pair<T> {
	pub a: T,
	pub b: T
}

implement<T> core.Copy for Pair<T> require T is core.Copy {
}

pub struct Outer<T> {
	pub p: Pair<T>
}

implement<T> core.Copy for Outer<T> require T is core.Copy {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_accepted_for_nested_generic_wrapper(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive case: a generic Copy wrapper whose field instantiates
	another generic Copy type with the same covered type param
	(`struct Outer<T> { p: Pair<T> }` + `implement<T> Copy for Outer<T>
	require T is Copy`) must be accepted.  The structural prover
	drills into Pair<T>'s substituted field_types and sees T as a
	TYPEVAR; covered_tparams includes T, so the recursion bottoms out
	as Copy.  This verifies `_copy_declared` plus the recursive
	instance-field walk handles generic Copy dependencies beyond
	direct T fields."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_NESTED_WRAPPER, capsys)
	diagnostics = payload.get("diagnostics", [])
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	copy_errors = [m for m in error_msgs if "Copy" in m]
	assert rc == 0 and not copy_errors, (
		f"expected clean compile for Outer<T> wrapping Pair<T> with require T is Copy, "
		f"got rc={rc} errors={error_msgs}"
	)


_GENERIC_COPY_NESTED_WRAPPER_MISSING_REQUIRE = """
module main;

import std.core as core;

pub struct Pair<T> {
	pub a: T,
	pub b: T
}

implement<T> core.Copy for Pair<T> require T is core.Copy {
}

pub struct Outer<T> {
	pub p: Pair<T>
}

implement<T> core.Copy for Outer<T> {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_rejected_for_nested_wrapper_without_require(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative case: the wrapper `Outer<T> { p: Pair<T> }` must
	propagate the Copy obligation — without its own
	`require T is Copy`, the structural prover sees T as a TYPEVAR
	not in covered_tparams when drilling through Pair<T>'s
	substituted fields, and rejects the Outer Copy impl."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_NESTED_WRAPPER_MISSING_REQUIRE, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc != 0, (
		f"expected compile failure for Outer<T> wrapping Pair<T> without require T is Copy, "
		f"but compilation succeeded (rc={rc} diags={diagnostics})"
	)
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	assert any("Copy" in m for m in error_msgs), (
		f"expected a Copy-related rejection diagnostic, got: {error_msgs}"
	)


_GENERIC_COPY_SHADOWING_LOCAL_COPY_TRAIT = """
module main;

import std.core as core;

pub trait Copy {
}

pub struct Sneaky { }

implement Copy for Sneaky {
}

pub struct Box<T> {
	pub value: T
}

implement<T> core.Copy for Box<T> require T is Copy {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_rejects_shadowing_local_copy_trait(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Negative case: a locally-defined `trait Copy` must not satisfy
	a `require T is Copy` clause on a `core.Copy` impl — the covered
	set is keyed on resolved trait identity (std.core.Copy), not on
	the bare name `Copy`.  Without this anchor, a user could write:

	    pub trait Copy {}
	    implement<T> core.Copy for Box<T> require T is Copy {}

	and trick the prover into accepting an unsound generic core.Copy
	impl for Box<T>: the `require` clause names the local trait, but
	the impl target is core.Copy.  The checker must reject Box<T> as
	non-Copy because T is not covered for the *canonical* Copy."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_SHADOWING_LOCAL_COPY_TRAIT, capsys)
	diagnostics = payload.get("diagnostics", [])
	assert rc != 0, (
		f"expected compile failure — local `trait Copy` must not satisfy "
		f"`require T is Copy` on a core.Copy impl — but compilation succeeded "
		f"(rc={rc} diags={diagnostics})"
	)
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	assert any("Copy" in m for m in error_msgs), (
		f"expected a Copy-related rejection diagnostic, got: {error_msgs}"
	)


_GENERIC_COPY_PHANTOM = """
module main;

import std.core as core;

pub struct Handle<T> {
	pub id: Int
}

implement<T> core.Copy for Handle<T> {
}

fn main() nothrow -> Int {
	return 0;
}
""".lstrip()


def test_generic_copy_impl_accepted_for_phantom_type_param(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Positive case: phantom generic `Handle<T>` (no stored T) must
	be accepted without a require clause — T never appears in a
	stored field, so no T-as-Copy obligation is induced."""
	rc, payload = _run_driftc_json(tmp_path, _GENERIC_COPY_PHANTOM, capsys)
	diagnostics = payload.get("diagnostics", [])
	error_msgs = [d["message"] for d in diagnostics if d.get("severity") == "error"]
	copy_errors = [m for m in error_msgs if "Copy" in m]
	assert rc == 0 and not copy_errors, (
		f"expected clean compile for phantom generic Copy<Handle<T>>, "
		f"got rc={rc} errors={error_msgs}"
	)
