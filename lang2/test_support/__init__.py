# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Shared helpers for tests that need checker inputs.

These helpers avoid re-spelling FnSignature construction, declared_can_throw
derivation via the checker stub, and exception catalog setup. They also provide
lightweight builders that accept AST/HIR-ish function declarations so test data
looks closer to what the real front-end will feed into the checker.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from lang2.driftc.checker import Checker, FnSignature


def make_signatures(
	return_types: Mapping[str, Any],
	throws_events: Mapping[str, Sequence[str]] | None = None,
) -> Dict[str, FnSignature]:
	"""
	Build a name -> FnSignature map from simple return-type/throws inputs.

	Args:
	  return_types: mapping of function name -> return type (string/tuple placeholder).
	  throws_events: optional mapping of function name -> iterable of event names.
	"""
	signatures: Dict[str, FnSignature] = {}
	for name, ret_ty in return_types.items():
		events = tuple(throws_events[name]) if throws_events and name in throws_events else ()
		signatures[name] = FnSignature(name=name, return_type=ret_ty, throws_events=events)
	return signatures


def declared_from_signatures(signatures: Mapping[str, FnSignature]) -> Dict[str, bool]:
	"""
	Derive declared_can_throw via the checker stub given FnSignature inputs.
	"""
	checked = Checker(signatures=signatures).check(signatures.keys())
	return {name: info.declared_can_throw for name, info in checked.fn_infos.items()}


def build_exception_catalog(events: Iterable[str] | Mapping[str, int]) -> Dict[str, int]:
	"""
	Construct a simple exception catalog (event FQN -> code) for tests.

	Accepts either an explicit mapping or an iterable of names (assigned 1..N).
	"""
	if isinstance(events, Mapping):
		return dict(events)
	return {name: idx for idx, name in enumerate(events, start=1)}


def signatures_from_decl_nodes(func_decls: Iterable[object]) -> Dict[str, FnSignature]:
	"""
	Build FnSignature entries from AST/HIR-like function declarations.

	Each decl is expected to have:
	  - name
	  - return_type (string/tuple placeholder is fine)
	  - throws or throws_events: iterable of event names (optional)
	"""
	signatures: Dict[str, FnSignature] = {}
	for decl in func_decls:
		name = getattr(decl, "name")
		ret_ty = getattr(decl, "return_type", None)
		throws = _throws_from_decl(decl)
		signatures[name] = FnSignature(name=name, return_type=ret_ty, throws_events=throws)
	return signatures


def exception_catalog_from_decls(func_decls: Iterable[object], base: Mapping[str, int] | None = None) -> Dict[str, int]:
	"""
	Build an exception catalog (event FQN -> code) from throws clauses on decls.

	If `base` is provided, it is copied and augmented; new events are assigned
	sequentially after the highest existing code.
	"""
	catalog: Dict[str, int] = dict(base) if base else {}
	next_code = (max(catalog.values()) + 1) if catalog else 1
	for decl in func_decls:
		for evt in _throws_from_decl(decl):
			if evt not in catalog:
				catalog[evt] = next_code
				next_code += 1
	return catalog


def _throws_from_decl(decl: object) -> Tuple[str, ...]:
	"""Extract throws/throws_events from a decl object if present."""
	throws = getattr(decl, "throws", None)
	if throws is None:
		throws = getattr(decl, "throws_events", None)
	if throws is None:
		return ()
	return tuple(throws)


__all__ = [
	"make_signatures",
	"declared_from_signatures",
	"build_exception_catalog",
	"signatures_from_decl_nodes",
	"exception_catalog_from_decls",
]
