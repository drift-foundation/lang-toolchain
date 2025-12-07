"""
Integration: type-aware FnResult forwarding should succeed via checker TypeEnv.
"""

from __future__ import annotations

from lang2 import stage1 as H
from lang2.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_typeaware_fnresult_forwarding_passes_with_checker_type_env():
	"""
	Can-throw function returning a forwarded FnResult should pass when TypeEnv is present.
	Structural guard would reject this if we weren't using the typed path.
	"""
	fn_name = "f_forward"
	# HIR: function calls a callee returning FnResult and forwards the result.
	hir = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HCall(fn=H.HVar(name="callee"), args=[])),
			H.HReturn(value=H.HVar(name="x")),
		]
	)

	signatures = make_signatures(
		{
			fn_name: "FnResult<Int, Error>",
			"callee": "FnResult<Int, Error>",
		}
	)

	# Driver with build_ssa=True should build SSA and a checker-owned TypeEnv.
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={fn_name: hir},
		signatures=signatures,
		exc_env={},
		build_ssa=True,
		return_checked=True,
	)

	assert fn_name in mir_funcs
	assert checked.type_env is not None
	assert checked.diagnostics == []
