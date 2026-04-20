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
from enum import Enum, IntEnum, auto
from typing import Callable, Tuple, Optional

from lang.driftc import stage1 as H


class EscapeLevel(IntEnum):
	"""How far a closure/loan is allowed to escape its origin scope."""

	IMMEDIATE = 0  # Never outlives the call — used only for immediately-invoked lambdas.
	LOCAL = 1      # Stays within the enclosing function scope.
	SCOPED = 2     # May be handed to a structured-concurrency scope (e.g., std.concurrent.scope).
	THREAD = 3     # May be sent to a detached virtual thread.
	STATIC = 4     # May be registered globally / used as a 'static callback.


class IndexKind(Enum):
	"""Coarse-grained index classification to keep Place hashable."""

	ANY = auto()       # Unknown / non-constant index; conservatively overlaps.
	CONST = auto()     # Known constant index.
	VAR = auto()       # Variable index (binding id); overlaps unless facts prove disjoint.


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


@dataclass(frozen=True)
class DerefProj:
	"""
	Dereference projection (`*p`).

	We model deref as a projection so the place `*p` is represented as:
	  Place(base=p, projections=[DerefProj()])

	This keeps the place model composable for future extensions like `(*p).field`
	and `p[i]` on borrowed aggregates.
	"""
	pass

Projection = FieldProj | IndexProj | DerefProj


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


def _try_eval_const_int(expr: H.HExpr) -> Optional[int]:
	"""Best-effort constant folding for integer index expressions."""
	if isinstance(expr, H.HLiteralInt):
		try:
			return int(expr.value)
		except Exception:
			return None
	if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.NEG:
		inner = _try_eval_const_int(expr.expr)
		if inner is None:
			return None
		return -inner
	if isinstance(expr, H.HBinary):
		left = _try_eval_const_int(expr.left)
		right = _try_eval_const_int(expr.right)
		if left is None or right is None:
			return None
		if expr.op is H.BinaryOp.ADD:
			return left + right
		if expr.op is H.BinaryOp.SUB:
			return left - right
		if expr.op is H.BinaryOp.MUL:
			return left * right
	return None


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


def places_overlap(a: Place, b: Place) -> bool:
	"""
	Return True when two places may refer to overlapping storage.

	This function is the single source of truth for "place overlap" used by
	borrow checking and write-frozen rules.

	MVP rules:
	- Different bases never overlap.
	- Prefix overlap counts: `x` overlaps `x.field` and `x[0]`.
	- Field projections are disjoint when the field names differ.
	- Index projections:
	  - CONST vs CONST are disjoint when indices differ (`arr[0]` vs `arr[1]`).
	  - ANY overlaps everything (`arr[i]` overlaps `arr[0]` and `arr[j]`).
	  - VAR overlaps everything unless a fact system proves disjointness.
	- Any projection-kind mismatch at the same depth is treated as overlapping
	  (conservative until we have more precise type/layout information).
	"""
	if a.base != b.base:
		return False

	ap = a.projections
	bp = b.projections
	n = min(len(ap), len(bp))
	for idx in range(n):
		pa = ap[idx]
		pb = bp[idx]
		if pa == pb:
			continue

		# Field-vs-field: disjoint when names differ.
		if isinstance(pa, FieldProj) and isinstance(pb, FieldProj):
			return False

		# Index-vs-index: disjoint only when both are CONST and values differ.
		if isinstance(pa, IndexProj) and isinstance(pb, IndexProj):
			if pa.kind is IndexKind.CONST and pb.kind is IndexKind.CONST:
				if pa.value is not None and pb.value is not None and pa.value != pb.value:
					return False
			# Otherwise: ANY overlaps, or equal const handled by pa==pb above.
			return True

		# Deref-vs-deref: pa==pb above; any other deref mismatch is conservative overlap.
		return True

	# One place is a prefix of the other (or identical): overlaps by definition.
	return True


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
	# Canonical place expression (stage1→stage2 boundary).
	if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
		base = base_lookup(expr.base)
		if base is None:
			return None
		place = Place(base)
		for proj in expr.projections:
			if isinstance(proj, H.HPlaceField):
				place = place.with_projection(FieldProj(proj.name))
				continue
			if isinstance(proj, H.HPlaceDeref):
				place = place.with_projection(DerefProj())
				continue
			if isinstance(proj, H.HPlaceIndex):
				const_val = _try_eval_const_int(proj.index)
				if const_val is not None:
					place = place.with_projection(IndexProj(kind=IndexKind.CONST, value=const_val))
					continue
				if isinstance(proj.index, H.HVar):
					bid = getattr(proj.index, "binding_id", None)
					if bid is not None:
						place = place.with_projection(IndexProj(kind=IndexKind.VAR, value=int(bid)))
						continue
				place = place.with_projection(IndexProj(kind=IndexKind.ANY))
				continue
			# Unknown projections conservatively make this non-addressable until
			# the place model is extended.
			return None
		return place

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
		const_val = _try_eval_const_int(expr.index)
		if const_val is not None:
			return base_place.with_projection(IndexProj(kind=IndexKind.CONST, value=const_val))
		if isinstance(expr.index, H.HVar):
			bid = getattr(expr.index, "binding_id", None)
			if bid is not None:
				return base_place.with_projection(IndexProj(kind=IndexKind.VAR, value=int(bid)))
		return base_place.with_projection(IndexProj(kind=IndexKind.ANY))
	if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.DEREF:
		base_place = place_from_expr(expr.expr, base_lookup=base_lookup)
		if base_place is None:
			return None
		return base_place.with_projection(DerefProj())
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
