# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-27
"""
NodeId assignment for HIR nodes.

This pass assigns stable, per-function NodeIds so typed side tables can key
off HIR nodes without relying on Python object identity.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Iterable

from lang.driftc.stage1 import hir_nodes as H

_HIR_MODULES = {H.__name__, "lang.driftc.stage1.closures"}


def assign_node_ids(root: H.HNode, *, start: int = 1) -> int:
	"""
	Assign NodeIds to all HIR nodes reachable from `root`.

	Returns the next available NodeId after traversal.
	"""
	next_id = start
	seen: set[int] = set()

	def walk(obj: object) -> None:
		nonlocal next_id
		obj_id = id(obj)
		if obj_id in seen:
			return
		seen.add(obj_id)

		if isinstance(obj, H.HNode):
			if is_dataclass(obj) and getattr(obj, "__dataclass_params__", None) and obj.__dataclass_params__.frozen:
				object.__setattr__(obj, "node_id", next_id)
			else:
				obj.node_id = next_id
			next_id += 1

		if not _should_descend(obj):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk_value(getattr(obj, f.name))
		else:
			for val in vars(obj).values():
				walk_value(val)

	def walk_value(val: object) -> None:
		if val is None:
			return
		if isinstance(val, (list, tuple)):
			for item in val:
				walk_value(item)
			return
		if isinstance(val, dict):
			for key in sorted(val.keys(), key=repr):
				walk_value(val[key])
			return
		walk(val)

	def _should_descend(obj: object) -> bool:
		if isinstance(obj, H.HNode):
			return True
		if is_dataclass(obj) and obj.__class__.__module__ in _HIR_MODULES:
			return True
		return False

	walk(root)
	return next_id


def assign_callsite_ids(root: H.HNode, *, start: int = 0) -> int:
	"""
	Assign CallSiteIds to all call nodes reachable from `root`.

	Returns the next available CallSiteId after traversal.
	"""
	next_id = start
	seen: set[int] = set()

	def _is_call_node(obj: object) -> bool:
		return isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke))

	def walk(obj: object) -> None:
		nonlocal next_id
		obj_id = id(obj)
		if obj_id in seen:
			return
		seen.add(obj_id)

		if _is_call_node(obj):
			if is_dataclass(obj) and getattr(obj, "__dataclass_params__", None) and obj.__dataclass_params__.frozen:
				object.__setattr__(obj, "callsite_id", next_id)
			else:
				obj.callsite_id = next_id
			next_id += 1

		if not _should_descend(obj):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk_value(getattr(obj, f.name))
		else:
			for val in vars(obj).values():
				walk_value(val)

	def walk_value(val: object) -> None:
		if val is None:
			return
		if isinstance(val, (list, tuple)):
			for item in val:
				walk_value(item)
			return
		if isinstance(val, dict):
			for key in sorted(val.keys(), key=repr):
				walk_value(val[key])
			return
		walk(val)

	def _should_descend(obj: object) -> bool:
		if isinstance(obj, H.HNode):
			return True
		if is_dataclass(obj) and obj.__class__.__module__ in _HIR_MODULES:
			return True
		return False

	walk(root)
	return next_id


def validate_callsite_ids(root: H.HNode) -> None:
	"""
	Validate CallSiteIds for all call nodes reachable from `root`.
	"""
	ids: list[int] = []
	seen: set[int] = set()

	def walk(obj: object) -> None:
		obj_id = id(obj)
		if obj_id in seen:
			return
		seen.add(obj_id)
		if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
			callsite_id = getattr(obj, "callsite_id", None)
			if callsite_id is None:
				raise AssertionError("missing callsite_id on call node")
			ids.append(int(callsite_id))
		if not _should_descend(obj):
			return
		if is_dataclass(obj):
			for f in fields(obj):
				walk_value(getattr(obj, f.name))
		else:
			for val in vars(obj).values():
				walk_value(val)

	def walk_value(val: object) -> None:
		if val is None:
			return
		if isinstance(val, (list, tuple)):
			for item in val:
				walk_value(item)
			return
		if isinstance(val, dict):
			for key in sorted(val.keys(), key=repr):
				walk_value(val[key])
			return
		walk(val)

	def _should_descend(obj: object) -> bool:
		if isinstance(obj, H.HNode):
			return True
		if is_dataclass(obj) and obj.__class__.__module__ in _HIR_MODULES:
			return True
		return False

	walk(root)
	if not ids:
		return
	uniq = set(ids)
	if len(uniq) != len(ids):
		raise AssertionError("duplicate callsite_id values")
	lo, hi = min(uniq), max(uniq)
	if hi - lo + 1 != len(uniq):
		raise AssertionError("callsite_id range is not dense")


__all__ = ["assign_node_ids", "assign_callsite_ids", "validate_callsite_ids"]
