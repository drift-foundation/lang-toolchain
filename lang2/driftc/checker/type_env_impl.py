# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-08
"""
Checker-owned TypeEnv backed by the TypeTable.

This is the "real" TypeEnv front-end code should provide to stage4. It wraps
TypeIds from `lang2/driftc/core/types_core.py` and an SSA value -> TypeId map.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from lang2.driftc.core.types_protocol import TypeEnv
from lang2.driftc.core.types_core import TypeId, TypeKind, TypeTable


class CheckerTypeEnv(TypeEnv):
	"""
	TypeEnv implementation backed by the checker's TypeTable.

	`value_types` maps (function name, SSA value id) -> TypeId. Stage4 treats
	TypeIds as opaque; `is_fnresult`/`fnresult_parts` consult the TypeTable.
	"""

	def __init__(self, table: TypeTable, value_types: Mapping[tuple[str, str], TypeId]) -> None:
		self._table = table
		self._value_types: Dict[tuple[str, str], TypeId] = dict(value_types)

	def type_of_ssa_value(self, func_name: str, value_id: str) -> TypeId:
		"""Return the TypeId for a given SSA value."""
		return self._value_types[(func_name, value_id)]

	def is_fnresult(self, ty: Any) -> bool:
		"""
		Return True if the TypeId refers to a FnResult type.

		TypeEnv protocol uses Any, but callers should treat this as TypeId.
		"""
		if not isinstance(ty, int):
			return False
		return self._table.get(ty).kind is TypeKind.FNRESULT

	def fnresult_parts(self, ty: Any) -> Tuple[Any, Any]:
		"""
		Return (ok_type, err_type) TypeIds for a FnResult TypeId.

		Raises TypeError if called on non-FnResult types.
		"""
		if not isinstance(ty, int):
			raise TypeError(f"fnresult_parts called on non-TypeId {ty!r}")
		td = self._table.get(ty)
		if td.kind is not TypeKind.FNRESULT or len(td.param_types) != 2:
			raise TypeError(f"fnresult_parts called on non-FnResult type {td}")
		return td.param_types[0], td.param_types[1]


__all__ = ["CheckerTypeEnv"]
