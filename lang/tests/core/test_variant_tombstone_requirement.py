# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import pytest

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeTable, VariantArmSchema, VariantFieldSchema


def test_variant_instantiation_requires_tombstone_for_droppable_payloads() -> None:
	table = TypeTable()
	base = table.declare_variant(
		"m",
		"Maybe",
		["T"],
		[
			VariantArmSchema(
				name="Some",
				fields=[VariantFieldSchema(name="value", type_expr=GenericTypeExpr.param(0))],
			),
			VariantArmSchema(name="None", fields=[]),
		],
	)
	with pytest.raises(ValueError, match="requires tombstone_ctor"):
		table.ensure_instantiated(base, [table.ensure_string()])
