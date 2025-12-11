# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Bridge helpers to wrap InferredTypeEnv into a checker-owned TypeEnv.

This is a transitional path: it lets the driver/tests supply a TypeEnv with
real TypeIds (TypeTable) using the existing SSA-based inference until the
checker owns type assignment end-to-end.
"""

from __future__ import annotations

from typing import Dict, Mapping

from lang2.driftc.checker import FnSignature
from lang2.driftc.checker.type_env_impl import CheckerTypeEnv
from lang2.driftc.stage4 import SsaFunc
from lang2.driftc.core.types_core import TypeId, TypeTable
from lang2.driftc.core.types_env_impl import InferredTypeEnv


def build_checker_type_env_from_inferred(
	inferred: InferredTypeEnv,
	ssa_funcs: Mapping[str, SsaFunc],
	signatures: Mapping[str, FnSignature] | None = None,
) -> CheckerTypeEnv:
	"""
	Wrap an InferredTypeEnv (SSA + signature-based) into a CheckerTypeEnv.

	This interns the opaque types used by InferredTypeEnv (strings/tuples) into
	real TypeIds via a TypeTable so stage4 can consume the standard TypeEnv API.
	"""
	table = TypeTable()
	cache: Dict[object, TypeId] = {}

	def intern(opaque: object) -> TypeId:
		"""Map an opaque inferred type into a stable TypeId."""
		if opaque in cache:
			return cache[opaque]
		ty_id: TypeId
		if isinstance(opaque, str):
			if "Int" in opaque:
				ty_id = table.new_scalar("Int")
			elif "Bool" in opaque:
				ty_id = table.new_scalar("Bool")
			elif "Error" in opaque:
				ty_id = table.new_error("Error")
			elif "FnResult" in opaque:
				# Unknown ok/err parts; keep Unknown placeholders.
				unknown_ok = table.new_unknown("UnknownOk")
				unknown_err = table.new_unknown("UnknownErr")
				ty_id = table.new_fnresult(unknown_ok, unknown_err)
			else:
				ty_id = table.new_scalar(opaque)
		elif isinstance(opaque, tuple):
			# Tuple convention from InferredTypeEnv:
			# (ok, err) -> FnResult, ("FnResult", ok, err) -> FnResult
			if len(opaque) == 2:
				ok = intern(opaque[0])
				err = intern(opaque[1])
				ty_id = table.new_fnresult(ok, err)
			elif len(opaque) >= 3 and opaque[0] == "FnResult":
				ok = intern(opaque[1])
				err = intern(opaque[2])
				ty_id = table.new_fnresult(ok, err)
			else:
				ty_id = table.new_scalar(str(opaque))
		else:
			ty_id = table.new_unknown(str(opaque))
		cache[opaque] = ty_id
		return ty_id

	value_types: Dict[tuple[str, str], TypeId] = {}
	for fn_name, ssa in ssa_funcs.items():
		for block in ssa.func.blocks.values():
			for idx, instr in enumerate(block.instructions):
				key = (fn_name, getattr(instr, "dest", None))
				if key[1] is None:
					continue
				try:
					opaque = inferred.type_of_ssa_value(fn_name, key[1])
				except KeyError:
					continue
				value_types[key] = intern(opaque)
			term = block.terminator
			if hasattr(term, "value") and getattr(term, "value") is not None:
				val = term.value
				try:
					opaque = inferred.type_of_ssa_value(fn_name, val)
				except KeyError:
					continue
				value_types[(fn_name, val)] = intern(opaque)

	return CheckerTypeEnv(table, value_types)


__all__ = ["build_checker_type_env_from_inferred"]
