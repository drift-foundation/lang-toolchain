#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Callable resolution atop CallableRegistry (v1).

This module applies the v1 rules:
- Exact arity/type equality for overload selection.
- Receiver compatibility via self_mode with auto-borrow T -> &T / &mut T only.
- No return-type overloading, no inference-based overload picking.

It returns a single CallableDecl and any inferred receiver auto-borrow kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from lang2.driftc.core.types_core import TypeId, TypeKind, TypeTable
from lang2.driftc.method_registry import (
	CallableRegistry,
	CallableDecl,
	CallableSignature,
	SelfMode,
	Visibility,
	ModuleId,
)


class ResolutionError(ValueError):
	"""Raised when no viable or ambiguous callables are found."""

	def __init__(self, message: str, *, span: object | None = None) -> None:
		super().__init__(message)
		self.span = span


@dataclass(frozen=True)
class MethodResolution:
	"""Resolved method info with receiver auto-borrow kind if applied."""

	decl: CallableDecl
	receiver_autoborrow: Optional[SelfMode]  # None, SELF_BY_REF, or SELF_BY_REF_MUT
	result_type: Optional[TypeId] = None


def _unwrap_ref(type_table: TypeTable, ty: TypeId) -> TypeId:
	"""
	Peel a single &T or &mut T layer. V1 assumes at most one ref layer on the
	receiver; nested refs (&&T) are not supported yet.
	"""
	td = type_table.get(ty)
	if td.kind is TypeKind.REF and td.param_types:
		return td.param_types[0]
	return ty


def _receiver_compat(
	type_table: TypeTable, receiver_type: TypeId, decl: CallableDecl
) -> Tuple[bool, Optional[SelfMode]]:
	"""
	Check if a receiver type is compatible with the decl's receiver param and self_mode.
	Returns (is_compatible, autoborrow_kind)
	"""
	if decl.self_mode is None or not decl.signature.param_types:
		return False, None
	param_self = decl.signature.param_types[0]
	mode = decl.self_mode

	# By-value: require exact match.
	if mode is SelfMode.SELF_BY_VALUE:
		return (receiver_type == param_self), None

	td_param = type_table.get(param_self)
	td_recv = type_table.get(receiver_type)

	if mode is SelfMode.SELF_BY_REF:
		# Already a ref of the right mutability?
		if receiver_type == param_self and td_recv.kind is TypeKind.REF and td_recv.ref_mut is False:
			return True, None
		# Auto-borrow from T to &T
		if td_param.kind is TypeKind.REF and td_param.ref_mut is False and _unwrap_ref(type_table, param_self) == receiver_type:
			return True, SelfMode.SELF_BY_REF
		return False, None

	if mode is SelfMode.SELF_BY_REF_MUT:
		if receiver_type == param_self and td_recv.kind is TypeKind.REF and td_recv.ref_mut is True:
			return True, None
		if td_param.kind is TypeKind.REF and td_param.ref_mut is True and _unwrap_ref(type_table, param_self) == receiver_type:
			return True, SelfMode.SELF_BY_REF_MUT
		return False, None

	return False, None


def resolve_function_call(
	registry: CallableRegistry,
	type_table: TypeTable,
	*,
	name: str,
	arg_types: List[TypeId],
	visible_modules: Iterable[ModuleId],
	current_module: Optional[ModuleId] = None,
) -> CallableDecl:
	candidates = registry.get_free_candidates(
		name=name, visible_modules=visible_modules, include_private_in=current_module
	)
	viable: List[CallableDecl] = []
	for decl in candidates:
		if decl.is_generic:
			continue  # v1: skip unresolved generics
		params = decl.signature.param_types
		if len(params) != len(arg_types):
			continue
		if all(p == a for p, a in zip(params, arg_types)):
			viable.append(decl)
	if not viable:
		raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
	if len(viable) > 1:
		raise ResolutionError(f"ambiguous call to function '{name}' with args {arg_types}")
	return viable[0]


def resolve_method_call(
	registry: CallableRegistry,
	type_table: TypeTable,
	*,
	receiver_type: TypeId,
	method_name: str,
	arg_types: List[TypeId],
	visible_modules: Iterable[ModuleId],
	current_module: Optional[ModuleId] = None,
) -> MethodResolution:
	receiver_nominal = _unwrap_ref(type_table, receiver_type)
	candidates = registry.get_method_candidates(
		receiver_nominal_type_id=receiver_nominal,
		name=method_name,
		visible_modules=visible_modules,
		include_private_in=current_module,
	)
	viable: List[MethodResolution] = []
	for decl in candidates:
		if decl.is_generic:
			continue  # skip unresolved generics in v1
		params = decl.signature.param_types
		if len(params) - 1 != len(arg_types):
			continue
		compatible, autoborrow_kind = _receiver_compat(type_table, receiver_type, decl)
		if not compatible:
			continue
		if all(p == a for p, a in zip(params[1:], arg_types)):
			viable.append(MethodResolution(decl=decl, receiver_autoborrow=autoborrow_kind))
	if not viable:
		raise ResolutionError(f"no matching method '{method_name}' for receiver {receiver_type} and args {arg_types}")
	if len(viable) > 1:
		raise ResolutionError(f"ambiguous method '{method_name}' for receiver {receiver_type} and args {arg_types}")
	return viable[0]


__all__ = [
	"resolve_function_call",
	"resolve_method_call",
	"ResolutionError",
	"MethodResolution",
]
