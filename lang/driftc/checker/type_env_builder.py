# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Minimal checker-owned TypeEnv builder (signature-driven).

This is a lightweight step toward the real checker TypeEnv: it assigns the
function's declared return TypeId to returned SSA values for can-throw
functions. More complete type assignment will replace this over time.
"""

from __future__ import annotations

from typing import Dict, Mapping

from lang.driftc.checker import CheckedProgramById, FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.checker.type_env_impl import CheckerTypeEnv
from lang.driftc.stage4 import SsaFunc
from lang.driftc.core.types_core import TypeTable, TypeId


def build_minimal_checker_type_env(
	checked: CheckedProgramById,
	ssa_funcs: Mapping[FunctionId, SsaFunc],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	table: TypeTable | None = None,
) -> CheckerTypeEnv | None:
	"""
	Assign the function's declared return TypeId to returned SSA values.

	This only handles can-throw functions and only tags
	terminator return values. It uses the supplied TypeTable (or a fresh one
	if none is provided).
	"""
	table = table or checked.type_table or TypeTable()
	value_types: Dict[tuple[FunctionId, str], TypeId] = {}

	for fn_id, fn_info in checked.fn_infos_by_id.items():
		if not fn_info.declared_can_throw:
			continue
		sig = signatures_by_id.get(fn_id)
		if sig is None or sig.return_type_id is None:
			continue
		# Surface return types are `T`; can-throw ABI returns `FnResult<T, Error>`.
		err_tid = sig.error_type_id or table.ensure_error()
		fnres_tid = table.ensure_fnresult(sig.return_type_id, err_tid)
		ssa = ssa_funcs.get(fn_id)
		if ssa is None:
			continue
		for block in ssa.func.blocks.values():
			term = block.terminator
			val = getattr(term, "value", None)
			if val is None:
				continue
			value_types[(fn_id, val)] = fnres_tid

	if not value_types:
		return None

	return CheckerTypeEnv(table, value_types)


__all__ = ["build_minimal_checker_type_env"]
