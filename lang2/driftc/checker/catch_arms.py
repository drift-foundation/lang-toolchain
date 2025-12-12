# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-06
"""
Catch arm validation helpers (checker-side).

These helpers live in the checker so that structural rules around catch arms
are enforced before lowering. Lowering still defends against obviously bad
shapes, but user-facing diagnostics belong here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Set

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.span import Span


@dataclass
class CatchArmInfo:
	"""Syntactic info about a catch arm (event FQN and optional source loc)."""

	event_fqn: Optional[str]  # None = catch-all
	span: Span = field(default_factory=Span)


def _format_span(span: Span) -> str:
	parts = []
	if span.file:
		parts.append(span.file)
	if span.line is not None:
		col = span.column if span.column is not None else 0
		parts.append(f"{span.line}:{col}")
	return ":".join(parts) if parts else "<unknown location>"


def _report(
	msg: str,
	diagnostics: Optional[list[Diagnostic]],
	span: Span,
	notes: Optional[list[str]] = None,
) -> None:
	"""Append a diagnostic if provided, otherwise raise RuntimeError."""
	if diagnostics is not None:
		diagnostics.append(Diagnostic(message=msg, severity="error", span=span, notes=notes or []))
	else:
		raise RuntimeError(msg)


def validate_catch_arms(
	arms: Sequence[CatchArmInfo],
	known_events: Set[str],
	diagnostics: Optional[list[Diagnostic]] = None,
) -> None:
	"""
	Validate a list of catch arms:

	- at most one catch-all arm (event_fqn is None)
	- catch-all must be the last arm if present
	- no duplicate specific event arms
	- specific event names must be in the known_events set

	Diagnostics are appended when provided; otherwise a RuntimeError is raised.
	"""
	seen_events: Set[str] = set()
	catch_all_seen = False
	catch_all_idx: int | None = None
	catch_all_span: Span = Span()
	event_spans: dict[str, Span] = {}
	for idx, arm in enumerate(arms):
		if arm.event_fqn is None:
			if catch_all_seen:
				notes = [f"first catch-all is here: {_format_span(catch_all_span)}"] if catch_all_span else None
				_report("multiple catch-all arms are not allowed", diagnostics, arm.span, notes=notes)
			catch_all_seen = True
			catch_all_idx = idx
			catch_all_span = arm.span
		else:
			if arm.event_fqn in seen_events:
				prev_span = event_spans.get(arm.event_fqn)
				notes = [f"previous catch for '{arm.event_fqn}' is here: {_format_span(prev_span)}"] if prev_span else None
				_report(f"duplicate catch arm for event {arm.event_fqn}", diagnostics, arm.span, notes=notes)
			if arm.event_fqn not in known_events:
				_report(f"unknown catch event {arm.event_fqn}", diagnostics, arm.span)
			seen_events.add(arm.event_fqn)
			event_spans.setdefault(arm.event_fqn, arm.span)
	# catch-all must be last
	if catch_all_seen and catch_all_idx is not None and catch_all_idx != len(arms) - 1:
		_report("catch-all must be the last catch arm", diagnostics, catch_all_span)
