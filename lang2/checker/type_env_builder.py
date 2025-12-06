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

from lang2.checker import CheckedProgram, FnSignature
from lang2.checker.type_env_impl import CheckerTypeEnv
from lang2.stage4 import SsaFunc
from lang2.types_core import TypeTable, TypeId


def build_minimal_checker_type_env(
	checked: CheckedProgram,
	ssa_funcs: Mapping[str, SsaFunc],
	signatures: Mapping[str, FnSignature],
	table: TypeTable | None = None,
) -> CheckerTypeEnv | None:
	"""
	Assign the function's declared return TypeId to returned SSA values.

	This only handles can-throw functions (FnResult returns) and only tags
	terminator return values. It uses the supplied TypeTable (or a fresh one
	if none is provided).
	"""
	table = table or checked.type_table or TypeTable()
	value_types: Dict[tuple[str, str], TypeId] = {}

	for fn_name, fn_info in checked.fn_infos.items():
		if not fn_info.declared_can_throw:
			continue
		sig = signatures.get(fn_name)
		if sig is None or sig.return_type_id is None:
			continue
		ssa = ssa_funcs.get(fn_name)
		if ssa is None:
			continue
		for block in ssa.func.blocks.values():
			term = block.terminator
			val = getattr(term, "value", None)
			if val is None:
				continue
			value_types[(fn_name, val)] = sig.return_type_id

	if not value_types:
		return None

	return CheckerTypeEnv(table, value_types)


__all__ = ["build_minimal_checker_type_env"]
