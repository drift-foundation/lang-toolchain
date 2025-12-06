# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Minimal checker stub for lang2.

This module exists solely to give the driver a place to hang checker-provided
metadata (currently: `declared_can_throw`). It is *not* a full type checker and
should be replaced by a real implementation once the type system is wired in.

When the real checker lands, this package will:

* resolve function signatures (`FnResult` return, `throws(...)` clause),
* validate catch-arm event names against the exception catalog, and
* populate a concrete `TypeEnv` for stage4 type-aware checks.

For now we only thread a boolean throw intent per function through to the driver
and validate catch-arm shapes when provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, FrozenSet, Mapping, Sequence, Set, Tuple

from lang2.diagnostics import Diagnostic
from lang2.types_protocol import TypeEnv
from lang2.checker.catch_arms import CatchArmInfo, validate_catch_arms
from lang2.types_core import TypeTable, TypeId, TypeKind
from lang2.types_core import TypeTable, TypeId, TypeKind


@dataclass
class FnSignature:
	"""
	Placeholder function signature used by the stub checker.

	Only `name`, `return_type`, and optional `throws_events` are represented.
	The real checker will replace this with its own type-checked signature
	structure.
	"""

	name: str
	return_type: Any
	throws_events: Tuple[str, ...] = ()
	return_type_id: Optional[TypeId] = None  # resolved TypeId (checker-owned)
	error_type_id: Optional[TypeId] = None   # resolved error TypeId


@dataclass
class FnInfo:
	"""
	Per-function checker metadata (placeholder).

	Only `name` and `declared_can_throw` are populated by this stub. Real
	`FnInfo` will carry richer information such as declared event set, return
	type, and source span for diagnostics.
	"""

	name: str
	declared_can_throw: bool

	# Optional fields reserved for the real checker; left as None here.
	declared_events: Optional[FrozenSet[str]] = None
	span: Optional[Any] = None  # typically a Span/Location
	return_type: Optional[Any] = None  # legacy placeholder (to be TypeId)
	error_type: Optional[Any] = None   # legacy placeholder (to be TypeId)
	return_type_id: Optional[TypeId] = None
	error_type_id: Optional[TypeId] = None


@dataclass
class CheckedProgram:
	"""
	Container returned by the checker.

	In the stub this only carries fn_infos; real implementations will also
	provide a concrete TypeEnv, diagnostics, and the exception catalog for
	catch/throws validation.
	"""

	fn_infos: Dict[str, FnInfo]
	type_env: Optional[TypeEnv] = None
	exception_catalog: Optional[Dict[str, int]] = None
	diagnostics: List[Diagnostic] = field(default_factory=list)


class Checker:
	"""
	Placeholder checker.

	Accepts a sequence of function declarations and an optional declared_can_throw
	map (defaults to False for all). This input is strictly a testing shim; new
	callers should prefer `signatures` and treat `declared_can_throw` as a
	deprecated convenience. A real checker will compute declared_can_throw from
	signatures (FnResult/throws) and the type system, and validate catch arms
	against an exception catalog. The `declared_can_throw` map is a legacy path
	and slated for removal once real signatures land.
	"""

	def __init__(
		self,
		declared_can_throw: Mapping[str, bool] | None = None,
		signatures: Mapping[str, FnSignature] | None = None,
		catch_arms: Mapping[str, Sequence[CatchArmInfo]] | None = None,
		exception_catalog: Mapping[str, int] | None = None,
	) -> None:
		# Until a real type checker exists we support two testing shims:
		# 1) an explicit name -> bool map, or
		# 2) a name -> FnSignature map, from which we can infer can-throw based
		#    on the return type resembling FnResult.
		self._declared_map = declared_can_throw or {}
		self._signatures = signatures or {}
		self._catch_arms = catch_arms or {}
		self._exception_catalog = dict(exception_catalog) if exception_catalog else None
		# Naive type table for return type resolution; real checker will own this.
		self._type_table = TypeTable()
		self._int_type = self._type_table.new_scalar("Int")
		self._bool_type = self._type_table.new_scalar("Bool")
		self._error_type = self._type_table.new_error("Error")

	def check(self, fn_decls: Iterable[str]) -> CheckedProgram:
		"""
		Produce a CheckedProgram with FnInfo for each fn name in `fn_decls`.

		This stub also validates any provided catch arms against the
		exception catalog when available, accumulating diagnostics instead
		of raising.
		"""
		fn_infos: Dict[str, FnInfo] = {}
		diagnostics: List[Diagnostic] = []
		known_events: Set[str] = set(self._exception_catalog.keys()) if self._exception_catalog else set()

		for name in fn_decls:
			declared_can_throw = self._declared_map.get(name)
			sig = self._signatures.get(name)
			declared_events: Optional[FrozenSet[str]] = None
			return_type = None
			return_type_id: Optional[TypeId] = None
			error_type_id: Optional[TypeId] = None

			if sig is not None:
				declared_events = frozenset(sig.throws_events) if sig.throws_events else None
				return_type = sig.return_type
				return_type_id, error_type_id = self._resolve_signature_types(sig)
				sig.return_type_id = return_type_id
				sig.error_type_id = error_type_id
				if declared_events is None and sig.throws_events:
					declared_events = frozenset(sig.throws_events)

			if declared_can_throw is None:
				if sig is not None:
					declared_can_throw = self._is_fnresult_return(sig.return_type)
				else:
					declared_can_throw = False

			catch_arms = self._catch_arms.get(name)
			if catch_arms is not None:
				validate_catch_arms(catch_arms, known_events, diagnostics)

			fn_infos[name] = FnInfo(
				name=name,
				declared_can_throw=declared_can_throw,
				declared_events=declared_events,
				return_type=return_type,
				return_type_id=return_type_id,
				error_type_id=error_type_id,
			)

		# TODO: real checker will:
		#   - resolve signatures (FnResult/throws),
		#   - collect catch arms per function and validate them against the exception catalog,
		#   - build a concrete TypeEnv and diagnostics list.
		# The real checker will attach type_env, diagnostics, and exception_catalog.
		return CheckedProgram(
			fn_infos=fn_infos,
			type_env=None,
			exception_catalog=self._exception_catalog,
			diagnostics=diagnostics,
		)

	def _is_fnresult_return(self, return_type: Any) -> bool:
		"""
		Best-effort predicate to decide if a return type resembles FnResult<_, Error>.

		This is intentionally loose to avoid committing to a concrete type
		representation before the real checker exists. For now we consider:

		* strings containing 'FnResult'
		* tuples shaped like ('FnResult', ok_ty, err_ty)
		"""
		if isinstance(return_type, str):
			return "FnResult" in return_type
		if isinstance(return_type, tuple) and return_type and return_type[0] == "FnResult":
			return True
		return False

	def _resolve_signature_types(self, sig: FnSignature) -> tuple[Optional[TypeId], Optional[TypeId]]:
		"""
		Naively map a signature's return type into TypeIds using the TypeTable.

		This is a stopgap until real type resolution exists; it recognizes:
		- strings containing 'FnResult' -> FnResult<Int, Error>
		- tuple ('FnResult', ok, err) -> FnResult of naive ok/err mapping
		- strings 'Int'/'Bool' -> scalar types
		- fallback: Unknown
		"""
		rt = sig.return_type
		if isinstance(rt, str):
			if "FnResult" in rt:
				return self._type_table.new_fnresult(self._int_type, self._error_type), self._error_type
			if rt == "Int":
				return self._int_type, None
			if rt == "Bool":
				return self._bool_type, None
			# Unknown string maps to a scalar placeholder
			return self._type_table.new_scalar(rt), None
		if isinstance(rt, tuple):
			if len(rt) == 3 and rt[0] == "FnResult":
				ok = self._map_opaque(rt[1])
				err = self._map_opaque(rt[2])
				return self._type_table.new_fnresult(ok, err), err
			if len(rt) == 2:
				ok = self._map_opaque(rt[0])
				err = self._map_opaque(rt[1])
				return self._type_table.new_fnresult(ok, err), err
		# Fallback unknown
		return self._type_table.new_unknown("UnknownReturn"), None

	def _map_opaque(self, val: Any) -> TypeId:
		"""Naively map an opaque return component into a TypeId."""
		if isinstance(val, str):
			if val == "Int":
				return self._int_type
			if val == "Bool":
				return self._bool_type
			if "Error" in val:
				return self._error_type
			return self._type_table.new_scalar(val)
		if isinstance(val, tuple):
			if len(val) == 2:
				ok = self._map_opaque(val[0])
				err = self._map_opaque(val[1])
				return self._type_table.new_fnresult(ok, err)
			if len(val) >= 3 and val[0] == "FnResult":
				ok = self._map_opaque(val[1])
				err = self._map_opaque(val[2])
				return self._type_table.new_fnresult(ok, err)
		return self._type_table.new_unknown(str(val))
