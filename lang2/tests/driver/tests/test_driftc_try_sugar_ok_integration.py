"""
Positive integration: try sugar on a can-throw call should pass end-to-end.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures, build_exception_catalog


def test_try_sugar_with_ok_return_passes():
	"""
	`f` applies try sugar (`expr?`) to a known-ok FnResult value and returns the ok value.

	This stays intentionally internal: surface signatures are `returns T`, and
	FnResult is used only as the internal carrier that try-sugar expands over.
	"""
	hirs = {
		"f": H.HBlock(
			statements=[
				H.HLet(name="tmp", value=H.HTryResult(expr=H.HResultOk(value=H.HLiteralInt(value=1)))),
				H.HReturn(value=H.HVar(name="tmp")),
			]
		),
	}
	signatures = make_signatures(
		{
			"f": "Int",
		},
		declared_can_throw={"f": True},
	)
	exc_env = build_exception_catalog([])

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_env,
		build_ssa=True,
		return_checked=True,
	)

	assert set(mir_funcs.keys()) == {"f"}
	assert checked.diagnostics == []
	assert checked.type_env is not None
