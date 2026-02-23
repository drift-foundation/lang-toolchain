# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Set

from lang.driftc.core.diagnostics import Diagnostic

# Capture discovery diagnostics are typecheck-phase.
def _cap_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)

from lang.driftc.core.span import Span
from lang.driftc.stage1 import closures as C
from lang.driftc.stage1 import hir_nodes as H


@dataclass
class CaptureDiscoveryResult:
	captures: list[C.HCapture]
	diagnostics: list[Diagnostic]


@dataclass
class _CaptureUsage:
	read: bool = False
	borrow_shared: bool = False
	borrow_mut: bool = False
	move: bool = False
	write: bool = False
	span: Span = Span()


def discover_captures(lambda_expr: H.HLambda) -> CaptureDiscoveryResult:
	"""
	Discover captures for a lambda (v0: locals/params + field projections only).

	- Allowed capture roots: locals/params (BindingId)
	- Allowed projections: field chain (no index/deref)
	- Captures are deduped and sorted deterministically.
	"""
	usage: dict[C.HCaptureKey, _CaptureUsage] = {}
	diags: list[Diagnostic] = []
	used_roots: Set[int] = set()
	used_root_spans: dict[int, Span] = {}
	used_root_names: dict[int, set[str]] = {}

	lambda_local_ids: Set[H.BindingId] = set()
	lambda_local_names: Set[str] = set()
	for p in lambda_expr.params:
		if p.binding_id is not None:
			lambda_local_ids.add(p.binding_id)
		lambda_local_names.add(p.name)

	def _record_local_from_stmt(stmt: H.HStmt) -> None:
		if isinstance(stmt, H.HLocalConst) and stmt.binding_id is not None:
			lambda_local_ids.add(stmt.binding_id)
			lambda_local_names.add(stmt.name)
		if isinstance(stmt, H.HLet) and stmt.binding_id is not None:
			lambda_local_ids.add(stmt.binding_id)
			lambda_local_names.add(stmt.name)

	def _add_usage(
		root: H.BindingId | None,
		fields: list[str],
		span: Span,
		root_name: str | None = None,
		*,
		read: bool = False,
		borrow_shared: bool = False,
		borrow_mut: bool = False,
		move: bool = False,
		write: bool = False,
	) -> None:
		if root is None or root in lambda_local_ids:
			return
		used_roots.add(int(root))
		if int(root) not in used_root_spans:
			used_root_spans[int(root)] = span
		if root_name is not None and root_name != "":
			used_root_names.setdefault(int(root), set()).add(root_name)
		key = C.HCaptureKey(root_local=root, proj=tuple(C.HCaptureProj(field=f) for f in fields))
		entry = usage.setdefault(key, _CaptureUsage())
		if entry.span == Span():
			entry.span = span
		entry.read = entry.read or read
		entry.borrow_shared = entry.borrow_shared or borrow_shared
		entry.borrow_mut = entry.borrow_mut or borrow_mut
		entry.move = entry.move or move
		entry.write = entry.write or write

	def _flatten_field_chain(expr: H.HExpr) -> tuple[H.BindingId | None, list[str], str | None] | None:
		# HPlaceExpr (canonical place)
		if isinstance(expr, H.HPlaceExpr):
			root = getattr(expr.base, "binding_id", None)
			root_name = expr.base.name if isinstance(expr.base, H.HVar) else None
			fields: list[str] = []
			for proj in expr.projections:
				if isinstance(proj, H.HPlaceField):
					fields.append(proj.name)
				else:
					return None
			return (root, fields, root_name)
		# HField chain rooted in HVar
		if isinstance(expr, H.HField):
			inner = _flatten_field_chain(expr.subject)
			if inner is None:
				return None
			root, fields, root_name = inner
			return (root, fields + [expr.name], root_name)
		if isinstance(expr, H.HVar):
			return (expr.binding_id, [], expr.name)
		return None

	def _walk_place_expr(place: H.HExpr, *, usage_kind: str) -> None:
		flattened = _flatten_field_chain(place)
		if flattened is None:
			root = getattr(getattr(place, "base", None), "binding_id", None)
			if root is not None and root not in lambda_local_ids:
				diags.append(
					_cap_diag(
						message="lambda captures support field projections only in v0",
						severity="error",
						span=getattr(place, "loc", Span()),
					)
				)
			if isinstance(place, H.HPlaceExpr):
				for proj in place.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk_expr(proj.index)
			return
		if flattened is not None:
			root, fields, root_name = flattened
			_add_usage(
				root,
				fields,
				getattr(place, "loc", Span()),
				root_name=root_name,
				read=usage_kind == "read",
				borrow_shared=usage_kind == "borrow_shared",
				borrow_mut=usage_kind == "borrow_mut",
				move=usage_kind == "move",
				write=usage_kind == "write",
			)
		if isinstance(place, H.HPlaceExpr):
			for proj in place.projections:
				if isinstance(proj, H.HPlaceIndex):
					_walk_expr(proj.index)

	def _walk_expr(e: H.HExpr) -> None:
		# Skip nested lambdas; captures are per-lambda.
		if isinstance(e, H.HLambda):
			return
		if isinstance(e, H.HMove):
			_walk_place_expr(e.subject, usage_kind="move")
			return
		if isinstance(e, H.HBorrow):
			_walk_place_expr(e.subject, usage_kind="borrow_mut" if e.is_mut else "borrow_shared")
			return
		if isinstance(e, H.HVar):
			_add_usage(e.binding_id, [], getattr(e, "span", Span()), root_name=e.name, read=True)
			return
		elif isinstance(e, (H.HPlaceExpr, H.HField)):
			flattened = _flatten_field_chain(e)
			if flattened is not None:
				root, fields, root_name = flattened
				_add_usage(root, fields, getattr(e, "loc", getattr(e, "span", Span())), root_name=root_name, read=True)
			elif isinstance(e, H.HPlaceExpr):
				root = getattr(e.base, "binding_id", None)
				if root is not None and root not in lambda_local_ids:
					diags.append(
						_cap_diag(
							message="lambda captures support field projections only in v0",
							severity="error",
							span=getattr(e, "loc", Span()),
						)
					)
			if isinstance(e, H.HPlaceExpr):
				for proj in e.projections:
					if isinstance(proj, H.HPlaceIndex):
						_walk_expr(proj.index)
			return
		# Traverse children
		for child in _iter_expr_children(e):
			_walk_expr(child)

	def _iter_expr_children(e: H.HExpr) -> list[H.HExpr]:
		children: list[H.HExpr] = []
		for field_name in getattr(e, "__dataclass_fields__", {}) or {}:
			val = getattr(e, field_name, None)
			if isinstance(val, H.HExpr):
				children.append(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, H.HExpr):
						children.append(item)
		return children

	def _walk_stmt(s: H.HStmt) -> None:
		_record_local_from_stmt(s)
		if isinstance(s, H.HBlock):
			for st in s.statements:
				_walk_stmt(st)
		elif hasattr(H, "HUnsafeBlock") and isinstance(s, getattr(H, "HUnsafeBlock")):
			for st in s.block.statements:
				_walk_stmt(st)
		elif isinstance(s, H.HExprStmt):
			_walk_expr(s.expr)
		elif isinstance(s, H.HLocalConst):
			pass  # literal value, no captures
		elif isinstance(s, H.HLet):
			_walk_expr(s.value)
		elif isinstance(s, H.HAssign):
			if isinstance(s.target, H.HPlaceExpr):
				_walk_place_expr(s.target, usage_kind="write")
			else:
				flattened = _flatten_field_chain(s.target)
				if flattened is not None:
					root, fields, root_name = flattened
					_add_usage(root, fields, getattr(s.target, "loc", Span()), root_name=root_name, write=True)
			_walk_expr(s.value)
		elif isinstance(s, H.HAugAssign):
			if isinstance(s.target, H.HPlaceExpr):
				_walk_place_expr(s.target, usage_kind="write")
			else:
				flattened = _flatten_field_chain(s.target)
				if flattened is not None:
					root, fields, root_name = flattened
					_add_usage(root, fields, getattr(s.target, "loc", Span()), root_name=root_name, write=True)
			_walk_expr(s.value)
		elif isinstance(s, H.HIf):
			_walk_expr(s.cond)
			for st in s.then_block.statements:
				_walk_stmt(st)
			if s.else_block:
				for st in s.else_block.statements:
					_walk_stmt(st)
		elif isinstance(s, H.HReturn):
			if s.value is not None:
				_walk_expr(s.value)
		elif isinstance(s, H.HLoop):
			for st in s.body.statements:
				_walk_stmt(st)
		elif isinstance(s, H.HTry):
			for st in s.body.statements:
				_walk_stmt(st)
			for arm in s.catches:
				for st in arm.block.statements:
					_walk_stmt(st)
		elif isinstance(s, H.HThrow):
			_walk_expr(s.value)
		elif isinstance(s, H.HMatchExpr):
			_walk_expr(s)
		elif isinstance(s, H.HTryExpr):
			_walk_expr(s)

	def _kind_from_explicit(kind: str, span: Span) -> C.HCaptureKind | None:
		if kind == "ref":
			return C.HCaptureKind.REF
		if kind == "ref_mut":
			return C.HCaptureKind.REF_MUT
		if kind == "copy":
			return C.HCaptureKind.COPY
		if kind == "move":
			return C.HCaptureKind.MOVE
		if kind == "auto":
			return C.HCaptureKind.REF
		diags.append(
			_cap_diag(
				message="unsupported explicit capture kind",
				severity="error",
				span=span,
			)
		)
		return None

	explicit_caps = getattr(lambda_expr, "explicit_captures", None)

	# Seed locals from body statements (params already collected).
	if lambda_expr.body_block is not None:
		for stmt in lambda_expr.body_block.statements:
			_record_local_from_stmt(stmt)
			_walk_stmt(stmt)
	if lambda_expr.body_expr is not None:
		_walk_expr(lambda_expr.body_expr)

	if explicit_caps is not None:
		root_usage: dict[int, _CaptureUsage] = {}
		for key, use in usage.items():
			root = key.root_local
			if root is None:
				continue
			entry = root_usage.setdefault(int(root), _CaptureUsage())
			entry.read = entry.read or use.read
			entry.borrow_shared = entry.borrow_shared or use.borrow_shared
			entry.borrow_mut = entry.borrow_mut or use.borrow_mut
			entry.move = entry.move or use.move
			entry.write = entry.write or use.write
			if entry.span == Span():
				entry.span = use.span
		seen_names: set[str] = set()
		explicit_roots: set[int] = set()
		explicit_names: dict[int, str] = {}
		explicit_list: list[C.HCapture] = []
		for cap in explicit_caps:
			if cap.name in seen_names:
				diags.append(
					_cap_diag(
						message="duplicate capture in captures(...) list",
						severity="error",
						span=cap.span,
					)
				)
				continue
			seen_names.add(cap.name)
			if cap.binding_id is None:
				diags.append(
					_cap_diag(
						message="explicit captures require a root identifier from the enclosing scope",
						severity="error",
						span=cap.span,
					)
				)
				continue
			kind = _kind_from_explicit(cap.kind, cap.span)
			if kind is None:
				continue
			if cap.kind == "auto":
				use = root_usage.get(int(cap.binding_id))
				if use is not None:
					if use.move:
						diags.append(
							_cap_diag(
								message=f"capture '{cap.name}' moves value; capture move {cap.name} to move",
								severity="error",
								span=use.span if use.span != Span() else cap.span,
							)
						)
						continue
					if use.write or use.borrow_mut:
						kind = C.HCaptureKind.REF_MUT
					elif use.borrow_shared or use.read:
						kind = C.HCaptureKind.REF
			explicit_roots.add(int(cap.binding_id))
			explicit_names[int(cap.binding_id)] = cap.name
			explicit_list.append(
				C.HCapture(
					kind=kind,
					key=C.HCaptureKey(root_local=cap.binding_id, proj=()),
					span=cap.span,
				)
			)
		for name in lambda_local_names:
			if name in seen_names:
				diags.append(
					_cap_diag(
						message="capture name collides with lambda param/local",
						severity="error",
						span=getattr(lambda_expr, "span", Span()),
					)
				)
		for root_id in used_roots:
			if root_id in explicit_roots:
				continue
			root_names = used_root_names.get(root_id)
			if root_names is not None and any(name in lambda_local_names for name in root_names):
				continue
			span = used_root_spans.get(root_id, Span())
			diags.append(
				_cap_diag(
					message="value used in closure body is not listed in captures(...)",
					severity="error",
					span=span,
				)
			)
		for cap in explicit_list:
			if cap.kind is not C.HCaptureKind.REF:
				continue
			root_id = int(cap.key.root_local)
			use = root_usage.get(root_id)
			if use is None:
				continue
			if use.write or use.borrow_mut:
				name = explicit_names.get(root_id, "value")
				span = use.span if use.span != Span() else cap.span
				diags.append(
					_cap_diag(
						message=f"capture '{name}' is shared; capture &mut {name} to mutate",
						severity="error",
						span=span,
					)
				)
		lambda_expr.captures = explicit_list
		return CaptureDiscoveryResult(captures=explicit_list, diagnostics=diags)

	captures: list[C.HCapture] = []
	capture_as_move = bool(getattr(lambda_expr, "capture_as_move", False))
	for key, use in usage.items():
		if use.move and key.proj:
			diags.append(
				_cap_diag(
					message="lambda move captures of projections are not supported yet",
					severity="error",
					span=use.span,
				)
			)
		if use.move and (use.borrow_shared or use.borrow_mut or use.write):
			diags.append(
				_cap_diag(
					message="lambda capture mixes move and borrow/write uses",
					severity="error",
					span=use.span,
				)
			)
		if use.borrow_shared and (use.borrow_mut or use.write):
			diags.append(
				_cap_diag(
					message="lambda capture uses both shared and mutable access",
					severity="error",
					span=use.span,
				)
			)
		if use.move:
			kind = C.HCaptureKind.MOVE
		elif use.borrow_mut or use.write:
			kind = C.HCaptureKind.REF_MUT
		elif use.borrow_shared:
			kind = C.HCaptureKind.REF
		elif use.read:
			kind = C.HCaptureKind.MOVE if capture_as_move else C.HCaptureKind.REF
		else:
			continue
		captures.append(C.HCapture(kind=kind, key=key, span=use.span))

	def _overlaps(a: C.HCaptureKey, b: C.HCaptureKey) -> bool:
		if a.root_local != b.root_local:
			return False
		if len(a.proj) <= len(b.proj):
			return a.proj == b.proj[: len(a.proj)]
		return b.proj == a.proj[: len(b.proj)]

	for i, cap_a in enumerate(captures):
		for cap_b in captures[i + 1 :]:
			if not _overlaps(cap_a.key, cap_b.key):
				continue
			if cap_a.kind is C.HCaptureKind.REF and cap_b.kind is C.HCaptureKind.REF:
				continue
			diags.append(
				_cap_diag(
					message="overlapping lambda captures are not supported with mutable or move captures",
					severity="error",
					span=cap_a.span,
				)
			)

	sorted_caps = C.sort_captures(captures)
	lambda_expr.captures = sorted_caps
	return CaptureDiscoveryResult(captures=sorted_caps, diagnostics=diags)
