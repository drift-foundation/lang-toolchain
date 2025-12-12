"""
Exercises the checker stub's signature inference and catch-arm validation.
"""

from __future__ import annotations

from lang2.driftc.checker import Checker, FnSignature
from lang2.driftc.checker.catch_arms import CatchArmInfo


def test_checker_infers_fnresult_and_declared_events_from_signature():
	"""Checker should infer can-throw + declared events from FnSignature."""
	signatures = {
		"f": FnSignature(name="f", return_type=("FnResult", "Ok", "Err"), throws_events=("EvtA", "EvtB")),
	}
	checked = Checker(signatures=signatures).check(["f"])

	info = checked.fn_infos["f"]
	assert info.declared_can_throw is True
	assert info.declared_events == frozenset({"EvtA", "EvtB"})
	assert info.return_type == ("FnResult", "Ok", "Err")
	# TypeIds should be assigned for FnResult/err side.
	assert info.return_type_id is not None
	assert info.error_type_id is not None
	assert checked.diagnostics == []


def test_checker_validates_catch_arms_and_accumulates_diagnostics():
	"""Checker should run catch-arm validation and accumulate diagnostics."""
	signatures = {"f": FnSignature(name="f", return_type="Int")}
	# Provide a minimal HIR with invalid catch arms so the checker discovers them.
	from lang2.driftc import stage1 as H  # local import to avoid circular test deps

	hir = H.HBlock(
		statements=[
			H.HTry(
				body=H.HBlock(statements=[]),
				catches=[
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
					H.HCatchArm(event_fqn=None, binder=None, block=H.HBlock(statements=[])),
				],
			)
		]
	)
	checker = Checker(signatures=signatures, exception_catalog={"m:Evt": 1}, hir_blocks={"f": hir})

	checked = checker.check(["f"])

	assert checked.diagnostics, "expected diagnostics for invalid catch arms"
	msgs = [diag.message for diag in checked.diagnostics]
	assert any("multiple catch-all" in msg for msg in msgs)
	assert any("catch-all must be the last" in msg for msg in msgs)
