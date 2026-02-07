# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import StructFieldSchema, TypeTable


def test_define_struct_schema_fields_rejects_field_name_mismatch() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base = table.declare_struct("m", "Foo", ["a", "b"])
	fields = [
		StructFieldSchema(name="b", type_expr=GenericTypeExpr(name="Int")),
		StructFieldSchema(name="a", type_expr=GenericTypeExpr(name="Int")),
	]
	with pytest.raises(ValueError, match="field names mismatch"):
		table.define_struct_schema_fields(base, fields)
