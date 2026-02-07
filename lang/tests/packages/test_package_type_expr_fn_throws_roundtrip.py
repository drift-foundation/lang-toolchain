# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
import pytest

from lang.driftc.packages.provisional_dmir_v0 import decode_type_expr, encode_type_expr
from lang.driftc.packages.type_table_link_v0 import decode_type_table_obj
from lang.driftc.parser.ast import TypeExpr


def _fn_type_expr(*, nothrow: bool) -> TypeExpr:
	int_ty = TypeExpr(name="Int")
	return TypeExpr(name="fn", args=[int_ty, int_ty], fn_throws=(not nothrow))


def test_fn_type_expr_roundtrip_preserves_nothrow() -> None:
	expr = _fn_type_expr(nothrow=True)
	encoded = encode_type_expr(expr, default_module=None, type_param_names=None)
	assert encoded is not None
	assert encoded.get("can_throw") is False
	decoded = decode_type_expr(encoded)
	assert decoded is not None
	assert decoded.fn_throws is False


def test_fn_type_expr_roundtrip_preserves_can_throw_default() -> None:
	expr = _fn_type_expr(nothrow=False)
	encoded = encode_type_expr(expr, default_module=None, type_param_names=None)
	assert encoded is not None
	assert encoded.get("can_throw") is True
	decoded = decode_type_expr(encoded)
	assert decoded is not None
	assert decoded.fn_throws is True


def test_fn_type_expr_decode_missing_can_throw_defaults_to_can_throw() -> None:
	expr = _fn_type_expr(nothrow=False)
	encoded = encode_type_expr(expr, default_module=None, type_param_names=None)
	assert encoded is not None
	encoded.pop("can_throw", None)
	decoded = decode_type_expr(encoded)
	assert decoded is not None
	assert decoded.fn_throws is True


def test_decode_type_expr_rejects_null_can_throw() -> None:
	obj = {
		"name": "fn",
		"args": [{"name": "Int"}, {"name": "Int"}],
		"can_throw": None,
	}
	assert decode_type_expr(obj) is None


def test_decode_type_table_rejects_null_fn_throws() -> None:
	obj = {
		"defs": {
			"1": {
				"kind": "FUNCTION",
				"name": "fn",
				"param_types": [],
				"module_id": None,
				"ref_mut": None,
				"fn_throws": None,
				"field_names": None,
			}
		}
	}
	with pytest.raises(ValueError, match="fn_throws"):
		decode_type_table_obj(obj)
