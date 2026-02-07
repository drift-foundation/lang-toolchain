#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Small inference context/result objects used by the type checker.

These are intentionally lightweight and do not depend on the full type checker
implementation, so they can be reused by other subsystems without pulling in
large imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal

from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeId, TypeParamId


class InferErrorKind(str, Enum):
	ARITY = "arity"
	NO_TYPES = "no_types"
	NO_TYPEPARAMS = "no_typeparams"
	TYPEARG_COUNT = "typearg_count"
	CANNOT_INFER = "cannot_infer"
	CONFLICT = "conflict"
	MISMATCH = "mismatch"
	BLOCKED = "blocked"


@dataclass(frozen=True)
class InferConstraintOrigin:
	kind: Literal["receiver", "arg", "expected_return", "ctor_expected", "ctor_field"]
	index: int | None = None
	name: str | None = None

	def label(self) -> str:
		if self.kind == "receiver":
			return "receiver"
		if self.kind == "arg":
			if self.index is not None and self.name:
				return f"arg #{self.index} ('{self.name}')"
			if self.index is not None:
				return f"arg #{self.index}"
			return "arg"
		if self.kind == "expected_return":
			return "expected return"
		if self.kind == "ctor_expected":
			return "expected type"
		if self.kind == "ctor_field":
			if self.name:
				return f"field '{self.name}'"
			return "field"
		return self.kind


@dataclass(frozen=True)
class InferConstraint:
	lhs: TypeId
	rhs: TypeId
	origin: InferConstraintOrigin
	span: Span


@dataclass(frozen=True)
class InferBindingEvidence:
	param_id: TypeParamId
	bound_to: TypeId
	origin: InferConstraintOrigin
	span: Span


@dataclass(frozen=True)
class InferConflictEvidence:
	lhs: TypeId
	rhs: TypeId
	origin: InferConstraintOrigin
	span: Span
	param_id: TypeParamId | None = None


@dataclass
class InferTrace:
	bindings: dict[TypeParamId, list[InferBindingEvidence]] = field(default_factory=dict)
	conflicts: list[InferConflictEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class InferError:
	kind: InferErrorKind
	missing_params: list[TypeParamId] = field(default_factory=list)
	conflicts: list[InferConflictEvidence] = field(default_factory=list)
	expected_count: int | None = None


@dataclass
class InferContext:
	call_kind: Literal["free", "method", "ufcs", "ctor", "call"]
	call_name: str
	span: Span
	type_param_ids: list[TypeParamId]
	type_param_names: dict[TypeParamId, str]
	param_types: list[TypeId]
	param_names: list[str] | None
	return_type: TypeId | None
	arg_types: list[TypeId]
	receiver_type: TypeId | None = None
	expected_return: TypeId | None = None
	ctor_field_types: dict[str, TypeId] | None = None
	debug_infer: bool = False


@dataclass
class InferResult:
	ok: bool
	subst: object | None
	inst_params: list[TypeId] | None
	inst_return: TypeId | None
	trace: InferTrace = field(default_factory=InferTrace)
	error: InferError | None = None
	context: InferContext | None = None


def format_infer_failure(
	ctx: InferContext,
	res: InferResult,
	*,
	label_typeid: Callable[[TypeId], str],
) -> tuple[str, list[str]]:
	err = res.error
	if err is None:
		return "cannot infer type arguments", []
	label = ctx.call_name
	if ctx.call_kind == "ctor":
		label = f"struct '{ctx.call_name}'"
	elif ctx.call_kind in {"method", "ufcs"}:
		label = f"method '{ctx.call_name}'"
	else:
		label = f"'{ctx.call_name}'"
	if err.kind is InferErrorKind.CONFLICT and err.conflicts:
		msg = f"cannot infer type arguments for {label}: conflicting constraints"
		notes: list[str] = []
		conflict = err.conflicts[0]
		param = None
		if conflict.param_id is not None:
			param = ctx.type_param_names.get(conflict.param_id, "T")
		if param:
			notes.append(f"conflict for {param}")
		notes.append(
			f"{conflict.origin.label()} requires {label_typeid(conflict.lhs)}"
		)
		notes.append(
			f"{conflict.origin.label()} conflicts with {label_typeid(conflict.rhs)}"
		)
		return msg, notes
	if err.kind is InferErrorKind.CANNOT_INFER and err.missing_params:
		names = [
			ctx.type_param_names.get(pid, f"T{idx}") for idx, pid in enumerate(err.missing_params)
		]
		missing = ", ".join(names)
		msg = f"cannot infer type arguments for {label}: {missing}"
		notes = [
			f"no constraints for {name} (not determined from receiver, arguments, or expected type)"
			for name in names
		]
		notes.append("add explicit '<type ...>' or provide an expected type")
		return msg, notes
	msg = f"cannot infer type arguments for {label}; add explicit '<type ...>'"
	return msg, []


__all__ = [
	"InferConstraintOrigin",
	"InferConstraint",
	"InferBindingEvidence",
	"InferConflictEvidence",
	"InferTrace",
	"InferErrorKind",
	"InferError",
	"InferContext",
	"InferResult",
	"format_infer_failure",
]
