"""
Integration: explicit nothrow + try-sugar should be rejected.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_driver_flags_try_sugar_in_explicit_nothrow_fn():
	"""
	`f_plain` is explicitly declared nothrow but uses try sugar on a can-throw call,
	so it may throw and must be rejected with a checker diagnostic.
	"""
	hirs = {
		"callee": H.HBlock(
			statements=[H.HReturn(value=H.HLiteralInt(value=1))]
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
			"f_plain": "Int",
			"callee": "Int",
		},
		declared_can_throw={"callee": True, "f_plain": False},
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
	assert any("declared nothrow but may throw" in m for m in msgs)
