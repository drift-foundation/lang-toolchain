"""
Checker should flag mismatches between declared_can_throw and the resolved return type.
"""

from __future__ import annotations

from lang2.driftc.checker import Checker, FnSignature


def test_can_throw_true_non_fnresult_return_flags_diagnostic():
	"""Legacy bool map forcing can-throw on a non-FnResult return should be rejected."""
	checker = Checker(
		declared_can_throw={"f": True},
		signatures={"f": FnSignature(name="f", return_type="Int")},
	)
	checked = checker.check(["f"])
	msgs = [d.message for d in checked.diagnostics]
	assert any("not FnResult" in m for m in msgs)


def test_can_throw_false_fnresult_return_flags_diagnostic():
	"""Declaring non-can-throw while returning FnResult should be rejected."""
	checker = Checker(
		declared_can_throw={"g": False},
		signatures={"g": FnSignature(name="g", return_type="FnResult<Int, Error>")},
	)
	checked = checker.check(["g"])
	msgs = [d.message for d in checked.diagnostics]
	assert any("returns FnResult but is not declared can-throw" in m for m in msgs)
