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

For now we only thread a boolean throw intent per function through to the driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, FrozenSet


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
	return_type: Optional[Any] = None
	error_type: Optional[Any] = None


@dataclass
class CheckedProgram:
	"""
	Container returned by the checker.

	In the stub this only carries fn_infos; real implementations will also
	provide a concrete TypeEnv, diagnostics, and the exception catalog for
	catch/throws validation.
	"""

	fn_infos: Dict[str, FnInfo]
	type_env: Optional[Any] = None
	exception_catalog: Optional[Dict[str, int]] = None
	diagnostics: List[Any] = None  # placeholder until Diagnostic is defined


class Checker:
	"""
	Placeholder checker.

	Accepts a sequence of function declarations and an optional declared_can_throw
	map (defaults to False for all). This input is strictly a testing shim; a real
	checker will compute declared_can_throw from signatures (FnResult/throws) and
	the type system.
	"""

	def __init__(self, declared_can_throw: Dict[str, bool] | None = None) -> None:
		self._declared = declared_can_throw or {}

	def check(self, fn_decls: Iterable[str]) -> CheckedProgram:
		"""
		Produce a CheckedProgram with FnInfo for each fn name in `fn_decls`.
		"""
		fn_infos: Dict[str, FnInfo] = {}
		for name in fn_decls:
			fn_infos[name] = FnInfo(name=name, declared_can_throw=self._declared.get(name, False))

		# The real checker will attach type_env, diagnostics, and exception_catalog.
		return CheckedProgram(
			fn_infos=fn_infos,
			type_env=None,
			exception_catalog=None,
			diagnostics=[],
		)
