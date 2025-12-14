"""
Integration: can-throw calls are checked and ok values forwarded.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_typeaware_fnresult_forwarding_passes_with_checker_type_env():
	"""
	A can-throw function can call another can-throw function and return the ok
	value. Internally this is FnResult plumbing, but surface signatures stay
	`returns T`.
	"""
	fn_name = "f_forward"
	# HIR: function calls a can-throw callee and returns the value.
	hir = H.HBlock(
		statements=[
			H.HLet(name="x", value=H.HCall(fn=H.HVar(name="callee"), args=[])),
			H.HReturn(value=H.HVar(name="x")),
		]
	)

	signatures = make_signatures(
		{
			fn_name: "Int",
			"callee": "Int",
		},
		declared_can_throw={fn_name: True, "callee": True},
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
