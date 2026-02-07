"""
Integration: driver builds SSA + TypeEnv for type-aware throw checks.
"""

from __future__ import annotations

from lang.driftc import stage1 as H
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.core.function_id import function_symbol
from lang.driftc.core.types_core import TypeTable
from lang.test_support import build_exception_catalog, make_signatures


def test_compile_stubbed_funcs_builds_ssa_and_type_env():
	"""
	When build_ssa=True, the driver should run MIR→SSA, derive a TypeEnv from;
	signatures, and pass it into throw checks without raising.
	"""
	hir_block = H.HBlock(
		statements=[
			H.HThrow(value=H.HExceptionInit(event_fqn="m:Evt", pos_args=[], kw_args=[])),
		]
	)

	signatures = make_signatures({"f": "Int"}, declared_can_throw={"f": True})
	type_table = TypeTable()
	type_table.exception_schemas = {"m:Evt": ("m:Evt", [])}
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={"f": hir_block},
		signatures=signatures,
		exc_env=build_exception_catalog({"m:Evt": 1}),
		type_table=type_table,
		build_ssa=True,
		return_checked=True,
	)

	assert any(function_symbol(fn_id) == "f" for fn_id in mir_funcs)
	assert not checked.diagnostics, f"unexpected diagnostics: {checked.diagnostics}"
	assert checked.type_env is not None
