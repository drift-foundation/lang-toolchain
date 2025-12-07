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
	type_table: Optional["TypeTable"] = None
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
		self._unknown_type = self._type_table.new_unknown("Unknown")

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
			type_table=self._type_table,
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

	def build_type_env_from_ssa(
		self,
		ssa_funcs: Mapping[str, "SsaFunc"],
		signatures: Mapping[str, FnSignature],
	) -> Optional["CheckerTypeEnv"]:
		"""
		Assign TypeIds to SSA values using checker signatures and simple heuristics.

		This is a minimal pass: it handles constants, ConstructResultOk/Err, Call/
		MethodCall, AssignSSA copies, and Phi when incoming types agree. Unknowns
		default to `Unknown` TypeId. Returns None if no types were assigned.
		"""
		from lang2.checker.type_env_impl import CheckerTypeEnv
		from lang2.stage2 import ConstructResultOk, ConstructResultErr, Call, MethodCall, ConstInt, ConstBool, ConstString, AssignSSA, Phi
		value_types: Dict[tuple[str, str], TypeId] = {}

		# Helper to fetch a mapped type with Unknown fallback.
		def ty_for(fn: str, val: str) -> TypeId:
			return value_types.get((fn, val), self._unknown_type)

		changed = True
		# Fixed-point with a small iteration cap.
		for _ in range(5):
			if not changed:
				break
			changed = False
			for fn_name, ssa in ssa_funcs.items():
				sig = signatures.get(fn_name)
				fn_return_parts: tuple[TypeId, TypeId] | None = None
				if sig and sig.return_type_id is not None:
					td = self._type_table.get(sig.return_type_id)
					if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
						fn_return_parts = (td.param_types[0], td.param_types[1])

				for block in ssa.func.blocks.values():
					for instr in block.instructions:
						dest = getattr(instr, "dest", None)
						if isinstance(instr, ConstInt) and dest is not None:
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._int_type
								changed = True
						elif isinstance(instr, ConstBool) and dest is not None:
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._bool_type
								changed = True
						elif isinstance(instr, ConstString) and dest is not None:
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._type_table.new_scalar("String")
								changed = True
						elif isinstance(instr, ConstructResultOk):
							if dest is None:
								continue
							ok_ty = ty_for(fn_name, instr.value)
							err_ty = fn_return_parts[1] if fn_return_parts else self._error_type
							dest_ty = self._type_table.new_fnresult(ok_ty, err_ty)
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ConstructResultErr):
							if dest is None:
								continue
							err_ty = ty_for(fn_name, instr.error)
							ok_ty = fn_return_parts[0] if fn_return_parts else self._unknown_type
							dest_ty = self._type_table.new_fnresult(ok_ty, err_ty)
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, Call) and dest is not None:
							callee_sig = signatures.get(instr.fn)
							if callee_sig and callee_sig.return_type_id is not None:
								dest_ty = callee_sig.return_type_id
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, MethodCall) and dest is not None:
							callee_sig = signatures.get(instr.method_name)
							if callee_sig and callee_sig.return_type_id is not None:
								dest_ty = callee_sig.return_type_id
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, AssignSSA):
							if dest is None:
								continue
							src_ty = value_types.get((fn_name, instr.src))
							if src_ty is not None and value_types.get((fn_name, dest)) != src_ty:
								value_types[(fn_name, dest)] = src_ty
								changed = True
						elif isinstance(instr, Phi):
							if dest is None:
								continue
							incoming = [value_types.get((fn_name, v)) for v in instr.incoming.values()]
							incoming = [t for t in incoming if t is not None]
							if incoming and all(t == incoming[0] for t in incoming):
								ty = incoming[0]
								if value_types.get((fn_name, dest)) != ty:
									value_types[(fn_name, dest)] = ty
									changed = True

					term = block.terminator
					if hasattr(term, "value") and getattr(term, "value") is not None:
						val = term.value
						if fn_return_parts is not None:
							ty = self._type_table.new_fnresult(fn_return_parts[0], fn_return_parts[1])
						else:
							ty = value_types.get((fn_name, val), self._unknown_type)
						if value_types.get((fn_name, val)) != ty:
							value_types[(fn_name, val)] = ty
							changed = True

		if not value_types:
			return None
		return CheckerTypeEnv(self._type_table, value_types)
