"""
Integration: type-aware FnResult check should emit a diagnostic when a can-throw
function returns a non-FnResult value.
"""

from __future__ import annotations

from lang2 import stage1 as H
from lang2.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_typeaware_fnresult_mismatch_emits_diagnostic():
	"""
	Function declared FnResult returns a bare Int; type-aware check should fail.
	"""
	fn_name = "f_bad"
	hir = H.HBlock(
		statements=[
			H.HReturn(value=H.HLiteralInt(value=1)),
		]
	)
	signatures = make_signatures({fn_name: "FnResult<Int, Error>"})

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={fn_name: hir},
		signatures=signatures,
		exc_env={},
		build_ssa=True,
		return_checked=True,
	)

	# MIR exists, and the checker supplies a TypeEnv; violations should surface
	# as diagnostics rather than RuntimeError on the driver path.
	assert fn_name in mir_funcs
	assert checked.type_env is not None
	messages = [diag.message for diag in checked.diagnostics]
	assert any("non-FnResult" in msg for msg in messages)
