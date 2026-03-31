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
