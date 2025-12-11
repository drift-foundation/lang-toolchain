# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-06
"""
Catch arm validation helpers (checker-side).

These helpers live in the checker so that structural rules around catch arms
are enforced before lowering. Lowering still defends against obviously bad
shapes (multiple catch-alls, catch-all not last) as a last-resort guard, but
user-facing diagnostics belong here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Set

from lang2.driftc.core.diagnostics import Diagnostic


@dataclass
class CatchArmInfo:
	"""Syntactic info about a catch arm (event name and optional span/binder)."""

	event_name: Optional[str]  # None = catch-all
	span: Optional[str] = None  # placeholder for a real Span/Loc type


def _report(msg: str, diagnostics: Optional[list[Diagnostic]]) -> None:
	"""Append a diagnostic if provided, otherwise raise RuntimeError."""
	if diagnostics is not None:
		diagnostics.append(Diagnostic(message=msg, severity="error", span=None))
	else:
		raise RuntimeError(msg)


def validate_catch_arms(
	arms: Sequence[CatchArmInfo],
	known_events: Set[str],
	diagnostics: Optional[list[Diagnostic]] = None,
) -> None:
	"""
	Validate a list of catch arms:

	- at most one catch-all arm (event_name is None)
	- catch-all must be the last arm if present
	- no duplicate specific event arms
	- specific event names must be in the known_events set

	Raises RuntimeError on the first violation; real checker will emit proper
	diagnostics instead of exceptions.
	"""
	seen_events: Set[str] = set()
	catch_all_seen = False
	for idx, arm in enumerate(arms):
		if arm.event_name is None:
			if catch_all_seen:
				_report("multiple catch-all arms are not allowed", diagnostics)
			catch_all_seen = True
		else:
			if arm.event_name in seen_events:
				_report(f"duplicate catch arm for event {arm.event_name}", diagnostics)
			if arm.event_name not in known_events:
				_report(f"unknown catch event {arm.event_name}", diagnostics)
			seen_events.add(arm.event_name)
		# catch-all must be last
		if catch_all_seen and idx != len(arms) - 1:
			_report("catch-all must be the last catch arm", diagnostics)
