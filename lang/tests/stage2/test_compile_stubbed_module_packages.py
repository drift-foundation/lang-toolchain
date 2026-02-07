# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from lang.driftc.core.types_core import TypeTable
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.stage1 import HBlock, HReturn, HLiteralInt
from lang.driftc.core.function_id import FunctionId


def test_compile_stubbed_funcs_populates_module_packages() -> None:
	type_table = TypeTable()
	type_table.module_packages = {}
	func_hirs = {
		FunctionId(module="m", name="main", ordinal=0): HBlock(statements=[HReturn(value=HLiteralInt(value=0))]),
	}
	compile_stubbed_funcs(
		func_hirs=func_hirs,
		type_table=type_table,
		package_id="pkg",
	)
	assert type_table.module_packages.get("m") == "pkg"
