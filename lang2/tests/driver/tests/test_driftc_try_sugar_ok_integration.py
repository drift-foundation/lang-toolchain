"""
Positive integration: try sugar + FnResult.Ok should pass end-to-end.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures, build_exception_catalog


def test_try_sugar_with_ok_return_passes():
	"""
	`f` calls a FnResult-returning callee via try sugar and returns Ok(x).
	Checker should accept the try operand, lowering should desugar, and typed
	throw checks should pass with no diagnostics.
	"""
	hirs = {
		"callee": H.HBlock(
			statements=[
				H.HReturn(value=H.HResultOk(value=H.HLiteralInt(value=1))),
			]
		),
		"f": H.HBlock(
			statements=[
				H.HLet(name="tmp", value=H.HTryResult(expr=H.HCall(fn=H.HVar(name="callee"), args=[]))),
				H.HReturn(value=H.HResultOk(value=H.HVar(name="tmp"))),
			]
		),
	}
	signatures = make_signatures(
		{
			"callee": "FnResult<Int, Error>",
			"f": "FnResult<Int, Error>",
		}
	)
	exc_env = build_exception_catalog(["Evt"])

	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs=hirs,
		signatures=signatures,
		exc_env=exc_env,
		build_ssa=True,
		return_checked=True,
	)

	assert set(mir_funcs.keys()) == {"callee", "f"}
	assert checked.diagnostics == []
	assert checked.type_env is not None
