# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Phase 3 of the terminal-`throws` work: package metadata round-trip for the
`declared_throws` (auto-try value-returning) and `declared_terminal_throws`
(bare terminal) flags.

Phase 1 v3 added both flags as in-memory state on the parser AST,
`_FrontendDecl`, `FnSignature`, and `InterfaceMethodSchema`. Phase 2 added
the body-flow and call-site enforcement on `declared_terminal_throws`. But
neither flag was wired through the package serialization layer:

  - `provisional_dmir_v0.encode_signatures` and the FnSignature decoder in
    `driftc.py` only round-tripped `declared_can_throw` (the boundary ABI
    flag, True iff non-nothrow). Both `declared_throws` and
    `declared_terminal_throws` were dropped, defaulting to False on the
    consumer side.
  - The `InterfaceMethodSchema` encoder/decoder in `provisional_dmir_v0.py`
    + `type_table_link_v0.py` only round-tripped `declared_nothrow`. Both
    new flags were dropped.
  - The trait method definition encoder/decoder in `driftc.py` (the iface
    payload) round-tripped `name`, `type_params`, `params`, `return_type`,
    `require`, `span` — but not `declared_nothrow`, `declared_throws`, or
    `declared_terminal_throws`.

Phase 3 closes all four sites for `declared_throws` and
`declared_terminal_throws`. The `declared_nothrow` gap on trait methods is
also closed because it lives in the same encoder dict and the existing
trait/impl matching at `type_checker.py:1349` reads it.

These tests pin the round-trip directly via `encode_type_table` /
`decode_type_table_obj` (the same path the existing
`test_interface_schema_roundtrip.py` uses) plus a higher-level
`encode_signatures` / FnSignature reconstruction test.
"""
from __future__ import annotations

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import (
	InterfaceMethodSchema,
	InterfaceParamSchema,
	TypeTable,
)
from lang.driftc.packages.provisional_dmir_v0 import (
	encode_signatures,
	encode_type_table,
)
from lang.driftc.packages.type_table_link_v0 import decode_type_table_obj


# ---------------------------------------------------------------------------
# InterfaceMethodSchema round-trip
# ---------------------------------------------------------------------------


def test_interface_method_declared_throws_round_trips() -> None:
	"""`InterfaceMethodSchema.declared_throws=True` (auto-try value-returning
	form) survives encode/decode through provisional_dmir_v0."""
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base_id = table.declare_interface("m", "Callback1", ["A", "R"])
	method = InterfaceMethodSchema(
		name="call",
		params=[InterfaceParamSchema(name="x", type_expr=GenericTypeExpr.param(0))],
		return_type=GenericTypeExpr.param(1),
		type_params=[],
		declared_nothrow=False,
		is_unsafe=False,
		declared_throws=True,
		declared_terminal_throws=False,
	)
	table.define_interface_schema_methods(base_id, [method])

	encoded = encode_type_table(table, package_id="pkgA")
	decoded = decode_type_table_obj(encoded)
	keys = [k for k in decoded.interface_schemas.keys() if k.module_id == "m" and k.name == "Callback1"]
	assert len(keys) == 1
	_type_params, methods, _parents, _base_id = decoded.interface_schemas[keys[0]]
	assert len(methods) == 1
	out = methods[0]
	assert out.declared_throws is True, (
		f"REGRESSION: declared_throws=True dropped on round-trip; got {out.declared_throws!r}"
	)
	assert out.declared_terminal_throws is False
	assert out.declared_nothrow is False


def test_interface_method_declared_terminal_throws_round_trips() -> None:
	"""`InterfaceMethodSchema.declared_terminal_throws=True` (bare terminal
	form) survives encode/decode. The terminal form has `return_type=None` —
	Phase 1 v3 made the schema field Optional and the encoder writes null
	for it. Phase 3 now also round-trips the flag itself."""
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base_id = table.declare_interface("m", "Boomer", [])
	method = InterfaceMethodSchema(
		name="boom",
		params=[],
		return_type=None,  # bare terminal form has no return type
		type_params=[],
		declared_nothrow=False,
		is_unsafe=False,
		declared_throws=False,
		declared_terminal_throws=True,
	)
	table.define_interface_schema_methods(base_id, [method])

	encoded = encode_type_table(table, package_id="pkgA")
	decoded = decode_type_table_obj(encoded)
	keys = [k for k in decoded.interface_schemas.keys() if k.module_id == "m" and k.name == "Boomer"]
	assert len(keys) == 1
	_type_params, methods, _parents, _base_id = decoded.interface_schemas[keys[0]]
	assert len(methods) == 1
	out = methods[0]
	assert out.declared_terminal_throws is True, (
		f"REGRESSION: declared_terminal_throws=True dropped on round-trip; "
		f"got {out.declared_terminal_throws!r}"
	)
	assert out.declared_throws is False
	assert out.declared_nothrow is False
	# Phase 1 v3 invariant: terminal form has return_type=None, no Void synthesis.
	assert out.return_type is None, (
		f"REGRESSION: terminal-throws schema return_type should be None after "
		f"round-trip; got {out.return_type!r}"
	)


def test_interface_method_neither_flag_round_trips() -> None:
	"""Sanity: an interface method with neither `throws` flag set should
	round-trip with both flags False (forward-compat with packages built
	before Phase 3 — additive new fields default to False on missing)."""
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base_id = table.declare_interface("m", "Plain", [])
	method = InterfaceMethodSchema(
		name="quiet",
		params=[],
		return_type=GenericTypeExpr.named("Int", []),
		type_params=[],
		declared_nothrow=True,
		is_unsafe=False,
		# Both throws flags default to False
	)
	table.define_interface_schema_methods(base_id, [method])

	encoded = encode_type_table(table, package_id="pkgA")
	decoded = decode_type_table_obj(encoded)
	keys = [k for k in decoded.interface_schemas.keys() if k.module_id == "m" and k.name == "Plain"]
	assert len(keys) == 1
	_type_params, methods, _parents, _base_id = decoded.interface_schemas[keys[0]]
	out = methods[0]
	assert out.declared_throws is False
	assert out.declared_terminal_throws is False
	assert out.declared_nothrow is True


# ---------------------------------------------------------------------------
# FnSignature round-trip via encode_signatures
# ---------------------------------------------------------------------------


def test_fn_signature_declared_throws_round_trips_in_dmir_payload() -> None:
	"""`FnSignature.declared_throws=True` is encoded into the dmir payload
	by `encode_signatures`. We pin the encoded dict shape directly here
	rather than reconstructing through the consumer-side decoder (which
	lives in driftc.py and is harder to invoke in isolation). The decoder
	test is at the consumer integration level via the existing package-
	consumer e2e cases."""
	table = TypeTable()
	int_ty = table.ensure_int()
	sig = FnSignature(
		name="m::auto_try_fn",
		module="m",
		param_type_ids=[],
		return_type_id=int_ty,
		declared_can_throw=True,
		declared_throws=True,
		declared_terminal_throws=False,
		is_pub=True,
	)
	encoded = encode_signatures({"m::auto_try_fn": sig}, module_id="m", type_table=table)
	assert "m::auto_try_fn" in encoded
	entry = encoded["m::auto_try_fn"]
	assert entry.get("declared_throws") is True, (
		f"REGRESSION: declared_throws=True missing from encoded payload; entry keys={list(entry.keys())}"
	)
	assert entry.get("declared_terminal_throws") is False
	assert entry.get("declared_can_throw") is True


def test_fn_signature_declared_terminal_throws_round_trips_in_dmir_payload() -> None:
	"""`FnSignature.declared_terminal_throws=True` is encoded into the dmir
	payload by `encode_signatures`. Bare terminal form."""
	table = TypeTable()
	sig = FnSignature(
		name="m::fail",
		module="m",
		param_type_ids=[],
		return_type_id=None,  # terminal form has no return type
		declared_can_throw=True,
		declared_throws=False,
		declared_terminal_throws=True,
		is_pub=True,
	)
	encoded = encode_signatures({"m::fail": sig}, module_id="m", type_table=table)
	assert "m::fail" in encoded
	entry = encoded["m::fail"]
	assert entry.get("declared_terminal_throws") is True, (
		f"REGRESSION: declared_terminal_throws=True missing from encoded payload; "
		f"entry keys={list(entry.keys())}"
	)
	assert entry.get("declared_throws") is False
	assert entry.get("declared_can_throw") is True


def test_fn_signature_neither_flag_round_trips_in_dmir_payload() -> None:
	"""Sanity: a plain may-throw function with neither flag set encodes both
	as False (forward-compat baseline)."""
	table = TypeTable()
	int_ty = table.ensure_int()
	sig = FnSignature(
		name="m::plain",
		module="m",
		param_type_ids=[],
		return_type_id=int_ty,
		declared_can_throw=True,
		declared_throws=False,
		declared_terminal_throws=False,
		is_pub=True,
	)
	encoded = encode_signatures({"m::plain": sig}, module_id="m", type_table=table)
	entry = encoded["m::plain"]
	assert entry.get("declared_throws") is False
	assert entry.get("declared_terminal_throws") is False


# ---------------------------------------------------------------------------
# Forward-compat: payloads without the new fields decode as False.
# ---------------------------------------------------------------------------


def test_interface_method_decoder_defaults_missing_flags_to_false() -> None:
	"""Forward-compat: an old encoded payload (no Phase 3 flags) must decode
	with both flags defaulting to False so that consumers built against an
	older toolchain can still load Phase 3-aware packages."""
	# Build a real payload via encode, then strip the new fields to simulate
	# an old payload, then decode and verify defaults.
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base_id = table.declare_interface("m", "OldStyle", [])
	method = InterfaceMethodSchema(
		name="m",
		params=[],
		return_type=GenericTypeExpr.named("Int", []),
		type_params=[],
		declared_nothrow=False,
		is_unsafe=False,
		declared_throws=True,
		declared_terminal_throws=True,
	)
	table.define_interface_schema_methods(base_id, [method])
	encoded = encode_type_table(table, package_id="pkgA")
	# Walk the encoded payload and strip the new flags from interface schemas
	# to simulate an old-format package.
	for schema in encoded.get("interface_schemas", []):
		for m in schema.get("methods", []):
			m.pop("declared_throws", None)
			m.pop("declared_terminal_throws", None)
	decoded = decode_type_table_obj(encoded)
	keys = [k for k in decoded.interface_schemas.keys() if k.module_id == "m" and k.name == "OldStyle"]
	out = decoded.interface_schemas[keys[0]][1][0]
	assert out.declared_throws is False, (
		f"forward-compat: missing declared_throws field should default to False; "
		f"got {out.declared_throws!r}"
	)
	assert out.declared_terminal_throws is False
