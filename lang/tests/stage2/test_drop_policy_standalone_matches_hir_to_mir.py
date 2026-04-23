# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Contract test: `compute_drop_policy(type_table, ty)` from
`lang.driftc.stage2.drop_policy_compute` MUST return identical
`DropPolicy` values to `HIRToMIR._drop_policy(ty)` for every canonical
type.

The two implementations are deliberate duplicates:
`HIRToMIR._drop_policy` is the canonical funnel inside HIR→MIR; the
standalone `compute_drop_policy` exists so post-HIR→MIR passes (Phase
3C drop-flag insertion; future 3B consumer swaps) can call the same
classification without instantiating a full `HIRToMIR` (which has
heavy `__init__` side effects — emits a `ConstString` into its
builder, etc.).

This test pins the equivalence on the same canonical types covered
by `test_drop_policy_contract.py`.  Any semantic change to
`HIRToMIR._drop_policy` MUST be mirrored in `compute_drop_policy`,
or this test fails loudly.
"""

from __future__ import annotations

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeId, TypeTable, VariantArmSchema, VariantFieldSchema
from lang.driftc.stage2 import HIRToMIR, make_builder
from lang.driftc.stage2.drop_policy_compute import compute_drop_policy
from lang.driftc.stage2.hir_to_mir import DropPolicy


def _hir_to_mir_policy(type_table: TypeTable, ty: TypeId) -> DropPolicy:
	builder = make_builder(FunctionId(module="main", name="main", ordinal=0))
	lower = HIRToMIR(builder, type_table=type_table)
	return lower._drop_policy(ty)


def _assert_match(type_table: TypeTable, ty: TypeId, label: str) -> None:
	canonical = _hir_to_mir_policy(type_table, ty)
	standalone = compute_drop_policy(type_table, ty)
	assert canonical == standalone, (
		f"compute_drop_policy diverged from HIRToMIR._drop_policy for "
		f"{label}: canonical={canonical} standalone={standalone}.  "
		f"The two implementations are duplicates and MUST stay in sync; "
		f"see `lang/driftc/stage2/drop_policy_compute.py` and "
		f"`HIRToMIR._drop_policy` in `lang/driftc/stage2/hir_to_mir.py`."
	)


def test_compute_drop_policy_matches_for_int() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_int(), "Int")


def test_compute_drop_policy_matches_for_bool() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_bool(), "Bool")


def test_compute_drop_policy_matches_for_byte() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_byte(), "Byte")


def test_compute_drop_policy_matches_for_string() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_string(), "String")


def test_compute_drop_policy_matches_for_unknown() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_unknown(), "Unknown")


def test_compute_drop_policy_matches_for_diagnostic_value() -> None:
	type_table = TypeTable()
	_assert_match(type_table, type_table.ensure_diagnostic_value(), "DiagnosticValue")


def test_compute_drop_policy_matches_for_optional_int_variant() -> None:
	"""POD-payload variant — exercises the variant-walk path of
	`_contains_dv_transitive` and the structural drop axis."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	v_int_ty = type_table.ensure_instantiated(var_base, [int_ty])
	_assert_match(type_table, v_int_ty, "V<Int>")


def test_compute_drop_policy_matches_for_optional_string_variant() -> None:
	"""Refcount-bearing variant payload — exercises the
	`Optional<String>` shape that motivated the Phase 2a UAF fix.
	Pinning equivalence here is critical because Phase 2a's
	`has_structural_drop` axis is the gate the bug shape pivoted on."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])
	_assert_match(type_table, v_string_ty, "V<String>")


def test_compute_drop_policy_matches_under_copy_hook_shortcut() -> None:
	"""Copy-trait shortcut — pins that `compute_drop_policy` mirrors
	the `copy_status=True → needs_drop=False` shortcut that's the
	pre-Phase-1 behaviour the funnel preserves.  This is the bug
	shape Phase 2a fixed; both implementations must report the same
	(intentionally-buggy-in-isolation) classification under the
	hook."""
	type_table = TypeTable()
	string_ty = type_table.ensure_string()
	var_base = type_table.declare_variant(
		module_id="main",
		name="V",
		type_params=["T"],
		arms=[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	v_string_ty = type_table.ensure_instantiated(var_base, [string_ty])
	type_table.set_copy_query(lambda tid: tid == v_string_ty, allow_fallback=True)
	_assert_match(type_table, v_string_ty, "V<String> with Copy hook")


def test_compute_drop_policy_matches_for_destructible_struct() -> None:
	"""User-Destructible struct (has destructor_fns entry) — exercises
	the `is_destructible` axis."""
	type_table = TypeTable()
	int_ty = type_table.ensure_int()
	arc_tid = type_table.declare_struct(module_id="main", name="MyArc", field_names=["inner"])
	type_table.define_struct_fields(arc_tid, field_types=[int_ty])
	destroy_fn = FunctionId(module="main", name="MyArc::destroy", ordinal=0)
	type_table.destructor_fns = {arc_tid: destroy_fn}
	# Non-Copy so the shortcut doesn't kick in.
	non_copy = {arc_tid}
	type_table._copy_query = lambda tid: False if tid in non_copy else None  # type: ignore[attr-defined]
	_assert_match(type_table, arc_tid, "MyArc (Destructible struct)")
