"""
Integration: driver builds SSA + TypeEnv for type-aware throw checks.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import build_exception_catalog, make_signatures


def test_compile_stubbed_funcs_builds_ssa_and_type_env():
	"""
	When build_ssa=True, the driver should run MIR→SSA, derive a TypeEnv from
	signatures, and pass it into throw checks without raising.
	"""
	hir_block = H.HBlock(
		statements=[
			H.HThrow(value=H.HDVInit(dv_type_name="Evt", args=[])),
		]
	)

	signatures = make_signatures({"f": "FnResult<Int, Error>"})
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={"f": hir_block},
		signatures=signatures,
		exc_env=build_exception_catalog({"Evt": 1}),
		build_ssa=True,
		return_checked=True,
	)

	assert "f" in mir_funcs
	assert not checked.diagnostics, f"unexpected diagnostics: {checked.diagnostics}"
	assert checked.type_env is not None
