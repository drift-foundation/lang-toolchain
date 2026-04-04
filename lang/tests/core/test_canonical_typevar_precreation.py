# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Regression: declare_struct/declare_variant/declare_interface must pre-create
canonical TypeVars with correct display names for their type parameters.

Without pre-creation, the first ensure_typevar call for a TypeParamId from
a different package's impl block produces a renamed TypeVar (T→T0) which
breaks trait solver unification across package boundaries.

This regression covers variant and interface pre-creation in addition to
the struct path that was already covered by test_linker_typevar_dedup.
"""
from __future__ import annotations

from lang.driftc.core.types_core import (
	TypeId,
	TypeKind,
	TypeTable,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeParamId


# ---------------------------------------------------------------------------
# Variant canonical TypeVar pre-creation
# ---------------------------------------------------------------------------

def test_variant_declare_creates_canonical_type_param_ids():
	"""declare_variant with type_params must populate variant_type_param_ids."""
	tt = TypeTable()
	base = tt.declare_variant(
		"pkg.mod", "Result", ["T", "E"],
		[
			VariantArmSchema(name="Ok", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
			VariantArmSchema(name="Err", fields=[VariantFieldSchema(name="error", type_expr=GenericTypeExpr.param(1))]),
		],
	)
	tpids = tt.variant_type_param_ids.get(base)
	assert tpids is not None, "variant_type_param_ids not populated"
	assert len(tpids) == 2
	assert tpids[0].index == 0
	assert tpids[1].index == 1


def test_variant_canonical_typevar_has_correct_display_name():
	"""Pre-created TypeVars for variant params must use the declared name (T, E),
	not the fallback T0/T1 naming."""
	tt = TypeTable()
	base = tt.declare_variant(
		"pkg.mod", "Result", ["T", "E"],
		[
			VariantArmSchema(name="Ok", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
			VariantArmSchema(name="Err", fields=[VariantFieldSchema(name="error", type_expr=GenericTypeExpr.param(1))]),
		],
	)
	tpids = tt.variant_type_param_ids[base]
	tv_t = tt.ensure_typevar(tpids[0])
	tv_e = tt.ensure_typevar(tpids[1])
	assert tt.get(tv_t).name == "T", f"expected 'T', got '{tt.get(tv_t).name}'"
	assert tt.get(tv_e).name == "E", f"expected 'E', got '{tt.get(tv_e).name}'"


def test_variant_redeclare_preserves_type_param_ids():
	"""Re-declaring the same variant must return the same type param ids (idempotent)."""
	tt = TypeTable()
	arms = [
		VariantArmSchema(name="Ok", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
		VariantArmSchema(name="Err", fields=[VariantFieldSchema(name="error", type_expr=GenericTypeExpr.param(1))]),
	]
	base1 = tt.declare_variant("pkg.mod", "Result", ["T", "E"], arms)
	base2 = tt.declare_variant("pkg.mod", "Result", ["T", "E"], arms)
	assert base1 == base2
	tpids = tt.variant_type_param_ids[base1]
	# Ensure TypeVars are still the canonical ones
	assert tt.get(tt.ensure_typevar(tpids[0])).name == "T"
	assert tt.get(tt.ensure_typevar(tpids[1])).name == "E"


def test_variant_forward_nominal_preserves_type_param_ids():
	"""Variant declared after forward nominal resolution must still get type param ids."""
	tt = TypeTable()
	# Simulate forward nominal: declare a forward_nominal first
	from lang.driftc.core.types_core import NominalKey
	fwd_key = NominalKey(package_id="", module_id="pkg.mod", name="Result", kind=TypeKind.FORWARD_NOMINAL)
	fwd_id = tt._add(TypeKind.FORWARD_NOMINAL, "Result", [], module_id="pkg.mod")
	tt._nominal[fwd_key] = fwd_id
	# Now declare the real variant
	arms = [
		VariantArmSchema(name="Ok", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
		VariantArmSchema(name="Err", fields=[VariantFieldSchema(name="error", type_expr=GenericTypeExpr.param(1))]),
	]
	base = tt.declare_variant("pkg.mod", "Result", ["T", "E"], arms)
	assert base == fwd_id
	tpids = tt.variant_type_param_ids.get(base)
	assert tpids is not None, "forward-nominal→variant must populate variant_type_param_ids"
	assert tt.get(tt.ensure_typevar(tpids[0])).name == "T"


# ---------------------------------------------------------------------------
# Interface canonical TypeVar pre-creation
# ---------------------------------------------------------------------------

def test_interface_canonical_typevar_has_correct_display_name():
	"""Pre-created TypeVars for interface params must use the declared name."""
	tt = TypeTable()
	base = tt.declare_interface("pkg.mod", "Iterable", ["T"])
	tpids = tt.interface_type_param_ids.get(base)
	assert tpids is not None, "interface_type_param_ids not populated"
	assert len(tpids) == 1
	tv = tt.ensure_typevar(tpids[0])
	assert tt.get(tv).name == "T", f"expected 'T', got '{tt.get(tv).name}'"


def test_interface_forward_nominal_preserves_type_param_ids():
	"""Interface declared after forward nominal must still get type param ids."""
	tt = TypeTable()
	from lang.driftc.core.types_core import NominalKey
	fwd_key = NominalKey(package_id="", module_id="pkg.mod", name="Iterable", kind=TypeKind.FORWARD_NOMINAL)
	fwd_id = tt._add(TypeKind.FORWARD_NOMINAL, "Iterable", [], module_id="pkg.mod")
	tt._nominal[fwd_key] = fwd_id
	base = tt.declare_interface("pkg.mod", "Iterable", ["T"])
	assert base == fwd_id
	tpids = tt.interface_type_param_ids.get(base)
	assert tpids is not None
	assert tt.get(tt.ensure_typevar(tpids[0])).name == "T"


# ---------------------------------------------------------------------------
# canonicalize_impl_type_params: auto-probe across all nominal kinds
# ---------------------------------------------------------------------------

def test_canonicalize_impl_params_struct():
	"""canonicalize_impl_type_params must resolve struct targets."""
	tt = TypeTable()
	base = tt.declare_struct("pkg.mod", "Box", [], type_params=["T"])
	struct_tpids = tt.struct_type_param_ids[base]
	impl_owner = FunctionId(module="lang.__external", name="__impl_test:0", ordinal=0)
	impl_map = {"T": TypeParamId(impl_owner, 0)}
	result = tt.canonicalize_impl_type_params(
		impl_map, target_module="pkg.mod", target_name="Box", target_args=["T"],
	)
	assert result["T"] is struct_tpids[0]


def test_canonicalize_impl_params_variant():
	"""canonicalize_impl_type_params must resolve variant targets via auto-probe."""
	tt = TypeTable()
	base = tt.declare_variant(
		"pkg.mod", "Option", ["T"],
		[
			VariantArmSchema(name="None", fields=[]),
			VariantArmSchema(name="Some", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
		],
	)
	variant_tpids = tt.variant_type_param_ids[base]
	impl_owner = FunctionId(module="lang.__external", name="__impl_test:0", ordinal=0)
	impl_map = {"T": TypeParamId(impl_owner, 0)}
	result = tt.canonicalize_impl_type_params(
		impl_map, target_module="pkg.mod", target_name="Option", target_args=["T"],
	)
	assert result["T"] is variant_tpids[0], (
		"canonicalize_impl_type_params must alias impl T to variant's canonical T"
	)


def test_canonicalize_impl_params_interface():
	"""canonicalize_impl_type_params must resolve interface targets via auto-probe."""
	tt = TypeTable()
	base = tt.declare_interface("pkg.mod", "Iterable", ["T"])
	iface_tpids = tt.interface_type_param_ids[base]
	impl_owner = FunctionId(module="lang.__external", name="__impl_test:0", ordinal=0)
	impl_map = {"T": TypeParamId(impl_owner, 0)}
	result = tt.canonicalize_impl_type_params(
		impl_map, target_module="pkg.mod", target_name="Iterable", target_args=["T"],
	)
	assert result["T"] is iface_tpids[0], (
		"canonicalize_impl_type_params must alias impl T to interface's canonical T"
	)


def test_canonicalize_impl_params_swapped_args():
	"""Expression-based canonicalization must respect arg order, not name matching."""
	tt = TypeTable()
	base = tt.declare_variant(
		"pkg.mod", "Pair", ["A", "B"],
		[VariantArmSchema(name="Val", fields=[
			VariantFieldSchema(name="first", type_expr=GenericTypeExpr.param(0)),
			VariantFieldSchema(name="second", type_expr=GenericTypeExpr.param(1)),
		])],
	)
	variant_tpids = tt.variant_type_param_ids[base]
	impl_owner = FunctionId(module="lang.__external", name="__impl_test:0", ordinal=0)
	# impl<X,Y> Trait for Pair<Y,X> — swapped: Y→slot 0, X→slot 1
	impl_map = {"X": TypeParamId(impl_owner, 0), "Y": TypeParamId(impl_owner, 1)}
	result = tt.canonicalize_impl_type_params(
		impl_map, target_module="pkg.mod", target_name="Pair", target_args=["Y", "X"],
	)
	assert result["Y"] is variant_tpids[0], "Y should alias variant's canonical param 0 (A)"
	assert result["X"] is variant_tpids[1], "X should alias variant's canonical param 1 (B)"


# ---------------------------------------------------------------------------
# canonical_nominal_typevar: single interning point
# ---------------------------------------------------------------------------

def test_canonical_nominal_typevar_variant():
	"""canonical_nominal_typevar must return the pre-created TypeVar for variants."""
	tt = TypeTable()
	tt.declare_variant(
		"pkg.mod", "Option", ["T"],
		[
			VariantArmSchema(name="None", fields=[]),
			VariantArmSchema(name="Some", fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))]),
		],
	)
	tv = tt.canonical_nominal_typevar(kind="variant", module_id="pkg.mod", name="Option", param_index=0)
	assert tv is not None
	assert tt.get(tv).name == "T"
	assert tt.get(tv).kind is TypeKind.TYPEVAR


def test_canonical_nominal_typevar_interface():
	"""canonical_nominal_typevar must return the pre-created TypeVar for interfaces."""
	tt = TypeTable()
	tt.declare_interface("pkg.mod", "Iterable", ["T"])
	tv = tt.canonical_nominal_typevar(kind="interface", module_id="pkg.mod", name="Iterable", param_index=0)
	assert tv is not None
	assert tt.get(tv).name == "T"
	assert tt.get(tv).kind is TypeKind.TYPEVAR


def test_canonical_nominal_typevar_returns_none_for_missing():
	"""canonical_nominal_typevar must return None for nonexistent types."""
	tt = TypeTable()
	assert tt.canonical_nominal_typevar(kind="struct", module_id="x", name="X", param_index=0) is None
	assert tt.canonical_nominal_typevar(kind="variant", module_id="x", name="X", param_index=0) is None
	assert tt.canonical_nominal_typevar(kind="interface", module_id="x", name="X", param_index=0) is None
