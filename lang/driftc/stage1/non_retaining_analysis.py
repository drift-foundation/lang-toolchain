# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Mapping, Optional, Set, Tuple

from lang.driftc.borrow_checker import EscapeLevel
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind, TypeTable
from lang.driftc.method_registry import CallableDecl
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.stage1.capture_discovery import discover_captures


@dataclass
class _ParamUsage:
	has_retain: bool = False
	has_direct_call: bool = False
	has_unknown_forward: bool = False
	forward_edges: Set[Tuple[FunctionId, int]] = field(default_factory=set)


def analyze_non_retaining_params(
	typed_fns: Mapping[FunctionId, object],
	signatures_by_id: Mapping[FunctionId, FnSignature],
	*,
	type_table: Optional[TypeTable] = None,
	semantic_world: object | None = None,
) -> dict[FunctionId, FnSignature]:
	"""
	Compute param_escape_level (LOCAL) for callable params in functions with bodies.

	The analysis is intentionally conservative:
	- direct call uses are allowed (cb(...), cb.call(...))
	- forwarding is allowed only to already-proven non-retaining params
	- any alias/store/return/capture yields retaining (param_escape_level stays None → THREAD default)

	Params proven non-retaining get param_escape_level=LOCAL.
	Retaining or unknown params get param_escape_level=None (THREAD default via effective_param_escape_level).
	"""
	working_sigs: dict[FunctionId, FnSignature] = dict(signatures_by_id)

	method_sig_by_key: dict[tuple[int, str], FnSignature] = {}
	sig_id_by_obj: dict[int, FunctionId] = {id(sig): fn_id for fn_id, sig in working_sigs.items()}
	for sig in working_sigs.values():
		if sig.is_method and sig.impl_target_type_id is not None:
			key = (sig.impl_target_type_id, sig.method_name or sig.name)
			method_sig_by_key[key] = sig

	def _raw_type_is_callable(raw: object | None) -> bool:
		def _is_fn_name(name: str) -> bool:
			if name.startswith("FnMut"):
				return name[5:].isdigit()
			if name.startswith("FnOnce"):
				return name[6:].isdigit()
			if name.startswith("Fn"):
				return name[2:].isdigit()
			return False

		def _is_callback_name(name: str) -> bool:
			return name.startswith("Callback") and name[8:].isdigit()

		if raw is None:
			return False
		if isinstance(raw, str):
			return raw == "fn" or _is_fn_name(raw) or _is_callback_name(raw)
		if hasattr(raw, "name"):
			name = getattr(raw, "name")
			args = getattr(raw, "args", None)
			if name in {"&", "&mut"} and args:
				return _raw_type_is_callable(args[0])
			return name == "fn" or _is_fn_name(name) or _is_callback_name(name)
		return False

	def _param_is_callable(sig: FnSignature, idx: int) -> bool:
		if sig.param_types and idx < len(sig.param_types):
			if _raw_type_is_callable(sig.param_types[idx]):
				return True
		if type_table is None:
			return False
		if sig.param_type_ids and idx < len(sig.param_type_ids):
			td = type_table.get(sig.param_type_ids[idx])
			return td.kind is TypeKind.FUNCTION or _raw_type_is_callable(td.name)
		return False

	def _param_count(sig: FnSignature) -> int:
		if sig.param_names:
			return len(sig.param_names)
		if sig.param_type_ids:
			return len(sig.param_type_ids)
		if sig.param_types:
			return len(sig.param_types)
		return 0

	def _pel_to_nr(lvl: Optional[EscapeLevel]) -> Optional[bool]:
		if lvl in (EscapeLevel.IMMEDIATE, EscapeLevel.LOCAL):
			return True  # IMMEDIATE is most-restrictive non-escaping; LOCAL is non-retaining
		if lvl in (EscapeLevel.THREAD, EscapeLevel.STATIC):
			return False
		return None  # SCOPED or None → unknown

	param_nonretaining_by_id: dict[FunctionId, list[Optional[bool]] | None] = {
		fn_id: (
			[_pel_to_nr(lvl) for lvl in sig.param_escape_level]
			if sig.param_escape_level is not None else None
		)
		for fn_id, sig in working_sigs.items()
	}

	def _ensure_param_nonretaining(fn_id: FunctionId, count: int) -> list[Optional[bool]] | None:
		cur = param_nonretaining_by_id.get(fn_id)
		if cur is None:
			cur = [None] * count if count else None
			param_nonretaining_by_id[fn_id] = cur
			return cur
		if len(cur) < count:
			cur.extend([None] * (count - len(cur)))
		return cur

	def _resolve_sig_for_call(call: H.HExpr, call_resolutions: Mapping[int, object]) -> tuple[FunctionId | None, FnSignature | None]:
		res = call_resolutions.get(call.node_id)
		if isinstance(res, CallableDecl):
			if res.fn_id is not None:
				return res.fn_id, working_sigs.get(res.fn_id)
			return None, None
		if isinstance(res, MethodResolution):
			if res.decl.fn_id is not None:
				return res.decl.fn_id, working_sigs.get(res.decl.fn_id)
			impl_target = res.decl.impl_target_type_id
			if impl_target is None:
				return None, None
			sig = method_sig_by_key.get((impl_target, res.decl.name))
			if sig is None:
				return None, None
			return sig_id_by_obj.get(id(sig)), sig
		return None, None

	def _param_index_for_call(
		sig: FnSignature,
		*,
		arg_index: int | None = None,
		kw_name: str | None = None,
	) -> int | None:
		if kw_name is not None:
			if not sig.param_names:
				return None
			try:
				idx = sig.param_names.index(kw_name)
			except ValueError:
				return None
			if sig.is_method and idx == 0:
				return None
			return idx
		if arg_index is None:
			return None
		return arg_index + 1 if sig.is_method else arg_index

	usage_by_fn: dict[FunctionId, list[_ParamUsage]] = {}
	eligible_by_fn: dict[FunctionId, set[int]] = {}

	for fn_id, typed_fn in typed_fns.items():
		sig = working_sigs.get(fn_id)
		if sig is None:
			continue
		param_count = len(getattr(typed_fn, "param_bindings", []) or [])
		ensure_count = max(param_count, _param_count(sig))
		_ensure_param_nonretaining(fn_id, ensure_count)
		if param_count == 0:
			continue

		param_bindings = list(getattr(typed_fn, "param_bindings", []) or [])
		binding_to_index = {bid: idx for idx, bid in enumerate(param_bindings)}
		usages = [_ParamUsage() for _ in range(param_count)]
		call_resolutions = getattr(typed_fn, "call_resolutions", None) or {}

		eligible: set[int] = set()
		for idx in range(param_count):
			if _param_is_callable(sig, idx):
				eligible.add(idx)

		def _binding_id_for_var(var: H.HVar) -> int | None:
			if var.binding_id is not None:
				return var.binding_id
			if hasattr(typed_fn, "binding_for_var"):
				return typed_fn.binding_for_var.get(var.node_id)
			return None

		def _plain_param_index(expr: H.HExpr) -> int | None:
			if isinstance(expr, H.HMove):
				# `move body` forwarding transfers ownership into the callee
				# param — the non-retaining proof obligation is identical to
				# plain forwarding, so look through the move to the place.
				# Without this, the generic walk reaches the HPlaceExpr arm
				# and marks the param RETAINING, poisoning proven chains
				# like `delegate(h, move body)` → `with_handle(h, body)`.
				expr = expr.subject
			if isinstance(expr, H.HVar):
				bid = _binding_id_for_var(expr)
				return binding_to_index.get(bid) if bid is not None else None
			if isinstance(expr, H.HPlaceExpr) and not expr.projections and isinstance(expr.base, H.HVar):
				bid = _binding_id_for_var(expr.base)
				return binding_to_index.get(bid) if bid is not None else None
			return None

		def _mark_retain(idx: int) -> None:
			usages[idx].has_retain = True

		def _handle_forward(
			idx: int,
			call: H.HExpr,
			*,
			arg_index: int | None = None,
			kw_name: str | None = None,
		) -> None:
			eligible.add(idx)
			fn_id, sig = _resolve_sig_for_call(call, call_resolutions)
			if sig is None or fn_id is None:
				usages[idx].has_unknown_forward = True
				return
			param_index = _param_index_for_call(sig, arg_index=arg_index, kw_name=kw_name)
			if param_index is None:
				usages[idx].has_unknown_forward = True
				return
			usages[idx].forward_edges.add((fn_id, param_index))

		def _walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HLambda):
				# Use the checker-final capture list when present.
				# `discover_captures` MUTATES `expr.captures` (it re-assigns
				# the discovered list), and this analysis also runs
				# post-check right before the borrow-check loop — a fresh
				# discovery there would overwrite call-resolver adjustments
				# (implicit-move capture kinds on boxed callbacks) and
				# corrupt the already-extracted hidden-lambda env contract
				# (surfaced as an SSA return-type contract failure on the
				# hidden fn). Discover only when captures were never set.
				caps = expr.captures or discover_captures(expr).captures
				for cap in caps or []:
					idx = binding_to_index.get(int(cap.key.root_local))
					if idx is not None:
						usages[idx].has_retain = True
				return
			if isinstance(expr, H.HCall):
				callee_idx = _plain_param_index(expr.fn)
				if callee_idx is not None:
					usages[callee_idx].has_direct_call = True
					eligible.add(callee_idx)
				else:
					_walk_expr(expr.fn)
				for arg_index, arg in enumerate(expr.args):
					idx = _plain_param_index(arg)
					if idx is not None:
						_handle_forward(idx, expr, arg_index=arg_index)
						continue
					_walk_expr(arg)
				for kw in expr.kwargs:
					idx = _plain_param_index(kw.value)
					if idx is not None:
						_handle_forward(idx, expr, kw_name=kw.name)
						continue
					_walk_expr(kw.value)
				return
			if isinstance(expr, getattr(H, "HInvoke", ())):
				callee_idx = _plain_param_index(expr.callee)
				if callee_idx is not None:
					usages[callee_idx].has_direct_call = True
					eligible.add(callee_idx)
				else:
					_walk_expr(expr.callee)
				for arg_index, arg in enumerate(expr.args):
					idx = _plain_param_index(arg)
					if idx is not None:
						_handle_forward(idx, expr, arg_index=arg_index)
						continue
					_walk_expr(arg)
				for kw in expr.kwargs:
					idx = _plain_param_index(kw.value)
					if idx is not None:
						_handle_forward(idx, expr, kw_name=kw.name)
						continue
					_walk_expr(kw.value)
				return
			if isinstance(expr, H.HExceptionInit):
				for arg in expr.pos_args:
					_walk_expr(arg)
				for kw in expr.kw_args:
					_walk_expr(kw.value)
				return
			if isinstance(expr, H.HMatchExpr):
				_walk_expr(expr.scrutinee)
				for arm in expr.arms:
					for st in arm.block.statements:
						_walk_stmt(st)
					if arm.result is not None:
						_walk_expr(arm.result)
				return
			if isinstance(expr, H.HTryExpr):
				_walk_expr(expr.attempt)
				for arm in expr.arms:
					for st in arm.block.statements:
						_walk_stmt(st)
					if arm.result is not None:
						_walk_expr(arm.result)
				return
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				for st in expr.body.statements:
					_walk_stmt(st)
				_walk_expr(expr.result)
				return
			if isinstance(expr, H.HMethodCall):
				callee_idx = None
				if expr.method_name == "call":
					callee_idx = _plain_param_index(expr.receiver)
				if callee_idx is not None:
					usages[callee_idx].has_direct_call = True
					eligible.add(callee_idx)
				else:
					_walk_expr(expr.receiver)
				for arg_index, arg in enumerate(expr.args):
					idx = _plain_param_index(arg)
					if idx is not None:
						_handle_forward(idx, expr, arg_index=arg_index)
						continue
					_walk_expr(arg)
				for kw in expr.kwargs:
					idx = _plain_param_index(kw.value)
					if idx is not None:
						_handle_forward(idx, expr, kw_name=kw.name)
						continue
					_walk_expr(kw.value)
				return
			if isinstance(expr, H.HPlaceExpr):
				idx = _plain_param_index(expr.base)
				if idx is not None:
					_mark_retain(idx)
				else:
					_walk_expr(expr.base)
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk_expr(proj.index)
				return
			if isinstance(expr, H.HVar):
				bid = _binding_id_for_var(expr)
				idx = binding_to_index.get(bid) if bid is not None else None
				if idx is not None:
					_mark_retain(idx)
				return
			for field_name in getattr(expr, "__dataclass_fields__", {}) or {}:
				val = getattr(expr, field_name, None)
				if isinstance(val, H.HExpr):
					_walk_expr(val)
				elif isinstance(val, list):
					for item in val:
						if isinstance(item, H.HExpr):
							_walk_expr(item)

		def _walk_stmt(stmt: H.HStmt) -> None:
			if isinstance(stmt, H.HBlock):
				for st in stmt.statements:
					_walk_stmt(st)
			elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				for st in stmt.block.statements:
					_walk_stmt(st)
			elif isinstance(stmt, H.HExprStmt):
				_walk_expr(stmt.expr)
			elif isinstance(stmt, H.HLet):
				_walk_expr(stmt.value)
			elif isinstance(stmt, H.HAssign):
				_walk_expr(stmt.target)
				_walk_expr(stmt.value)
			elif isinstance(stmt, H.HAugAssign):
				_walk_expr(stmt.target)
				_walk_expr(stmt.value)
			elif isinstance(stmt, H.HIf):
				_walk_expr(stmt.cond)
				for st in stmt.then_block.statements:
					_walk_stmt(st)
				if stmt.else_block:
					for st in stmt.else_block.statements:
						_walk_stmt(st)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					_walk_expr(stmt.value)
			elif isinstance(stmt, H.HLoop):
				for st in stmt.body.statements:
					_walk_stmt(st)
			elif isinstance(stmt, H.HTry):
				for st in stmt.body.statements:
					_walk_stmt(st)
				for arm in stmt.catches:
					for st in arm.block.statements:
						_walk_stmt(st)
			elif isinstance(stmt, H.HThrow):
				_walk_expr(stmt.value)
			elif isinstance(stmt, H.HMatchExpr):
				_walk_expr(stmt)
			elif isinstance(stmt, H.HTryExpr):
				_walk_expr(stmt)

		body = getattr(typed_fn, "body", None)
		if body is not None:
			_walk_stmt(body)
		usage_by_fn[fn_id] = usages
		eligible_by_fn[fn_id] = eligible

	def _target_status(target: tuple[FunctionId, int], *, internal_status: dict[tuple[FunctionId, int], Optional[bool]]) -> Optional[bool]:
		if target in internal_status:
			return internal_status[target]
		nr = param_nonretaining_by_id.get(target[0])
		if nr is None:
			return None
		if target[1] >= len(nr):
			return None
		return nr[target[1]]

	internal_status: dict[tuple[FunctionId, int], Optional[bool]] = {}
	for fn_id, usages in usage_by_fn.items():
		eligible = eligible_by_fn.get(fn_id, set())
		for idx, usage in enumerate(usages):
			if idx not in eligible:
				continue
			if usage.has_retain:
				internal_status[(fn_id, idx)] = False
			else:
				internal_status[(fn_id, idx)] = None

	changed = True
	while changed:
		changed = False
		for fn_id, usages in usage_by_fn.items():
			eligible = eligible_by_fn.get(fn_id, set())
			for idx, usage in enumerate(usages):
				if idx not in eligible:
					continue
				key = (fn_id, idx)
				if internal_status.get(key) is False:
					continue
				if usage.has_unknown_forward:
					continue
				if any(_target_status(edge, internal_status=internal_status) is not True for edge in usage.forward_edges):
					continue
				if internal_status.get(key) is not True:
					internal_status[key] = True
					changed = True

	for fn_id, usages in usage_by_fn.items():
		nr = param_nonretaining_by_id.get(fn_id)
		param_count = len(usages)
		if nr is None or len(nr) < param_count:
			nr = [None] * param_count
			param_nonretaining_by_id[fn_id] = nr
		eligible = eligible_by_fn.get(fn_id, set())
		for idx in range(param_count):
			if idx not in eligible:
				nr[idx] = None
				continue
			nr[idx] = internal_status.get((fn_id, idx))

	def _build_pel(fn_id: FunctionId, sig: FnSignature) -> Optional[list[Optional[EscapeLevel]]]:
		nr_list = param_nonretaining_by_id.get(fn_id)
		if nr_list is None:
			return sig.param_escape_level
		base = list(sig.param_escape_level) if sig.param_escape_level is not None else [None] * len(nr_list)
		while len(base) < len(nr_list):
			base.append(None)
		for i, v in enumerate(nr_list):
			if v is True:
				existing = base[i]
				if existing is None or existing.value > EscapeLevel.LOCAL.value:
					base[i] = EscapeLevel.LOCAL  # promote unannotated/permissive slot to LOCAL
				# else: existing is IMMEDIATE or LOCAL — preserve stricter annotation
			elif v is False:
				base[i] = None  # analysis proved retaining; clear any pre-seeded annotation
			# v is None: analysis inconclusive; leave existing annotation unchanged
		return base if any(x is not None for x in base) else None

	if semantic_world is not None:
		# Production: write analysis results to the overlay only.
		# FnSignature objects are not mutated.
		for fn_id, sig in working_sigs.items():
			pel = _build_pel(fn_id, sig)
			semantic_world.annotate_signature(fn_id, "param_escape_level", list(pel) if pel is not None else None)
		return dict(working_sigs)
	# Test-only fallback (no world): rewrite signatures with embedded param_escape_level.
	return {
		fn_id: dataclass_replace(sig, param_escape_level=_build_pel(fn_id, sig))
		for fn_id, sig in working_sigs.items()
	}


__all__ = ["analyze_non_retaining_params"]
