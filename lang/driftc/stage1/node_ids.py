# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-27
"""
NodeId assignment for HIR nodes.

This pass assigns stable, per-function NodeIds so typed side tables can key
off HIR nodes without relying on Python object identity.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Callable, Iterable

from lang.driftc.stage1 import hir_nodes as H

_HIR_MODULES = {H.__name__, "lang.driftc.stage1.closures"}


def default_should_descend(obj: object) -> bool:
	"""Default `should_descend` predicate for `iter_hir_walk`.

	Returns True for any HIR node (`H.HNode`) and any dataclass declared
	in a known HIR module. Other consumers (e.g. the type checker) can
	pass a custom predicate that adds extra rules — most commonly,
	skipping `HLambda` so a parent function's walker does not descend
	into nested closures.
	"""
	if isinstance(obj, H.HNode):
		return True
	if is_dataclass(obj) and obj.__class__.__module__ in _HIR_MODULES:
		return True
	return False


# Backwards-compatibility alias for the internal name used before the
# helper was generalized. New code should use `default_should_descend`.
_should_descend = default_should_descend


def iter_hir_walk(
	root: object,
	*,
	should_descend: Callable[[object], bool] = default_should_descend,
) -> Iterable[object]:
	"""Iteratively walk an HIR tree, yielding each object in the same order
	the original recursive `walk` / `walk_value` pair would have visited it.

	Preserves:
	- pre-order discipline (parent before children)
	- declaration order across dataclass fields
	- the `id(obj)` dedup discipline of the original walker (only candidate
	  nodes are deduped; transient list/tuple/dict containers are not)

	The `should_descend` parameter lets callers customize which subtrees the
	walker descends into. Default is `default_should_descend` (any HIR node
	or HIR-module dataclass). The type checker passes a variant that also
	skips `H.HLambda` so call collectors do not cross closure boundaries.

	Exists because deeply nested HIR (e.g. ~200+ levels of nested if-stmts)
	overflowed Python's recursion limit in three sequential walker pairs in
	this file; see work/robustness/robustness-matrix.md row #2. Originally
	a stage1-private helper named `_iter_hir_walk`; promoted to public
	`iter_hir_walk` and parameterized so the four other local copies (in
	`driftc.py` and `type_checker.py`) can call it instead of duplicating
	the iterative pattern. See matrix row #15.
	"""
	seen: set[int] = set()
	stack: list[object] = [root]
	while stack:
		obj = stack.pop()
		if obj is None:
			continue
		# Flatten list/tuple/dict containers in place — same as the original
		# `walk_value` did via mutual recursion, but iteratively. No seen
		# tracking on transient containers, matching the original behavior.
		if isinstance(obj, (list, tuple)):
			for item in reversed(obj):
				stack.append(item)
			continue
		if isinstance(obj, dict):
			for key in sorted(obj.keys(), key=repr, reverse=True):
				stack.append(obj[key])
			continue
		oid = id(obj)
		if oid in seen:
			continue
		seen.add(oid)
		yield obj
		if not should_descend(obj):
			continue
		# Push children in reverse so LIFO pop yields them in declaration order.
		if is_dataclass(obj):
			for f in reversed(fields(obj)):
				stack.append(getattr(obj, f.name))
		else:
			for val in reversed(list(vars(obj).values())):
				stack.append(val)


# Backwards-compatibility alias for in-module callers and any
# external consumers that imported the original name.
_iter_hir_walk = iter_hir_walk


def assign_node_ids(root: H.HNode, *, start: int = 1) -> int:
	"""
	Assign NodeIds to all HIR nodes reachable from `root`.

	Returns the next available NodeId after traversal.
	"""
	next_id = start
	for obj in _iter_hir_walk(root):
		if isinstance(obj, H.HNode):
			if is_dataclass(obj) and getattr(obj, "__dataclass_params__", None) and obj.__dataclass_params__.frozen:
				object.__setattr__(obj, "node_id", next_id)
			else:
				obj.node_id = next_id
			next_id += 1
	return next_id


def assign_callsite_ids(root: H.HNode, *, start: int = 0) -> int:
	"""
	Assign CallSiteIds to all call nodes reachable from `root`.

	Returns the next available CallSiteId after traversal.
	"""
	next_id = start
	for obj in _iter_hir_walk(root):
		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			if is_dataclass(obj) and getattr(obj, "__dataclass_params__", None) and obj.__dataclass_params__.frozen:
				object.__setattr__(obj, "callsite_id", next_id)
			else:
				obj.callsite_id = next_id
			next_id += 1
	return next_id


def validate_callsite_ids(root: H.HNode) -> None:
	"""
	Validate CallSiteIds for all call nodes reachable from `root`.
	"""
	ids: list[int] = []
	for obj in _iter_hir_walk(root):
		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			callsite_id = getattr(obj, "callsite_id", None)
			if callsite_id is None:
				raise AssertionError("missing callsite_id on call node")
			ids.append(int(callsite_id))
	if not ids:
		return
	uniq = set(ids)
	if len(uniq) != len(ids):
		raise AssertionError("duplicate callsite_id values")
	lo, hi = min(uniq), max(uniq)
	if hi - lo + 1 != len(uniq):
		raise AssertionError("callsite_id range is not dense")


__all__ = ["assign_node_ids", "assign_callsite_ids", "validate_callsite_ids"]
