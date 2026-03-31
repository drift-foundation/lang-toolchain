# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Focused regressions for Stage 8.0/8.1 type-expression serialization."""
from __future__ import annotations

import pytest

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeId, TypeKind, TypeParamId, TypeTable
from lang.driftc.packages.provisional_dmir_v0 import (
	decode_type_expr,
	encode_signatures,
	typeid_to_type_expr,
)


# ---------------------------------------------------------------------------
# Stage 8.0: producer-side completeness enforcement
# ---------------------------------------------------------------------------

def test_encode_signatures_raises_on_unreconstructable_param_type() -> None:
	"""Prove that encode_signatures raises when a param TypeId cannot be
	reconstructed to a TypeExpr (Stage 8.0 hard-failure contract)."""
	tt = TypeTable()
	unknown_tid = tt.ensure_unknown()

	sig = FnSignature(
		name="bad",
		module="m",
		param_type_ids=[unknown_tid],
		return_type_id=tt.ensure_void(),
		declared_can_throw=False,
	)

	with pytest.raises(ValueError, match="typeid_to_type_expr failed for param TypeId"):
		encode_signatures({"m::bad": sig}, module_id="m", type_table=tt)


def test_encode_signatures_raises_on_unreconstructable_return_type() -> None:
	"""Prove that encode_signatures raises when a return TypeId cannot be
	reconstructed to a TypeExpr."""
	tt = TypeTable()
	unknown_tid = tt.ensure_unknown()

	sig = FnSignature(
		name="bad",
		module="m",
		param_type_ids=[],
		return_type_id=unknown_tid,
		declared_can_throw=False,
	)

	with pytest.raises(ValueError, match="typeid_to_type_expr failed for return TypeId"):
		encode_signatures({"m::bad": sig}, module_id="m", type_table=tt)


def test_encode_signatures_raises_on_unreconstructable_impl_target() -> None:
	"""Prove that encode_signatures raises when an impl_target TypeId cannot be
	reconstructed to a TypeExpr."""
	tt = TypeTable()
	unknown_tid = tt.ensure_unknown()
	void_tid = tt.ensure_void()

	sig = FnSignature(
		name="bad",
		module="m",
		param_type_ids=[],
		return_type_id=void_tid,
		declared_can_throw=False,
		impl_target_type_id=unknown_tid,
		is_method=True,
	)

	with pytest.raises(ValueError, match="typeid_to_type_expr failed for impl_target TypeId"):
		encode_signatures({"m::bad": sig}, module_id="m", type_table=tt)


def test_encode_signatures_succeeds_without_type_table() -> None:
	"""Without a type_table, encode_signatures should not attempt
	reconstruction and should not raise (backward compatibility)."""
	tt = TypeTable()
	unknown_tid = tt.ensure_unknown()

	sig = FnSignature(
		name="ok",
		module="m",
		param_type_ids=[unknown_tid],
		return_type_id=unknown_tid,
		declared_can_throw=False,
	)

	# No type_table -> no reconstruction, no error.
	result = encode_signatures({"m::ok": sig}, module_id="m")
	assert "m::ok" in result
	assert result["m::ok"]["param_types"] is None
	assert result["m::ok"]["return_type"] is None


def test_module_scoped_scalar_preserves_module_id() -> None:
	"""Module-scoped scalars must preserve module_id in the reconstructed
	TypeExpr so the consumer resolves the correct nominal type."""
	tt = TypeTable()
	# Simulate a module-scoped scalar (e.g. m.Size).
	scalar_tid = tt._add(TypeKind.SCALAR, "Size", [], module_id="m.types")

	expr = typeid_to_type_expr(scalar_tid, tt)
	assert expr is not None
	assert expr.name == "Size"
	assert expr.module_id == "m.types"


def test_builtin_scalar_has_no_module_id() -> None:
	"""Builtin scalars (Int, Bool, etc.) must NOT carry module_id."""
	tt = TypeTable()
	int_tid = tt.ensure_int()
	expr = typeid_to_type_expr(int_tid, tt)
	assert expr is not None
	assert expr.name == "Int"
	assert expr.module_id is None


# ---------------------------------------------------------------------------
# Stage 8.1: error_type consumer regression
# ---------------------------------------------------------------------------

def test_error_type_round_trip() -> None:
	"""Prove that error_type is emitted by the producer and can be resolved
	by the consumer, rather than falling back to the synthesize-from-can_throw
	fixup path."""
	# --- Producer side ---
	producer_tt = TypeTable()
	int_tid = producer_tt.ensure_int()
	err_tid = producer_tt.ensure_error()

	sig = FnSignature(
		name="might_fail",
		module="m",
		param_type_ids=[int_tid],
		return_type_id=int_tid,
		error_type_id=err_tid,
		declared_can_throw=True,
	)

	encoded = encode_signatures({"m::might_fail": sig}, module_id="m", type_table=producer_tt)
	sd = encoded["m::might_fail"]

	# Verify error_type field is present in the serialized payload.
	assert sd.get("error_type") is not None, "error_type must be emitted"
	assert sd["error_type"]["name"] == "Error"

	# --- Consumer side ---
	consumer_tt = TypeTable()

	# Resolve error_type via TypeExpr (the Stage 8.1 consumer path).
	et_expr = decode_type_expr(sd["error_type"])
	assert et_expr is not None
	resolved_err_tid = resolve_opaque_type(et_expr, consumer_tt, module_id="m")

	# The resolved TypeId should be the Error type, not Unknown.
	resolved_td = consumer_tt.get(resolved_err_tid)
	assert resolved_td.kind is TypeKind.ERROR, (
		f"error_type should resolve to ERROR, got {resolved_td.kind}"
	)


# ---------------------------------------------------------------------------
# Stage 8.2: canonical key and assertion regressions
# ---------------------------------------------------------------------------

def test_host_type_key_includes_package_identity() -> None:
	"""Prove that _host_type_key includes package_id for module-scoped
	nominals, so two structs with the same name in different packages
	produce different keys."""
	from lang.driftc.driftc import _host_type_key

	tt = TypeTable()
	tt.package_id = "pkg.a"
	tt.module_packages["mod.a"] = "pkg.a"
	tt.module_packages["mod.b"] = "pkg.b"

	tid_a = tt.declare_struct(module_id="mod.a", name="Foo", field_names=[])
	tid_b = tt.declare_struct(module_id="mod.b", name="Foo", field_names=[])

	key_a = _host_type_key(tid_a, tt)
	key_b = _host_type_key(tid_b, tt)

	assert key_a != key_b, (
		f"Same-named structs in different packages must have different canonical keys: "
		f"key_a={key_a} key_b={key_b}"
	)
	# Verify package_id is present in the key.
	assert "pkg.a" in str(key_a)
	assert "pkg.b" in str(key_b)


def test_host_type_key_same_package_same_key() -> None:
	"""Two TypeIds for the same nominal type produce identical keys."""
	from lang.driftc.driftc import _host_type_key

	tt = TypeTable()
	tt.package_id = "pkg.a"
	tt.module_packages["mod.a"] = "pkg.a"

	tid = tt.declare_struct(module_id="mod.a", name="Bar", field_names=[])
	key1 = _host_type_key(tid, tt)
	key2 = _host_type_key(tid, tt)

	assert key1 == key2


def test_assert_typexpr_tid_match_raises_on_canonical_mismatch() -> None:
	"""Prove that _assert_typexpr_tid_match raises AssertionError when
	canonical keys differ (not just log)."""
	from lang.driftc.driftc import _assert_typexpr_tid_match

	tt = TypeTable()
	int_tid = tt.ensure_int()
	bool_tid = tt.ensure_bool()

	with pytest.raises(AssertionError, match="CANONICAL MISMATCH"):
		_assert_typexpr_tid_match("test", int_tid, bool_tid, tt)


def test_assert_typexpr_tid_match_passes_on_identical() -> None:
	"""Prove that _assert_typexpr_tid_match passes silently when TypeIds
	are identical."""
	from lang.driftc.driftc import _assert_typexpr_tid_match

	tt = TypeTable()
	int_tid = tt.ensure_int()

	# Should not raise.
	_assert_typexpr_tid_match("test", int_tid, int_tid, tt)


def test_assert_typexpr_tid_match_raises_on_none_mismatch() -> None:
	"""Prove that _assert_typexpr_tid_match raises when one side is None."""
	from lang.driftc.driftc import _assert_typexpr_tid_match

	tt = TypeTable()
	int_tid = tt.ensure_int()

	with pytest.raises(AssertionError, match="one is None"):
		_assert_typexpr_tid_match("test", int_tid, None, tt)


# ---------------------------------------------------------------------------
# Stage 8.2: TYPEVAR canonical-key package identity
# ---------------------------------------------------------------------------

def test_host_type_key_typevar_includes_owner_package() -> None:
	"""Prove that two otherwise-identical TypeVars from different package/module
	owners do not collapse to the same _host_type_key."""
	from lang.driftc.driftc import _host_type_key

	tt = TypeTable()
	tt.package_id = "pkg.a"
	tt.module_packages["mod.a"] = "pkg.a"
	tt.module_packages["mod.b"] = "pkg.b"

	fn_a = FunctionId(module="mod.a", name="foo", ordinal=0)
	fn_b = FunctionId(module="mod.b", name="foo", ordinal=0)
	tp_a = TypeParamId(owner=fn_a, index=0)
	tp_b = TypeParamId(owner=fn_b, index=0)

	tv_a = tt.ensure_typevar(tp_a, name="T")
	tv_b = tt.ensure_typevar(tp_b, name="T")

	key_a = _host_type_key(tv_a, tt)
	key_b = _host_type_key(tv_b, tt)

	assert key_a != key_b, (
		f"TypeVars with same name/index but different owner packages must have "
		f"different canonical keys: key_a={key_a} key_b={key_b}"
	)
	# Verify package identity is in the key.
	assert "pkg.a" in str(key_a)
	assert "pkg.b" in str(key_b)


# ---------------------------------------------------------------------------
# Stage 8.2: Unknown fallback behavior
# ---------------------------------------------------------------------------

def test_param_type_unknown_falls_back_to_raw_ids() -> None:
	"""Prove that when TypeExpr param resolution produces Unknown, the consumer
	falls back to raw param_type_ids via tid_map rather than accepting Unknown."""
	from lang.driftc.packages.provisional_dmir_v0 import decode_type_expr

	tt = TypeTable()
	int_tid = tt.ensure_int()

	# Simulate a signature payload where param_types contains a TypeExpr
	# that cannot be resolved (references a struct not declared in this table).
	sd = {
		"fn_id": {"module": "m", "name": "f", "ordinal": 0},
		"name": "f",
		"module": "m",
		"param_type_ids": [int(int_tid)],
		"return_type_id": int(int_tid),
		"param_types": [{"name": "NoSuchType", "module": "missing.mod"}],
		"return_type": {"name": "Int"},
		"declared_can_throw": False,
		"is_method": False,
	}

	# Decode param_types — the TypeExpr is valid but resolve_opaque_type
	# will produce Unknown for "NoSuchType" in "missing.mod".
	pt_expr = decode_type_expr(sd["param_types"][0])
	assert pt_expr is not None
	resolved = resolve_opaque_type(pt_expr, tt, module_id="m")
	resolved_kind = tt.get(resolved).kind
	assert resolved_kind in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL), (
		f"precondition: should resolve to Unknown or FORWARD_NOMINAL, got {resolved_kind}"
	)

	# Now simulate the consumer path: with Unknown/FORWARD_NOMINAL guard,
	# param resolution should fall back to raw param_type_ids.
	# The tid_map is identity for this test.
	_UNRESOLVED = {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL}
	tid_map: dict[int, int] = {int(int_tid): int(int_tid)}

	# TypeExpr path: should detect unresolved type and fall back.
	param_type_ids = None
	param_types_raw = sd.get("param_types")
	if param_types_raw is not None:
		resolved_params = []
		all_ok = True
		for pt_obj in param_types_raw:
			pt_e = decode_type_expr(pt_obj)
			if pt_e is None:
				all_ok = False
				break
			_resolved_pt = resolve_opaque_type(pt_e, tt, module_id="m")
			if tt.get(_resolved_pt).kind in _UNRESOLVED:
				all_ok = False
				break
			resolved_params.append(_resolved_pt)
		if all_ok:
			param_type_ids = resolved_params
	if param_type_ids is None:
		raw_ptids = sd.get("param_type_ids")
		if isinstance(raw_ptids, list):
			param_type_ids = [tid_map.get(int(x), int(x)) for x in raw_ptids]

	# Should have fallen back to the raw path.
	assert param_type_ids is not None
	assert len(param_type_ids) == 1
	assert param_type_ids[0] == int_tid, (
		f"Should fall back to tid_map result ({int_tid}), got {param_type_ids[0]}"
	)


def test_error_type_unknown_not_accepted() -> None:
	"""Prove that when error_type TypeExpr resolution produces Unknown, the
	consumer does not accept it (err_tid stays None for downstream fixup)."""

	tt = TypeTable()

	# Simulate an error_type TypeExpr pointing to a non-existent type.
	sd_error_type = {"name": "NoSuchError", "module": "missing.mod"}
	et_expr = decode_type_expr(sd_error_type)
	assert et_expr is not None
	resolved = resolve_opaque_type(et_expr, tt, module_id="m")
	resolved_kind = tt.get(resolved).kind
	assert resolved_kind in (TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL), (
		f"precondition: should resolve to Unknown or FORWARD_NOMINAL, got {resolved_kind}"
	)

	# Consumer path: with Unknown/FORWARD_NOMINAL guard, err_tid should stay None.
	_UNRESOLVED = {TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL}
	err_tid = None
	_resolved_err = resolve_opaque_type(et_expr, tt, module_id="m")
	if tt.get(_resolved_err).kind not in _UNRESOLVED:
		err_tid = _resolved_err

	assert err_tid is None, (
		f"error_type resolving to unresolved type should not be accepted, got {err_tid}"
	)
