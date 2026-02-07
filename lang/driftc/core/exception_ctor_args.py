# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Shared exception-constructor argument resolution.

Surface syntax uses constructor-call style for exceptions:

  throw MyErr(arg0, field = expr, ...)

The compiler keeps exception schemas (field name lists) keyed by event FQN
string. This helper maps positional/keyword arguments to the declared field
names and reports schema violations with source spans.

Why this lives in core:
  - Both the checker and HIR→MIR lowering need the same mapping rules.
  - Keeping it centralized avoids the “update 2 places per feature” trap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from lang.driftc.core.diagnostics import Diagnostic

# Exception constructor diagnostics are typecheck-phase.
def _ex_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)

from lang.driftc.core.span import Span

@dataclass(frozen=True)
class KwArg:
	"""
	Minimal kw-arg shape for mapping. (Kept local to avoid stage coupling.)

	`name_span` should point at the keyword name token (not the value).
	"""

	name: str
	value: object
	name_span: Span


def _format_span(span: Span) -> str:
	"""Render a best-effort span for use in diagnostic notes."""
	if span.file and span.line and span.column:
		return f"{span.file}:{span.line}:{span.column}"
	if span.line and span.column:
		return f"{span.line}:{span.column}"
	return "<unknown>"


def resolve_exception_ctor_args(
	*,
	event_fqn: str,
	declared_fields: Optional[Sequence[str]],
	pos_args: Sequence[Tuple[object, Span]],
	kw_args: Sequence[KwArg],
	span: Span,
) -> Tuple[List[Tuple[str, object]], List[Diagnostic]]:
	"""
	Resolve constructor-call arguments into a `(field_name, value)` list.

	- `declared_fields` is the exception schema in declaration order. If None,
	  the schema is unknown (e.g., undeclared exception); the resolver will
	  return diagnostics and no resolved fields.
	- `pos_args` is a list of `(value, span)` in source order.
	- `kw_args` is a list of keyword args in source order.
	- `span` is the overall constructor span used when no tighter location is
	  available.

	Returns `(resolved_fields, diagnostics)`, where `resolved_fields` is in
	declaration order.
	"""

	diagnostics: List[Diagnostic] = []

	if declared_fields is None:
		diagnostics.append(
			_ex_diag(
				message=f"unknown exception event '{event_fqn}'",
				severity="error",
				span=span,
			)
		)
		return [], diagnostics

	declared = list(declared_fields)
	if not declared:
		if pos_args or kw_args:
			# The user must still write `MyErr()` syntactically, but providing any
			# arguments is a schema mismatch.
			arg_span = pos_args[0][1] if pos_args else kw_args[0].name_span
			diagnostics.append(
				_ex_diag(
					message=f"exception '{event_fqn}' declares no fields; no arguments are allowed",
					severity="error",
					span=arg_span,
				)
			)
		return [], diagnostics

	if len(pos_args) > len(declared):
		diagnostics.append(
			_ex_diag(
				message=f"too many positional arguments for exception '{event_fqn}'",
				severity="error",
				span=pos_args[len(declared)][1],
			)
		)
		# Continue collecting other errors (unknown/duplicates) where possible.

	assigned: dict[str, object] = {}
	assigned_span: dict[str, Span] = {}

	# 1) Positional arguments map to schema field order.
	for idx, (val, val_span) in enumerate(pos_args[: len(declared)]):
		field = declared[idx]
		assigned[field] = val
		assigned_span[field] = val_span

	# 2) Keyword arguments fill remaining fields.
	for kw in kw_args:
		name = kw.name
		if name not in declared:
			diagnostics.append(
				_ex_diag(
					message=f"unknown exception field '{name}' for '{event_fqn}'",
					severity="error",
					span=kw.name_span,
				)
			)
			continue
		if name in assigned:
			diagnostics.append(
				_ex_diag(
					message=f"duplicate exception field '{name}' for '{event_fqn}'",
					severity="error",
					span=kw.name_span,
					notes=[f"first assignment is here: {_format_span(assigned_span.get(name, span))}"],
				)
			)
			continue
		assigned[name] = kw.value
		assigned_span[name] = kw.name_span

	# 3) Missing fields are errors; we require exact coverage.
	missing = [f for f in declared if f not in assigned]
	if missing:
		diagnostics.append(
			_ex_diag(
				message=f"missing exception field(s) for '{event_fqn}': {', '.join(missing)}",
				severity="error",
				span=span,
			)
		)

	# 4) Produce resolved list in schema order.
	resolved: List[Tuple[str, object]] = [(f, assigned[f]) for f in declared if f in assigned]
	return resolved, diagnostics
