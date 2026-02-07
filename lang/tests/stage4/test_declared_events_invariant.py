"""
Stage4: declared events should cover all thrown events.
"""

from __future__ import annotations

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.stage4.throw_checks import FuncThrowInfo, enforce_declared_events_superset
from lang.driftc.core.function_id import FunctionId


def test_enforce_declared_events_superset_flags_extra_events():
	"""
	If a function declares throws {A} but actually throws {A, B}, stage4 should flag it.
	"""
	fn_id = FunctionId(module="main", name="f", ordinal=0)
	func_infos = {
		fn_id: FuncThrowInfo(
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
