"""
Integration test: driver should surface catch-arm validation diagnostics from HIR.
"""

from __future__ import annotations

from lang2.driftc import stage1 as H
from lang2.driftc.driftc import compile_stubbed_funcs
from lang2.driftc.checker import FnSignature
from lang2.test_support import build_exception_catalog


def test_driver_collects_catch_arms_and_reports_diagnostics():
	"""
	compile_stubbed_funcs should collect catch arms from HIR, pass them to the
	checker, and surface diagnostics for invalid shapes (multiple catch-alls).
	"""
	hir_block = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[]),
				catches=[
					H.HCatchArm(event_fqn="m:Evt", binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn="m:Evt", binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn="m:UnknownEvt", binder=None, block=H.HBlock(statements=[])),
				],
			),
			H.HReturn(value=H.HLiteralInt(value=0)),
		]
	)

	signatures = {"f": FnSignature(name="f", return_type="Int")}
	mir_funcs, checked = compile_stubbed_funcs(
		func_hirs={"f": hir_block},
		signatures=signatures,
		exc_env=build_exception_catalog({"m:Evt": 1}),
		return_checked=True,
	)

	assert "f" in mir_funcs
	assert checked.diagnostics, "expected diagnostics for invalid catch arms"
	msgs = [diag.message for diag in checked.diagnostics]
	assert any("duplicate catch arm for event m:Evt" in msg for msg in msgs)
	assert any("unknown catch event m:UnknownEvt" in msg for msg in msgs)
