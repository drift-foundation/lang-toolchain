# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.packages.type_table_link_v0 import _decode_generic_type_expr


def test_decode_generic_type_expr_rejects_empty_name_without_param_index() -> None:
	obj = {"name": "", "args": [], "param_index": None, "module_id": None}
	with pytest.raises(ValueError, match="GenericTypeExpr.name"):
		_decode_generic_type_expr(obj)
