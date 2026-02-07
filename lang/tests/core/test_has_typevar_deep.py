# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import StructFieldSchema, TypeParamId, TypeTable


def test_has_typevar_is_deep_for_struct_instances() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base = table.declare_struct("m", "Box", ["value"], type_params=["T"])
	table.define_struct_schema_fields(
		base,
		[StructFieldSchema(name="value", type_expr=GenericTypeExpr(param_index=0))],
	)
	param_id = TypeParamId(owner=FunctionId(module="m", name="Box", ordinal=0), index=0)
	tvar = table.ensure_typevar(param_id, name="T")
	inner = table.ensure_struct_template(base, [tvar])
	outer = table.ensure_struct_template(base, [inner])
	assert table.has_typevar(outer) is True
	with pytest.raises(ValueError, match="must be concrete"):
		table.ensure_struct_instantiated(base, [outer])
