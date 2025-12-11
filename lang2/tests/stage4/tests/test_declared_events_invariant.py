"""
Stage4: declared events should cover all thrown events.
"""

from __future__ import annotations

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.stage3.throw_summary import ThrowSummary
from lang2.driftc.stage4.throw_checks import FuncThrowInfo, enforce_declared_events_superset


def test_enforce_declared_events_superset_flags_extra_events():
	"""
	If a function declares throws {A} but actually throws {A, B}, stage4 should flag it.
	"""
	func_infos = {
		"f": FuncThrowInfo(
			constructs_error=False,
			exception_types={"EvtA", "EvtB"},
			may_fail_sites=set(),
			declared_can_throw=True,
			return_type_id=None,
			declared_events={"EvtA"},
		)
	}
	diagnostics: list[Diagnostic] = []

	enforce_declared_events_superset(func_infos, diagnostics=diagnostics)

	assert diagnostics, "Expected a diagnostic for throwing undeclared events"
	msgs = [d.message for d in diagnostics]
	assert any("EvtB" in m for m in msgs)
