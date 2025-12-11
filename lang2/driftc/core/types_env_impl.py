# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-05
"""
Simple testing implementations of the `TypeEnv` protocol.

These are **not** real type systems. They exist solely to let tests exercise
the type-aware FnResult checks in stage4 without pulling in a full
checker/type environment. `SimpleTypeEnv` lets tests install arbitrary type
handles; `InferredTypeEnv` can derive FnResult types from SSA + signatures.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from lang2.driftc.core.types_protocol import TypeEnv
from lang2.driftc.stage4.ssa import SsaFunc
from lang2.driftc.stage2 import (
	AssignSSA,
	Call,
	ConstructResultErr,
	ConstructResultOk,
	LoadLocal,
	MethodCall,
	Phi,
)
from lang2.driftc.checker import FnSignature
# InferredTypeEnv uses opaque types; a bridge converts to checker-owned TypeIds.


class SimpleTypeEnv(TypeEnv):
	"""
	A minimal in-memory TypeEnv for tests.

	Types are opaque handles; by convention in tests we treat a FnResult type as
	a 2-tuple `(ok_type, error_type)`. Anything else is treated as a non-FnResult
	type. This keeps stage4 decoupled from concrete type representations.
	"""

	def __init__(self) -> None:
		# Map (function name, SSA value id) -> opaque type handle.
		self._types: Dict[tuple[str, str], Any] = {}

	def set_ssa_type(self, func_name: str, value_id: str, ty: Any) -> None:
		"""Install a type handle for the given SSA value (used in tests)."""
		self._types[(func_name, value_id)] = ty

	def type_of_ssa_value(self, func_name: str, value_id: str) -> Any:
		"""Return the previously stored type for `value_id` in `func_name`."""
		return self._types[(func_name, value_id)]

	def is_fnresult(self, ty: Any) -> bool:
		"""
		Treat a 2-tuple as a FnResult `(ok_type, error_type)`; everything else is
		non-FnResult. Tests can choose whatever handles they like for the inner
		types (strings, ints, sentinel objects, etc.).
		"""
		return isinstance(ty, tuple) and len(ty) == 2

	def fnresult_parts(self, ty: Any) -> Tuple[Any, Any]:
		"""Return the (ok_type, error_type) tuple for a FnResult type."""
		if not self.is_fnresult(ty):
			raise TypeError(f"fnresult_parts called on non-FnResult type {ty!r}")
		ok_ty, err_ty = ty
		return ok_ty, err_ty


class InferredTypeEnv(TypeEnv):
	"""
	Best-effort TypeEnv built from SSA functions and (optional) signatures.

	This recognizes FnResult values produced by ConstructResultOk/Err and by
	function/method calls whose signatures return FnResult. AssignSSA copies and
	Phi nodes propagate types when incoming values agree.
	"""

	def __init__(self, types: Mapping[tuple[str, str], Any]) -> None:
		self._types: Dict[tuple[str, str], Any] = dict(types)

	def type_of_ssa_value(self, func_name: str, value_id: str) -> Any:
		"""Return the inferred type for an SSA value (raises KeyError if unknown)."""
		return self._types[(func_name, value_id)]

	def is_fnresult(self, ty: Any) -> bool:
		return _is_fnresult_type(ty)

	def fnresult_parts(self, ty: Any) -> Tuple[Any, Any]:
		if isinstance(ty, tuple):
			if len(ty) == 2:
				return ty
			if ty and ty[0] == "FnResult":
				ok = ty[1] if len(ty) > 1 else None
				err = ty[2] if len(ty) > 2 else None
				return ok, err
		# Strings or unknown shapes: treat as opaque FnResult parts.
		return (None, None)


def build_type_env_from_ssa(
	ssa_funcs: Mapping[str, SsaFunc],
	signatures: Mapping[str, FnSignature] | None = None,
) -> InferredTypeEnv:
	"""
	Derive a TypeEnv from SSA functions and (optionally) known signatures.

	Recognizes FnResult values produced by ConstructResultOk/Err and by calls
	with FnResult return types. Propagates types through AssignSSA and simple
	Phi nodes when incoming types agree.
	"""
	types: Dict[tuple[str, str], Any] = {}
	sig_map = signatures or {}

	for fname, ssa in ssa_funcs.items():
		# Seed parameter types from signatures when available so downstream
		# instructions (AssignSSA, Return) see concrete types for params.
		sig = sig_map.get(fname)
		if sig and sig.param_type_ids and ssa.func.params:
			for param_name, ty_id in zip(ssa.func.params, sig.param_type_ids):
				if ty_id is not None:
					types[(fname, param_name)] = ty_id

		# First pass: recognize direct FnResult constructions, call results from
		# known signatures, and propagate via AssignSSA/Phi when obvious.
		for block in ssa.func.blocks.values():
			for instr in block.instructions:
				if isinstance(instr, (ConstructResultOk, ConstructResultErr)):
					types[(fname, instr.dest)] = ("FnResult", None, None)
				elif isinstance(instr, Call) and instr.dest is not None:
					sig = sig_map.get(instr.fn)
					if sig is not None:
						ret_ty = sig.return_type_id if sig.return_type_id is not None else sig.return_type
						types[(fname, instr.dest)] = ret_ty
				elif isinstance(instr, MethodCall) and instr.dest is not None:
					sig = sig_map.get(instr.method_name)
					if sig is not None:
						ret_ty = sig.return_type_id if sig.return_type_id is not None else sig.return_type
						types[(fname, instr.dest)] = ret_ty
				elif isinstance(instr, AssignSSA):
					src_ty = types.get((fname, instr.src))
					if src_ty is not None:
						types[(fname, instr.dest)] = src_ty
				elif isinstance(instr, Phi):
					incoming_tys = {types.get((fname, val)) for val in instr.incoming.values()}
					incoming_tys.discard(None)
					if len(incoming_tys) == 1:
						types[(fname, instr.dest)] = incoming_tys.pop()

		# Second pass: if the function's own return type is FnResult and a return
		# value is missing a type, seed it from the signature so type-aware throw
		# checks have something to work with even when inference failed.
		fn_sig = sig_map.get(fname)
		if fn_sig:
			ret_ty = fn_sig.return_type_id if fn_sig.return_type_id is not None else fn_sig.return_type
		else:
			ret_ty = None
		if ret_ty and _is_fnresult_type(ret_ty):
			for block in ssa.func.blocks.values():
				term = block.terminator
				if hasattr(term, "value") and getattr(term, "value") is not None:
					val = term.value
					if (fname, val) not in types:
						types[(fname, val)] = ret_ty

	return InferredTypeEnv(types)


def _is_fnresult_type(ty: Any) -> bool:
	if isinstance(ty, str):
		return "FnResult" in ty
	if isinstance(ty, tuple):
		if len(ty) == 2:
			return True
		if ty and ty[0] == "FnResult":
			return True
	return False


__all__ = ["SimpleTypeEnv", "InferredTypeEnv", "build_type_env_from_ssa"]
