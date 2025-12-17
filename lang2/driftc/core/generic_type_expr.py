# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Generic-aware type expression shapes used by the compiler core.

Why this exists
---------------
The parser produces `parser.ast.TypeExpr` nodes which are convenient for source
representation, but those nodes cannot represent *generic type parameters*
(`T`, `Item`, etc.) in a way that the core type system can later instantiate.

For MVP generics (nominal type generics only), we need a tiny, self-contained
shape that:
- can reference a type parameter by index (`T` -> param 0),
- can represent named type constructors with arguments (`Array<T>`, `&mut T`),
- does not depend on parser AST types (layering).

This module provides `GenericTypeExpr` which is stored in schemas (e.g. variant
definitions) and later "evaluated" into concrete `TypeId`s when a generic type
is instantiated with concrete type arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class GenericTypeExpr:
	"""
	A generic-aware type expression tree.

	Invariants:
	- If `param_index` is not None, this node represents a type parameter and
	  `name`/`args` are ignored.
	- Otherwise this node represents a named type with optional type arguments.

	Examples:
	- `T`                      -> GenericTypeExpr(param_index=0)
	- `Int`                    -> GenericTypeExpr(name="Int")
	- `Optional<T>`            -> GenericTypeExpr(name="Optional", args=[param(0)])
	- `&mut Array<String>`     -> GenericTypeExpr(name="&mut", args=[named("Array",[named("String")])])
	"""

	name: str = ""
	args: List["GenericTypeExpr"] = field(default_factory=list)
	param_index: Optional[int] = None

	@staticmethod
	def param(idx: int) -> "GenericTypeExpr":
		"""Construct a type parameter reference node."""
		return GenericTypeExpr(param_index=int(idx))

	@staticmethod
	def named(name: str, args: List["GenericTypeExpr"] | None = None) -> "GenericTypeExpr":
		"""Construct a named type node (possibly with type arguments)."""
		return GenericTypeExpr(name=str(name), args=list(args or []), param_index=None)


__all__ = ["GenericTypeExpr"]

