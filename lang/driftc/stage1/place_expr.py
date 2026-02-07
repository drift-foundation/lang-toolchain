# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-15
"""
HIR place-expression helpers.

Why this exists
---------------
The compiler needs a *canonical* representation for addressable locations
(lvalues), so later phases (borrow checking, MIR lowering, SSA) do not have to
re-derive "is this a place?" from arbitrary HIR expression trees.

`HPlaceExpr` is that canonical shape:
  - a base binding (`HVar`), plus
  - a list of projections (`.field`, `[index]`, `*deref`).

This module provides:
  - `place_expr_from_lvalue_expr`: best-effort conversion from legacy place-like
    HIR expression shapes into an `HPlaceExpr`.

Normalization is responsible for using this conversion in place contexts:
  - `HBorrow.subject`
  - `HAssign.target`
  - `HMove.subject`
"""

from __future__ import annotations

from typing import Optional

from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.core.span import Span


def place_expr_from_lvalue_expr(expr: H.HExpr) -> Optional[H.HPlaceExpr]:
	"""
	Convert a legacy place-like HIR expression into a canonical `HPlaceExpr`.

	Returns:
	  - `HPlaceExpr` when the input is syntactically addressable, or
	  - `None` for rvalues (calls, literals, binops, etc.).

	Supported legacy shapes:
	  - `HVar`
	  - `HField(place, name)`
	  - `HIndex(place, idx)`
	  - `HUnary(DEREF, place)` (deref-place)
	  - already-canonical `HPlaceExpr`

	Important invariant:
	  `HPlaceExpr.base` is always an `HVar`. Borrowing of rvalues is handled by
	  stage1 materialization (introducing a temp binding), so we do not support
	  bases like `(*call()).field`.
	"""
	if isinstance(expr, H.HPlaceExpr):
		return expr
	if isinstance(expr, H.HVar):
		return H.HPlaceExpr(base=expr, projections=[], loc=getattr(expr, "loc", Span()))
	if isinstance(expr, H.HField):
		base = place_expr_from_lvalue_expr(expr.subject)
		if base is None:
			return None
		return H.HPlaceExpr(
			base=base.base,
			projections=[*base.projections, H.HPlaceField(name=expr.name)],
			loc=getattr(expr, "loc", base.loc),
		)
	if isinstance(expr, H.HIndex):
		base = place_expr_from_lvalue_expr(expr.subject)
		if base is None:
			return None
		return H.HPlaceExpr(
			base=base.base,
			projections=[*base.projections, H.HPlaceIndex(index=expr.index)],
			loc=getattr(expr, "loc", base.loc),
		)
	if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.DEREF:
		base = place_expr_from_lvalue_expr(expr.expr)
		if base is None:
			return None
		return H.HPlaceExpr(
			base=base.base,
			projections=[*base.projections, H.HPlaceDeref()],
			loc=getattr(expr, "loc", base.loc),
		)
	return None


__all__ = ["place_expr_from_lvalue_expr"]
