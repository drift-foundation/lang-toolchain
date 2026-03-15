# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, InterfaceParamSchema, TypeTable
from lang.driftc.packages.provisional_dmir_v0 import encode_type_table
from lang.driftc.packages.type_table_link_v0 import decode_type_table_obj


def test_interface_schema_round_trip() -> None:
	table = TypeTable()
	table.package_id = "pkgA"
	table.module_packages["m"] = "pkgA"
	base_id = table.declare_interface("m", "Callback1", ["A", "R"])
	method = InterfaceMethodSchema(
		name="call",
		params=[InterfaceParamSchema(name="x", type_expr=GenericTypeExpr.param(0))],
		return_type=GenericTypeExpr.param(1),
		type_params=[],
		declared_nothrow=True,
		is_unsafe=False,
	)
	table.define_interface_schema_methods(base_id, [method])

	encoded = encode_type_table(table, package_id="pkgA")
	decoded = decode_type_table_obj(encoded)
	keys = [k for k in decoded.interface_schemas.keys() if k.module_id == "m" and k.name == "Callback1"]
	assert len(keys) == 1
	key = keys[0]
	type_params, methods, _parents, _base_id = decoded.interface_schemas[key]
	assert type_params == ["A", "R"]
	assert len(methods) == 1
	out = methods[0]
	assert out.name == "call"
	assert out.type_params == []
	assert len(out.params) == 1
	assert out.params[0].name == "x"
	assert out.params[0].type_expr == GenericTypeExpr.param(0)
	assert out.return_type == GenericTypeExpr.param(1)
	assert out.declared_nothrow is True
	assert out.is_unsafe is False
