# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import InterfaceMethodSchema, InterfaceParamSchema, TypeTable


def test_interface_inheritance_duplicate_method_name_rejected() -> None:
	table = TypeTable()
	a = table.declare_interface("m", "A", type_params=[])
	b = table.declare_interface("m", "B", type_params=[])
	c = table.declare_interface("m", "C", type_params=[])
	method = InterfaceMethodSchema(
		name="f",
		type_params=[],
		params=[
			InterfaceParamSchema(
				name="self",
				type_expr=GenericTypeExpr(name="&", args=[GenericTypeExpr(name="Self", args=[])]),
			),
		],
		return_type=GenericTypeExpr(name="Void", args=[]),
	)
	table.define_interface_schema_methods(a, [method])
	table.define_interface_schema_methods(b, [method])
	with pytest.raises(ValueError, match="inherits duplicate method 'f'"):
		table.define_interface_schema_methods(c, [], parent_base_ids=[a, b])


def test_interface_inheritance_distinct_methods_ok() -> None:
	table = TypeTable()
	a = table.declare_interface("m", "A", type_params=[])
	b = table.declare_interface("m", "B", type_params=[])
	c = table.declare_interface("m", "C", type_params=[])
	method_f = InterfaceMethodSchema(
		name="f",
		type_params=[],
		params=[
			InterfaceParamSchema(
				name="self",
				type_expr=GenericTypeExpr(name="&", args=[GenericTypeExpr(name="Self", args=[])]),
			),
		],
		return_type=GenericTypeExpr(name="Void", args=[]),
	)
	method_g = InterfaceMethodSchema(
		name="g",
		type_params=[],
		params=[
			InterfaceParamSchema(
				name="self",
				type_expr=GenericTypeExpr(name="&", args=[GenericTypeExpr(name="Self", args=[])]),
			),
		],
		return_type=GenericTypeExpr(name="Void", args=[]),
	)
	table.define_interface_schema_methods(a, [method_f])
	table.define_interface_schema_methods(b, [method_g])
	table.define_interface_schema_methods(c, [], parent_base_ids=[a, b])
