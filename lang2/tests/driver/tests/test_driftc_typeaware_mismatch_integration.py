"""
Integration: can-throw functions return `T` at the surface but are wrapped into
an internal FnResult at the ABI.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_can_throw_plain_return_is_wrapped_into_ok():
	"""
	A can-throw function returning a plain `Int` at the surface should compile:
	HIR->MIR wraps the return into `ConstructResultOk` and stage4 sees a well-formed
	internal FnResult return.
	"""
	fn_name = "f_ok"
	hir = H.HBlock(
		statements=[
			H.HReturn(value=H.HLiteralInt(value=1)),
		]
	)
	signatures = make_signatures({fn_name: "Int"}, declared_can_throw={fn_name: True})

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={fn_name: hir},
		signatures=signatures,
		exc_env={},
		build_ssa=True,
		return_checked=True,
	)

	# MIR exists, the driver builds SSA+TypeEnv, and there should be no mismatch
	# diagnostics: the wrapper is internal and correct.
	assert fn_name in mir_funcs
	assert checked.type_env is not None
	assert checked.diagnostics == []

