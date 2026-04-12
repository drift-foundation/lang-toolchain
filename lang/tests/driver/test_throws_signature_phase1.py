# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 1 v3 of the terminal-`throws` work: dual-form grammar/AST/signature plumbing.

CONTEXT: Phase 1 v1 and v2 attempted to make `fn f() throws -> T` a parser-level
error and treat `throws` as a single new "terminal" keyword. That was wrong: the
existing `throws -> T` form has real semantic load — it sets a body-wide
auto-`Try::into_try` context for any `Result<X, E>` expression in the function
body (see `lang/driftc/type_checker.py:_should_auto_try`). Removing that
feature would break ~18 existing source files and contradict the user's
ergonomics direction.

Phase 1 v3 preserves the auto-try form and ADDS the new bare terminal form
alongside it. The four legal signature shapes are:

  fn f(...) -> T              plain may-throw value return, no auto-try
  fn f(...) nothrow -> T      non-throwing value return
  fn f(...) throws -> T       may-throw value return WITH body-wide auto-try
                              (existing behavior, preserved). Sets
                              declared_throws=True.
  fn f(...) throws            NEW: terminal throw-only form. Function never
                              returns normally. Sets
                              declared_terminal_throws=True. The body must
                              terminate via `throw` or tail-call to another
                              terminal-throws function — Phase 2 enforces this.

Mutual exclusion: `nothrow` cannot combine with either `throws` form.

This file exercises ONLY the signature shape contract and the in-memory
plumbing. It does NOT exercise:
  - Phase 2 body-flow enforcement for the bare terminal form (terminal-flow
    walk for the new form).
  - Phase 3 package metadata round-trip of declared_terminal_throws.
  - Phase 4 the std.core.Throw trait or any Try/or_throw rebind.

Each positive test introspects the LOWERED signature (FnSignature or
InterfaceMethodSchema) via `parse_drift_workspace_to_hir` and asserts the
right flag is set, not just `rc == 0`. Asserting `rc == 0` alone hides flag-
drop bugs like the `_FrontendDecl` regression caught in v2 review.
"""
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc(tmp_path: Path, capsys, source: str, module_name: str = "m_main") -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / module_name / "main.drift"
	_write_file(main_path, source)
	rc = driftc_main(["-M", str(root), str(main_path), "--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _diag_messages(payload: dict) -> list[str]:
	diags = payload.get("diagnostics") or []
	return [str(d.get("message", "")) for d in diags]


def _lower_workspace(tmp_path: Path, source: str, *, module_name: str = "m") -> dict:
	"""
	Run the parser/lowering pipeline on a single Drift source and return a dict
	with the lowered modules and the type table.
	"""
	root = tmp_path / "mods"
	main_path = root / module_name / "main.drift"
	_write_file(main_path, source)
	modules, type_table, _exc_catalog, _exports, _deps, parse_diags = parse_drift_workspace_to_hir(
		[main_path],
		module_paths=[root],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	return {
		"modules": modules,
		"type_table": type_table,
		"parse_diags": parse_diags,
	}


def _find_signature_by_name(modules, name: str):
	"""Return the first lowered FnSignature whose name (or fn_id name or
	method_name) matches `name`."""
	for mod in modules.values():
		for fn_id, sig in mod.signatures_by_id.items():
			if sig.name == name or getattr(sig, "method_name", None) == name:
				return sig
			if fn_id.name == name:
				return sig
	return None


# ---------------------------------------------------------------------------
# Positive tests: the four legal signature shapes parse cleanly and the
# lowered FnSignature carries the right flags.
# ---------------------------------------------------------------------------


def test_plain_may_throw_function_no_throws_flags(tmp_path: Path, capsys) -> None:
	"""`fn f() -> Int { throw E(); }` — plain may-throw, no auto-try, neither
	`declared_throws` nor `declared_terminal_throws` set."""
	source = """
module m;

exception Boom()

fn fail() -> Int {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == [], f"parse diags: {out['parse_diags']}"
	sig = _find_signature_by_name(out["modules"], "fail")
	assert sig is not None, "lowered signature for `fail` not found"
	assert sig.declared_throws is False, f"plain may-throw should not set declared_throws; got {sig.declared_throws!r}"
	assert sig.declared_terminal_throws is False, (
		f"plain may-throw should not set declared_terminal_throws; got {sig.declared_terminal_throws!r}"
	)


def test_nothrow_value_return_no_throws_flags(tmp_path: Path, capsys) -> None:
	"""`fn f() nothrow -> Int { return 0; }` — non-throwing value return, both
	throws flags False, declared_nothrow True."""
	source = """
module m;

fn quiet() nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	return quiet();
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "quiet")
	assert sig is not None
	assert sig.declared_throws is False
	assert sig.declared_terminal_throws is False
	# declared_can_throw is the boundary ABI flag — for nothrow it's False.
	assert sig.declared_can_throw is False, f"nothrow should set declared_can_throw=False; got {sig.declared_can_throw!r}"


def test_auto_try_value_return_sets_declared_throws(tmp_path: Path, capsys) -> None:
	"""`fn f() throws -> Int { ... }` — the EXISTING auto-try value-returning
	form. Sets `declared_throws=True` and leaves `declared_terminal_throws=False`.
	The body-wide auto-try context is what makes Result-typed expressions
	auto-unwrap via `Try::into_try`. This test pins that the flag flows through;
	the auto-try semantic itself is exercised by the existing
	`std_net_tcp_stress_connections_with_try` e2e and the trait-impl tests
	below.
	"""
	source = """
module m;

import std.core as core;
use trait core.Try;

exception Boom()

fn fail() throws -> Int {
	throw Boom();
}

fn main() nothrow -> Int {
	return try fail() catch { 1 };
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "fail")
	assert sig is not None, "lowered signature for `fail` not found"
	assert sig.declared_throws is True, (
		f"`throws -> T` form should set declared_throws=True; got {sig.declared_throws!r}"
	)
	assert sig.declared_terminal_throws is False, (
		f"`throws -> T` form must NOT set declared_terminal_throws; got {sig.declared_terminal_throws!r}"
	)


def test_bare_terminal_throws_sets_declared_terminal_throws(tmp_path: Path, capsys) -> None:
	"""`fn f() throws { throw E(); }` — NEW Phase 1 bare terminal form. Sets
	`declared_terminal_throws=True`, leaves `declared_throws=False`. Phase 0's
	missing-return checker must NOT false-positive — terminal-throws functions
	have no value return type to satisfy.
	"""
	source = """
module m;

exception Boom()

fn fail() throws {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"bare terminal throws should parse cleanly; payload={payload}"
	for msg in _diag_messages(payload):
		assert "must return a value" not in msg, f"Phase 0 false-positive on terminal throws: {msg}"

	out = _lower_workspace(tmp_path / "lower", source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "fail")
	assert sig is not None
	assert sig.declared_terminal_throws is True, (
		f"bare `throws` form should set declared_terminal_throws=True; got {sig.declared_terminal_throws!r}"
	)
	assert sig.declared_throws is False, (
		f"bare `throws` form must NOT set declared_throws (that's the auto-try form); "
		f"got {sig.declared_throws!r}"
	)


def test_pub_bare_terminal_throws_signature_carries_flag(tmp_path: Path, capsys) -> None:
	"""`pub fn f() throws { ... }` — pub modifier doesn't interfere with the
	terminal flag plumbing."""
	source = """
module m;

exception Boom()

pub fn fail() throws {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "fail")
	assert sig is not None
	assert sig.declared_terminal_throws is True
	assert sig.declared_throws is False
	assert sig.is_pub is True


def test_implement_block_bare_terminal_throws_method_carries_flag(tmp_path: Path, capsys) -> None:
	"""`implement Foo { pub fn bar(self: &Foo) throws { ... } }`.

	Pins the v2 reviewer's `_FrontendDecl` flag-drop fix for the new
	`declared_terminal_throws` flag too — the impl-block code path in
	`lang/driftc/parser/__init__.py` must pass BOTH flags through to the
	lowered signature, not just `declared_nothrow`.
	"""
	source = """
module m;

exception Boom()

pub struct Foo { v: Int }

implement Foo {
	pub fn bust(self: &Foo) throws {
		throw Boom();
	}
}

fn main() nothrow -> Int {
	return 0;
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "bust")
	assert sig is not None, "impl-block method `bust` lowered signature not found"
	assert sig.declared_terminal_throws is True, (
		f"REGRESSION: impl-block method `Foo::bust() throws` lost declared_terminal_throws "
		f"in lowering. Got {sig.declared_terminal_throws!r}. "
		f"See _FrontendDecl call site in lang/driftc/parser/__init__.py."
	)
	assert sig.declared_throws is False
	assert sig.is_method is True


def test_implement_block_auto_try_method_carries_flag(tmp_path: Path, capsys) -> None:
	"""`implement Foo { pub fn bar(self: &Foo) throws -> Int { ... } }` —
	the auto-try value-returning form on an impl-block method. Pins the
	v2 reviewer fix for `declared_throws` on impl-block methods.
	"""
	source = """
module m;

exception Boom()

pub struct Foo { v: Int }

implement Foo {
	pub fn bust(self: &Foo) throws -> Int {
		throw Boom();
	}
}

fn main() nothrow -> Int {
	return try Foo(v = 1).bust() catch { 1 };
}
"""
	out = _lower_workspace(tmp_path, source)
	assert out["parse_diags"] == []
	sig = _find_signature_by_name(out["modules"], "bust")
	assert sig is not None
	assert sig.declared_throws is True, (
		f"REGRESSION: impl-block method `Foo::bust() throws -> Int` lost declared_throws. "
		f"Got {sig.declared_throws!r}."
	)
	assert sig.declared_terminal_throws is False
	assert sig.is_method is True


def test_trait_method_bare_terminal_throws_parses(tmp_path: Path, capsys) -> None:
	"""`trait T { fn f(self: &Self) throws; }` — trait method declared as
	bare terminal `throws`. Pins the parser AST flag on TraitMethodSig."""
	source = """
module m;

pub trait Boomable {
	fn boom(self: &Self) throws;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"trait method declared throws should parse cleanly; payload={payload}"
	from lang.driftc.parser import parser as p

	prog = p.parse_program(source)
	traits = list(getattr(prog, "traits", []) or [])
	if not traits:
		traits = list(getattr(prog, "trait_defs", []) or [])
	boomable = next((t for t in traits if t.name == "Boomable"), None)
	assert boomable is not None, f"trait Boomable not found in parsed program"
	boom_method = next((m for m in boomable.methods if m.name == "boom"), None)
	assert boom_method is not None
	assert boom_method.declared_terminal_throws is True, (
		f"parser TraitMethodSig.declared_terminal_throws should be True; "
		f"got {boom_method.declared_terminal_throws!r}"
	)
	assert boom_method.declared_throws is False


def test_trait_method_auto_try_throws_returns_int(tmp_path: Path, capsys) -> None:
	"""`trait T { fn f(self: &Self) throws -> Int; }` — trait method declared
	in the auto-try value-returning form. Pins the parser AST flag."""
	source = """
module m;

pub trait Boomable {
	fn boom(self: &Self) throws -> Int;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"trait method `throws -> Int` should parse cleanly; payload={payload}"
	from lang.driftc.parser import parser as p

	prog = p.parse_program(source)
	traits = list(getattr(prog, "traits", []) or [])
	if not traits:
		traits = list(getattr(prog, "trait_defs", []) or [])
	boomable = next((t for t in traits if t.name == "Boomable"), None)
	assert boomable is not None
	boom_method = next((m for m in boomable.methods if m.name == "boom"), None)
	assert boom_method is not None
	assert boom_method.declared_throws is True, (
		f"parser TraitMethodSig.declared_throws should be True; got {boom_method.declared_throws!r}"
	)
	assert boom_method.declared_terminal_throws is False


def test_interface_method_bare_terminal_throws_flows_to_schema(tmp_path: Path, capsys) -> None:
	"""`interface I { fn f(self: &Self) throws; }` — interface method declared
	bare terminal. Pins both: (1) the flag flows through to
	InterfaceMethodSchema and (2) the schema's `return_type` is None for the
	terminal form (no Void synthesis).
	"""
	source = """
module m;

pub interface Boomer {
	fn boom(self: &Self) throws;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"interface method declared throws should parse cleanly; payload={payload}"

	out = _lower_workspace(tmp_path / "lower", source)
	assert out["parse_diags"] == []
	type_table = out["type_table"]
	boomer_schema = None
	for _base_id, schema in type_table.interface_bases.items():
		if schema.name == "Boomer" and schema.module_id == "m":
			boomer_schema = schema
			break
	assert boomer_schema is not None, (
		f"interface schema for `m::Boomer` not found; "
		f"keys={list(type_table.interface_bases.keys())}"
	)
	boom_method = next((mth for mth in boomer_schema.methods if mth.name == "boom"), None)
	assert boom_method is not None
	assert boom_method.declared_terminal_throws is True, (
		f"REGRESSION: interface method `Boomer::boom() throws` lost "
		f"declared_terminal_throws in InterfaceMethodSchema. "
		f"Got {boom_method.declared_terminal_throws!r}."
	)
	assert boom_method.declared_throws is False
	# return_type must be None for the terminal form (no Void synthesis).
	assert boom_method.return_type is None, (
		f"REGRESSION: terminal-throws interface method should have "
		f"return_type=None on the schema (no Void synthesis). "
		f"Got {boom_method.return_type!r}."
	)


def test_interface_method_auto_try_throws_returns_int(tmp_path: Path, capsys) -> None:
	"""`interface I { fn f(self: &Self) throws -> Int; }` — interface method
	declared with the auto-try value-returning form."""
	source = """
module m;

pub interface Boomer {
	fn boom(self: &Self) throws -> Int;
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc == 0, f"interface method `throws -> Int` should parse cleanly; payload={payload}"

	out = _lower_workspace(tmp_path / "lower", source)
	assert out["parse_diags"] == []
	type_table = out["type_table"]
	boomer_schema = None
	for _base_id, schema in type_table.interface_bases.items():
		if schema.name == "Boomer" and schema.module_id == "m":
			boomer_schema = schema
			break
	assert boomer_schema is not None
	boom_method = next((mth for mth in boomer_schema.methods if mth.name == "boom"), None)
	assert boom_method is not None
	assert boom_method.declared_throws is True
	assert boom_method.declared_terminal_throws is False
	assert boom_method.return_type is not None, (
		f"value-returning interface method should have a populated return_type; "
		f"got None"
	)


# ---------------------------------------------------------------------------
# Negative tests: structural signature-shape rejections.
# ---------------------------------------------------------------------------


def test_nothrow_throws_combination_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn f() nothrow throws { ... }` — `nothrow` and any `throws` form are
	mutually exclusive at the grammar level."""
	source = """
module m;

exception Boom()

fn fail() nothrow throws {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"nothrow + throws should be rejected; payload={payload}"


def test_nothrow_throws_with_return_type_is_rejected(tmp_path: Path, capsys) -> None:
	"""`fn f() nothrow throws -> Int { ... }` — `nothrow` and `throws -> T`
	are also mutually exclusive."""
	source = """
module m;

exception Boom()

fn fail() nothrow throws -> Int {
	throw Boom();
}

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"nothrow + throws + return type should be rejected; payload={payload}"


def test_intrinsic_bare_terminal_throws_is_rejected(tmp_path: Path, capsys) -> None:
	"""`@intrinsic fn f() throws;` — bare terminal form on intrinsic is
	rejected because intrinsics have no body for terminal-flow enforcement.

	Note: `@intrinsic fn f() throws -> Int;` (the auto-try value-returning
	form) is ALLOWED on intrinsics — auto-try is a no-op there since
	intrinsics have no body, and removing support would regress existing
	intrinsic declarations.
	"""
	source = """
module m;

@intrinsic fn boom() throws;

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"@intrinsic + bare terminal throws should be rejected; payload={payload}"
	msgs = _diag_messages(payload)
	assert any("intrinsic" in m.lower() and "throws" in m.lower() for m in msgs), (
		f"diagnostic should mention both 'intrinsic' and 'throws'; msgs={msgs}"
	)


def test_extern_c_bare_terminal_throws_is_rejected(tmp_path: Path, capsys) -> None:
	"""`extern "C" fn f() throws;` — extern C cannot use terminal throws.
	The `extern_fn` grammar rule already requires `NOTHROW` (no THROWS slot),
	so this is structurally rejected. Pinned by this test so a future
	grammar relaxation cannot quietly break the contract.
	"""
	source = """
module m;

extern "C" fn raise_signal() throws;

fn main() nothrow -> Int {
	return 0;
}
"""
	rc, payload = _run_driftc(tmp_path, capsys, source)
	assert rc != 0, f"extern C + bare terminal throws should be rejected; payload={payload}"
