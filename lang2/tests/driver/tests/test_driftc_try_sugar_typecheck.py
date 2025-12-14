"""
Checker should reject result-driven try sugar (`expr?`) when the operand is not
known to be a can-throw call (lang2 internal ABI uses FnResult).
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.test_support import make_signatures


def test_try_sugar_operand_must_be_fnresult():
	"""
	Using try sugar on a non-call/non-can-throw operand should surface a checker diagnostic.
	"""
	fn_name = "uses_try_wrong"
	hir = H.HBlock(
		statements=[
			H.HReturn(value=H.HTryResult(expr=H.HLiteralInt(value=1))),
		]
	)
	signatures = make_signatures({fn_name: "Int"}, declared_can_throw={fn_name: True})

	# Checker should emit a diagnostic about the try operand before lowering.
	_, checked = compile_stubbed_funcs(
		func_hirs={fn_name: hir},
		signatures=signatures,
		exc_env={},
		build_ssa=False,
		return_checked=True,
	)

	messages = [diag.message for diag in checked.diagnostics]
	assert any("try-expression on a non" in msg and "try sugar requires" in msg for msg in messages)
