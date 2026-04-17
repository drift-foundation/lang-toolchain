# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
LANGUAGE_BUG regression — unit-test scope: `TypeTable.
_eval_generic_type_expr` must evaluate
`GenericTypeExpr(name="fn", ...)` into a proper
`TypeKind.FUNCTION` TypeId instead of falling through to the
nominal-name branch and returning `Unknown`.

`_eval_generic_type_expr` is the schema-deserialize entry point
used by `lang/driftc/packages/type_table_link_v0.py::
import_type_tables_and_build_typeid_maps` when linking struct
field types for a consumed package. Before the fix, struct fields
whose type is a function pointer — e.g.
`ArcHeader.drop_thunk: Fn(mem.Ptr<Byte>) nothrow -> Void` in
`stdlib/std/concurrent/concurrent.drift` — degraded to `Unknown`
on the consumer side, producing either of two cascading failures:

  Variant A — same-process schema rebind:
    ValueError: struct 'std.concurrent::ArcHeader' fields already
      defined: [5, 5, 3] vs [5, 5, 1218]
  Variant B — consumer-side field `Unknown`:
    <source>:525:4: error: cannot copy 'thunk': type 'Unknown' ...
    <source>:525:9: error: call target is not a function value

**Scope of this file: direct unit-test on the schema evaluator.**
It calls `TypeTable._eval_generic_type_expr` with
`GenericTypeExpr(name="fn", ...)` shapes and asserts the result
is a real function type (param types, return type, nothrow flag
preserved) rather than `Unknown`, and that the schema-deserialize
path yields the SAME host TypeId as the source-parse path — that
cross-path TypeId equality is the exact invariant whose violation
caused the `fields already defined: [5, 5, 3] vs [5, 5, 1218]`
ValueError.

The end-to-end package-roundtrip pins for this bug live
elsewhere: `test_stdlib_as_package::test_arc_scope_drop_no_leak`
plus every `test_pkg_*` test in the Stage 2 ArcHeader cluster.
Those exercise the full compile+consume path with real struct
fields whose Fn type references cross-module types (e.g.
`mem.Ptr<Byte>` from `std.mem`) — the shape that actually routes
through `_eval_generic_type_expr` during package linking.  This
file does NOT build or consume a package; it pins the root-cause
function directly so the regression is still meaningful and fast
to run in isolation.
"""
from __future__ import annotations

import pytest

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeKind, TypeTable


def _fn_expr_nothrow_byte_to_void() -> GenericTypeExpr:
	"""Build `Fn(Byte) nothrow -> Void` as a schema-time expr."""
	return GenericTypeExpr.named(
		name="fn",
		args=[
			GenericTypeExpr.named(name="Byte"),
			GenericTypeExpr.named(name="Void"),
		],
		fn_throws=False,
	)


def _fn_expr_throws_int_to_int() -> GenericTypeExpr:
	"""Build `Fn(Int) -> Int` (may-throw) as a schema-time expr."""
	return GenericTypeExpr.named(
		name="fn",
		args=[
			GenericTypeExpr.named(name="Int"),
			GenericTypeExpr.named(name="Int"),
		],
		fn_throws=True,
	)


def _fn_expr_zero_args_to_int() -> GenericTypeExpr:
	"""Build `Fn() nothrow -> Int` — no params, return only."""
	return GenericTypeExpr.named(
		name="fn",
		args=[GenericTypeExpr.named(name="Int")],
		fn_throws=False,
	)


def test_eval_generic_type_expr_handles_fn_nothrow() -> None:
	"""`_eval_generic_type_expr` on a `name="fn"` expr must produce a
	`TypeKind.FUNCTION` TypeId — not `Unknown`.  This is the
	specific code path that degrades `ArcHeader.drop_thunk` on the
	package-consumer side when the schema is linked."""
	tt = TypeTable()
	expr = _fn_expr_nothrow_byte_to_void()

	tid = tt._eval_generic_type_expr(expr, [], module_id="test.mod")

	td = tt.get(tid)
	assert td.kind is TypeKind.FUNCTION, (
		f"expected TypeKind.FUNCTION, got {td.kind} (this is the "
		f"exact symptom: schema-deserialize Fn field returned "
		f"{td.kind} instead of FUNCTION)"
	)
	# Two slots: one param (Byte) + return (Void).
	assert len(td.param_types) == 2, (
		f"expected 2 param-slot types (1 param + 1 return), got "
		f"{len(td.param_types)}"
	)
	byte_id = tt.ensure_byte()
	void_id = tt.ensure_void()
	assert td.param_types[0] == byte_id, "param[0] type mismatch"
	assert td.param_types[1] == void_id, "return type mismatch"
	assert td.fn_throws is False, (
		f"nothrow flag not preserved: expected False, got "
		f"{td.fn_throws}"
	)


def test_eval_generic_type_expr_handles_fn_throws() -> None:
	"""The may-throw flag must round-trip too — `Fn(Int) -> Int`
	without the `nothrow` marker serializes with `fn_throws=True`
	and must come back the same."""
	tt = TypeTable()
	tid = tt._eval_generic_type_expr(
		_fn_expr_throws_int_to_int(), [], module_id="test.mod"
	)
	td = tt.get(tid)
	assert td.kind is TypeKind.FUNCTION
	assert td.fn_throws is True, (
		"throws flag dropped: expected True for `Fn(Int) -> Int`"
	)


def test_eval_generic_type_expr_handles_fn_zero_args() -> None:
	"""Zero-arg function type: `args` is `[ret_expr]` only.  The
	fix must still pick up the return type correctly when there
	are no param exprs."""
	tt = TypeTable()
	tid = tt._eval_generic_type_expr(
		_fn_expr_zero_args_to_int(), [], module_id="test.mod"
	)
	td = tt.get(tid)
	assert td.kind is TypeKind.FUNCTION
	assert len(td.param_types) == 1, (
		"zero-arg fn type should have exactly 1 slot (just the return)"
	)
	assert td.param_types[0] == tt.ensure_int()
	assert td.fn_throws is False


def test_fn_field_produces_same_typeid_as_source_path() -> None:
	"""Cross-path consistency: the same `Fn(Byte) nothrow -> Void`
	shape evaluated via `_eval_generic_type_expr` (schema-deserialize)
	must yield the SAME host TypeId as going through
	`TypeTable.ensure_function` directly (source-parse).  If these
	diverge, the consumer and the source-parse disagree on the
	field type and `define_struct_fields` rejects the rebind
	(Variant A: `fields already defined: [5, 5, 3] vs [5, 5, 1218]`)."""
	tt = TypeTable()
	# Source-parse path: build the Fn TypeId directly.
	byte_id = tt.ensure_byte()
	void_id = tt.ensure_void()
	source_fn_id = tt.ensure_function(
		[byte_id], void_id, can_throw=False
	)

	# Schema-deserialize path: walk the GenericTypeExpr.
	schema_fn_id = tt._eval_generic_type_expr(
		_fn_expr_nothrow_byte_to_void(), [], module_id="test.mod"
	)

	assert source_fn_id == schema_fn_id, (
		f"Fn TypeId mismatch between source and schema paths: "
		f"source={source_fn_id} schema={schema_fn_id}.  This is the "
		f"direct cause of the ArcHeader 'fields already defined' "
		f"ValueError on the package-consumer path."
	)


@pytest.mark.parametrize("throws", [False, True])
def test_fn_field_typeid_is_stable_across_throws_variants(throws: bool) -> None:
	"""Separate regressions for nothrow-vs-throws to make sure the
	cross-path TypeId match holds in both directions."""
	tt = TypeTable()
	int_id = tt.ensure_int()
	source_fn_id = tt.ensure_function([int_id], int_id, can_throw=throws)
	schema_fn_id = tt._eval_generic_type_expr(
		GenericTypeExpr.named(
			name="fn",
			args=[
				GenericTypeExpr.named(name="Int"),
				GenericTypeExpr.named(name="Int"),
			],
			fn_throws=throws,
		),
		[],
		module_id="test.mod",
	)
	assert source_fn_id == schema_fn_id
