"""
Integration: type-aware FnResult check should emit a diagnostic when a can-throw
function returns a non-FnResult value.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
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
	assert any("FnResult" in msg for msg in messages)


def test_typeaware_fnresult_part_mismatch_emits_diagnostic():
	"""
	Function declared FnResult<Int, Error> but returns FnResult<Bool, Error>;
	type-aware check should flag the ok/err part mismatch.
	"""
	fn_name = "f_mismatch"
	hir = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HCall(fn=H.HVar(name="callee_bool"), args=[])),
			H.HReturn(value=H.HVar(name="x")),
		]
	)
	signatures = make_signatures(
		{
			fn_name: "FnResult<Int, Error>",
			"callee_bool": ("FnResult", "Bool", "Error"),
		}
	)

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={fn_name: hir},
		signatures=signatures,
		exc_env={},
		build_ssa=True,
		return_checked=True,
	)

	assert fn_name in mir_funcs
	assert checked.type_env is not None
	messages = [diag.message for diag in checked.diagnostics]
	assert any("mismatched parts" in msg for msg in messages)
