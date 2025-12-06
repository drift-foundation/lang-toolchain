"""
Exercises the checker stub's signature inference and catch-arm validation.
"""

from __future__ import annotations

from lang2.checker import Checker, FnSignature
from lang2.checker.catch_arms import CatchArmInfo


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
	assert checked.diagnostics == []


def test_checker_validates_catch_arms_and_accumulates_diagnostics():
	"""Checker should run catch-arm validation and accumulate diagnostics."""
	signatures = {"f": FnSignature(name="f", return_type="Int")}
	catch_arms = {
		"f": [
			CatchArmInfo(event_name=None),
			CatchArmInfo(event_name=None),
		]
	}
	checker = Checker(signatures=signatures, catch_arms=catch_arms, exception_catalog={"Evt": 1})

	checked = checker.check(["f"])

	assert checked.diagnostics, "expected diagnostics for invalid catch arms"
	msgs = [diag.message for diag in checked.diagnostics]
	assert any("multiple catch-all" in msg for msg in msgs)
	assert any("catch-all must be the last" in msg for msg in msgs)
