#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Borrow-checker scaffolding: place representation and basic lvalue detection.

This models the "where" of values (locals + projections) so later passes can
track moves/borrows per place. It intentionally avoids policy (no diagnostics
or CFG yet) and just answers:
  * Is this HIR expression an lvalue (borrowable/moveable place)?
  * If so, what place does it identify?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Tuple, Optional

from lang2 import stage1 as H


class IndexKind(Enum):
	"""Coarse-grained index classification to keep Place hashable."""

	ANY = auto()       # Unknown / non-constant index; conservatively overlaps.
	CONST = auto()     # Known constant index.


@dataclass(frozen=True)
class FieldProj:
	"""Field access projection (e.g., `.name`)."""

	name: str


@dataclass(frozen=True)
class IndexProj:
	"""
	Index projection (e.g., `[i]`).

	Hashability: we classify indices instead of storing raw HIR to keep Place
	hashable. `value` is only set for CONST indices.
	"""

	kind: IndexKind
	value: Optional[int] = None


Projection = FieldProj | IndexProj


class PlaceState(Enum):
	"""Validity state for a place: uninitialized, valid, or moved."""

	UNINIT = auto()
	VALID = auto()
	MOVED = auto()


def merge_place_state(a: PlaceState, b: PlaceState) -> PlaceState:
	"""
	Meet operation for place states used in dataflow joins.

	Heuristic: MOVED dominates, then VALID, then UNINIT. Extend when finer
	granularity (TOP/⊥) is introduced.
	"""
	if a is b:
		return a
	if PlaceState.MOVED in (a, b):
		return PlaceState.MOVED
	if PlaceState.VALID in (a, b):
		return PlaceState.VALID
	return PlaceState.UNINIT


class PlaceKind(Enum):
	LOCAL = auto()
	PARAM = auto()
	GLOBAL = auto()
	CAPTURE = auto()
	TEMP = auto()


@dataclass(frozen=True)
class PlaceBase:
	"""Identity for the root of a Place (locals, params, globals)."""

	kind: PlaceKind
	local_id: int
	name: str


@dataclass(frozen=True)
class Place:
	"""
	A borrowable/moveable storage location.

	`base` carries identity (local/param/etc). `projections` capture field/index
	accesses, so `foo.bar[0]` becomes base `foo` with projections `.bar`, `[0]`.
	"""

	base: PlaceBase
	projections: Tuple[Projection, ...] = field(default_factory=tuple)

	def with_projection(self, proj: Projection) -> "Place":
		"""Return a new Place with an additional projection appended."""
		return Place(self.base, self.projections + (proj,))


def is_lvalue(expr: H.HExpr, *, base_lookup: Callable[[object], Optional[PlaceBase]]) -> bool:
	"""
	Decide if a HIR expression denotes a storage location (`Place`) per spec:
	- Locals/params (`HVar`) are lvalues.
	- Field/index access of an lvalue is an lvalue.
	- Everything else is an rvalue (temporaries, literals, calls, binops, etc.).
	"""
	return place_from_expr(expr, base_lookup=base_lookup) is not None


def place_from_expr(expr: H.HExpr, *, base_lookup: Callable[[object], Optional[PlaceBase]]) -> Optional[Place]:
	"""
	Construct a `Place` from a HIR expression when the expression is an lvalue.

	Returns None for rvalues.
	"""
	if isinstance(expr, H.HVar):
		base = base_lookup(expr)
		if base is None:
			return None
		return Place(base)
	if isinstance(expr, H.HField):
		base_place = place_from_expr(expr.subject, base_lookup=base_lookup)
		if base_place is None:
			return None
		return base_place.with_projection(FieldProj(expr.name))
	if isinstance(expr, H.HIndex):
		base_place = place_from_expr(expr.subject, base_lookup=base_lookup)
		if base_place is None:
			return None
		const_val: Optional[int] = None
		if isinstance(expr.index, H.HLiteralInt):
			try:
				const_val = int(expr.index.value)
			except Exception:
				const_val = None
		kind = IndexKind.CONST if const_val is not None else IndexKind.ANY
		return base_place.with_projection(IndexProj(kind=kind, value=const_val))
	# Other expressions are rvalues: calls, literals, binops, method calls, etc.
	return None


def place_from_expr_default(expr: H.HExpr) -> Optional[Place]:
	"""
	Backward-compatible helper using base names as identity.

	Prefer passing an explicit base_lookup in new code.
	"""
	return place_from_expr(
		expr,
		base_lookup=lambda hv: PlaceBase(
			PlaceKind.LOCAL,
			getattr(hv, "binding_id", 0) if getattr(hv, "binding_id", None) is not None else 0,
			hv.name if hasattr(hv, "name") else str(hv),
		),
	)
