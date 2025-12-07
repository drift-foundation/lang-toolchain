# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Minimal type-environment protocol for future type-aware checks.

Stage4 throw checks will eventually use this to assert that return values in
can-throw functions are real `FnResult<_, Error>` values. For now it is a stub;
the real checker/type system will provide a concrete implementation. The API is
type-centric (not SSA-string-centric) to avoid repainting call sites later.
"""

from __future__ import annotations

from typing import Protocol, Tuple, Any


class TypeEnv(Protocol):
	"""
	Protocol for querying SSA value types.

	TypeEnv deliberately avoids committing to a concrete type representation:
	callers should treat the returned objects as opaque `TypeId`-like handles and
	use the provided helpers to reason about FnResult.
	"""

	def type_of_ssa_value(self, func_name: str, value_id: str) -> Any:
		"""Return the type of the given SSA value in the named function."""
		...

	def is_fnresult(self, ty: Any) -> bool:
		"""Return True if the type represents `FnResult<_, Error>` (or equivalent)."""
		...

	def fnresult_parts(self, ty: Any) -> Tuple[Any, Any]:
		"""
		If `ty` is a FnResult, return `(ok_type, error_type)`.

		Implementations may raise if called on a non-FnResult type; stage4 should
		only call this after `is_fnresult` succeeds.
		"""
		...
