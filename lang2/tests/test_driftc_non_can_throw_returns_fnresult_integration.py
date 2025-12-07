"""
Integration: non-can-throw function returning FnResult should be rejected.
"""

from __future__ import annotations

from lang2 import stage1 as H
from lang2.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_driver_flags_fnresult_return_in_non_can_throw_fn():
	"""
	`f_plain` returns a forwarded FnResult from `callee` but is declared non-can-throw.
	Type-aware checks should emit a diagnostic via the driver.
	"""
	hirs = {
		"callee": H.HBlock(
			statements=[H.HReturn(value=H.HResultOk(value=H.HLiteralInt(value=1)))]
		),
		"f_plain": H.HBlock(
			statements=[
				H.HLet(
					name="x",
					value=H.HTryResult(expr=H.HCall(fn=H.HVar(name="callee"), args=[])),
				),
				H.HReturn(value=H.HVar(name="x")),
			]
		),
	}

	signatures = make_signatures(
		{
			"callee": "FnResult<Int, Error>",
			"f_plain": "Int",
		}
	)

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env={},
		build_ssa=True,
		return_checked=True,
	)

	assert set(mir_funcs.keys()) == {"callee", "f_plain"}
	assert checked.type_env is not None
	assert checked.fn_infos["f_plain"].declared_can_throw is False
	msgs = [d.message for d in checked.diagnostics]
	assert any("not declared can-throw but returns FnResult" in m for m in msgs)
