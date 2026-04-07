# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Robustness regression: TypeKey hash/eq must not RecursionError on deeply
nested type keys.

Surfaced by the row #11 cleanup pass on the robustness matrix
(`work/robustness/robustness-matrix.md`): re-probing nested generic types
in fn-parameter position revealed three sequential recursion sites in the
type-key handling pipeline. After fixing `_type_expr_key`
(`lang/driftc/parser/__init__.py`) and `type_key_from_typeid`
(`lang/driftc/traits/world.py`), the recursion shifted into
`TypeKey.__hash__` (auto-generated frozen-dataclass hash recurses through
the `args` tuple) and then into `TypeKey.__eq__` (auto-generated equality
recurses similarly).

The fix is two-part on `TypeKey`:
- `__post_init__` precomputes a cached hash from already-cached child
  hashes, so `__hash__` is O(1) and never recurses
- `__eq__` is overridden to walk both trees in lockstep with an explicit
  pair stack, short-circuiting on hash inequality

This file pins both new methods in isolation by building two synthetic
deeply-nested `TypeKey` trees and exercising hash/eq under
`sys.setrecursionlimit(1000)`.
"""
from __future__ import annotations

import sys

from lang.driftc.core.types_core import TypeTable
from lang.driftc.traits.world import TypeKey, type_key_from_typeid


def _build_deep_typekey(n: int, *, leaf_name: str = "Int", chain_name: str = "Array") -> TypeKey:
	"""Build `Array<Array<...<Int>>>` with `n` chain levels.

	Bottom-up construction so each `TypeKey.__post_init__` can read the
	cached hash from its already-built child.
	"""
	tk: TypeKey = TypeKey(package_id=None, module=None, name=leaf_name, args=())
	for _ in range(n):
		tk = TypeKey(package_id=None, module=None, name=chain_name, args=(tk,))
	return tk


def test_typekey_hash_deep_nesting_no_recursion_error() -> None:
	"""5000 levels of nested TypeKey must hash without crashing under
	default recursion limit.

	Pre-fix shape: `tuple.__hash__` recursing through `TypeKey.__hash__`
	on the `args` tuple at each level overflows Python's recursion stack
	at ~250 levels. The cached-hash post_init pre-computes everything
	bottom-up so `hash(deep_tk)` becomes O(1).
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		tk = _build_deep_typekey(5000)
		# `hash(tk)` must not crash and must return an int.
		h = hash(tk)
		assert isinstance(h, int)
	finally:
		sys.setrecursionlimit(prev)


def test_typekey_eq_deep_nesting_no_recursion_error() -> None:
	"""Two structurally-identical 5000-deep TypeKeys must compare equal
	without crashing under default recursion limit.

	Pre-fix shape: `tuple.__eq__` recursing through `TypeKey.__eq__` on
	the `args` tuple at each level overflows Python's recursion stack at
	~250 levels. The iterative `__eq__` walks both trees in lockstep
	with a pair stack and never recurses.
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		a = _build_deep_typekey(5000)
		b = _build_deep_typekey(5000)
		# Identical construction → must compare equal.
		assert a == b
		# Same hash too (the cached value is deterministic).
		assert hash(a) == hash(b)
	finally:
		sys.setrecursionlimit(prev)


def test_typekey_eq_distinguishes_inequality_at_depth() -> None:
	"""Two TypeKeys that differ only at the innermost leaf must compare
	not-equal without crashing.

	Pre-fix shape: as above, the recursive eq blows the stack at d≥250.
	Post-fix: the iterative eq walks down to the leaf, finds the
	mismatch, returns False.
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		a = _build_deep_typekey(5000, leaf_name="Int")
		b = _build_deep_typekey(5000, leaf_name="Bool")
		assert a != b
		# Hashes are very likely different too (cached hash includes the
		# leaf name through the recursive arg-hash chain), but the
		# inequality must be returned even if hashes happened to collide.
	finally:
		sys.setrecursionlimit(prev)


def test_typekey_eq_short_circuits_on_hash_mismatch() -> None:
	"""When two TypeKeys have different cached hashes, eq must return
	False immediately without walking the trees.

	Sanity that the hash short-circuit works.
	"""
	a = _build_deep_typekey(10, leaf_name="Int")
	b = _build_deep_typekey(10, leaf_name="Bool")
	# Different leaf names propagate through to different cached hashes.
	assert hash(a) != hash(b)
	assert a != b


def test_typekey_eq_identity_short_circuits() -> None:
	"""`tk == tk` must return True without walking the tree."""
	tk = _build_deep_typekey(10)
	assert tk == tk


def test_typekey_eq_returns_notimplemented_for_non_typekey() -> None:
	"""Comparing a TypeKey to an unrelated object must return NotImplemented
	(or False after Python's fallback), not crash."""
	tk = _build_deep_typekey(3)
	# Direct __eq__ call returns NotImplemented for non-TypeKey.
	assert tk.__eq__("not a typekey") is NotImplemented
	# Public `==` falls back to identity comparison and returns False.
	assert (tk == "not a typekey") is False


def _build_deep_array_typeid(table: TypeTable, n: int) -> int:
	"""Build `Array<Array<...<Int>>>` with n levels of `Array<>` nesting in
	a real `TypeTable` and return the outermost tid.

	Bottom-up construction so each `new_array` call interns its child first.
	This exercises the same code path the production parser uses to build
	nested array types from `var x: Array<Array<Int>> = ...;` source.
	"""
	tid = table.ensure_int()
	for _ in range(n):
		tid = table.new_array(tid)
	return tid


def test_type_key_from_typeid_deep_nested_no_recursion_error() -> None:
	"""5000 levels of nested `Array<...>` in a real TypeTable must produce
	a TypeKey via `type_key_from_typeid` without crashing under default
	recursion limit.

	Pre-fix shape: `type_key_from_typeid` recursed once per type-nesting
	level via the `tuple(type_key_from_typeid(...) for ...)` generator,
	overflowing Python's recursion stack at ~250 levels. The iterative
	post-order rewrite caches each tid → TypeKey result and walks the
	type DAG without stack growth.

	This is the missing direct regression for walker site #2 of the
	row #11 fix; the parser-side `_type_expr_key` and the synthetic
	`TypeKey` hash/eq tests cover sites #1 and #3, but only this test
	exercises the production `traits/world.py::type_key_from_typeid`
	path against a real `TypeTable`.
	"""
	prev = sys.getrecursionlimit()
	sys.setrecursionlimit(1000)
	try:
		table = TypeTable()
		deep_tid = _build_deep_array_typeid(table, 5000)
		key = type_key_from_typeid(table, deep_tid)
		# Sanity: the result is a TypeKey and the chain depth is preserved.
		assert isinstance(key, TypeKey)
		# Walk down the args spine iteratively (so the test itself does
		# not recurse) and count Array<…> levels.
		depth = 0
		node: TypeKey = key
		while node.name == "Array" and node.args:
			depth += 1
			node = node.args[0]
		assert depth == 5000, f"expected 5000 nested Array<>, got {depth}"
		assert node.name == "Int"
	finally:
		sys.setrecursionlimit(prev)


def test_typekey_hashable_in_set() -> None:
	"""Sanity: TypeKey instances are usable as dict/set keys after the
	hash refactor. The cached-hash discipline must not break dict/set
	behavior."""
	a = _build_deep_typekey(20, leaf_name="Int")
	b = _build_deep_typekey(20, leaf_name="Int")
	c = _build_deep_typekey(20, leaf_name="Bool")
	s = {a, b, c}
	# `a` and `b` are structurally equal → set should dedup them.
	assert len(s) == 2
	d = {a: 1, b: 2}  # b overwrites a's slot since hash/eq match
	assert d[a] == 2
