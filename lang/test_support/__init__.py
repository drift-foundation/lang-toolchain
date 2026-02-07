# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Shared helpers for tests that need checker inputs.

These helpers avoid re-spelling FnSignature construction, declared_can_throw
derivation via the checker stub, and exception catalog setup. They also provide
lightweight builders that accept AST/HIR-ish function declarations so test data
looks closer to what the real front-end will feed into the checker.

Note on `FnResult`:
  lang does not expose `FnResult` as a surface language type. The compiler may
  still use `FnResult<T, Error>` internally as an ABI carrier for can-throw
  functions. Tests should model can-throw intent via `FnSignature.declared_can_throw`
  (or via `hir_blocks` inference), not by writing `FnResult<...>` return types.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId


def make_signatures(
	return_types: Mapping[object, Any],
	throws_events: Mapping[object, Sequence[str]] | None = None,
	declared_can_throw: Mapping[object, bool] | None = None,
) -> Dict[object, FnSignature]:
	"""
	Build a name -> FnSignature map from simple return-type/throws inputs.

	Args:
	  return_types: mapping of function name -> return type (string/tuple placeholder).
	  throws_events: optional mapping of function name -> iterable of event names.
	"""
	signatures: Dict[object, FnSignature] = {}
	for key, ret_ty in return_types.items():
		events = tuple(throws_events[key]) if throws_events and key in throws_events else ()
		explicit = declared_can_throw.get(key) if declared_can_throw else None
		name = key.name if isinstance(key, FunctionId) else key
		signatures[key] = FnSignature(
			name=name,
			return_type=ret_ty,
			throws_events=events,
			declared_can_throw=explicit,
		)
	return signatures


def declared_from_signatures(signatures: Mapping[object, FnSignature]) -> Dict[object, bool]:
	"""
	Derive the explicit can-throw intent from signatures.

	In the current lang model, can-throw is not implied by the surface return
	type. This helper therefore returns `bool(sig.declared_can_throw)` for each
	signature, defaulting to False when unspecified.
	"""
	return {key: bool(sig.declared_can_throw) for key, sig in signatures.items()}


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
	  - declared_can_throw: Optional[bool] (optional)
	"""
	signatures: Dict[str, FnSignature] = {}
	for decl in func_decls:
		name = getattr(decl, "name")
		ret_ty = getattr(decl, "return_type", None)
		throws = _throws_from_decl(decl)
		explicit = getattr(decl, "declared_can_throw", None)
		if explicit is None and throws:
			# Decl-like stubs frequently use `throws_events` as the only explicit
			# "this may throw" marker. Treat that as can-throw intent.
			explicit = True
		if explicit is None:
			# Surface ABI rule: nothrow is the only way to force non-throwing.
			explicit = True
		signatures[name] = FnSignature(name=name, return_type=ret_ty, throws_events=throws, declared_can_throw=explicit)
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
