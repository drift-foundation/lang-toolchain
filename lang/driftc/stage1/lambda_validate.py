# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass

from lang.driftc.stage1.capture_discovery import discover_captures, resolve_projected_capture_type
from lang.driftc.stage1 import hir_nodes as H


@dataclass
class LambdaValidationResult:
	diagnostics: list


def validate_lambdas_non_retaining(
	node: H.HNode,
	*,
	signatures_by_id=None,
	call_resolutions=None,
	binding_types=None,
	type_table=None,
) -> LambdaValidationResult:
	"""
	Walk the HIR to discover and validate lambda captures (item 1 only).

	Item 1 (capture validation): run discover_captures on every lambda in the
	tree to detect overlap, inference conflicts, and capture-kind errors.

	Item 2 (escape-level enforcement) has been removed (Phase 3c) and is now
	owned by the borrow checker, which emits E_ESCAPE_THREAD / E_ESCAPE_STATIC
	/ E_ESCAPE_STORE at call-argument sites via _check_lambda_escape_level.

	Note: function-pointer coercion checks for REF/REF_MUT captures remain in
	type_checker.py and are not affected by this change.

	The signatures_by_id and call_resolutions parameters are accepted for
	backward compatibility with callers but are no longer used.

	`binding_types`/`type_table`, when both given (post-typecheck callers
	only — `driftc.py`'s post_check_analysis phase has real types here),
	let a MOVE-kind capture of a Copy-AND-bitcopy projected field (e.g.
	`p.count: Int`) downgrade to a plain COPY read instead of being
	rejected — see `capture_discovery.py::discover_captures`'s
	`is_copy_projected_field`. Without both, this call is the earliest
	lambda-capture pass in the pipeline and has no type information of its
	own, so it keeps the conservative blanket rejection.

	Narrowed to bitcopy types only (0.33.70 review finding): a
	Copy-but-non-bitcopy field (`String`, or a Copy struct/variant
	containing one) produced a confirmed heap-use-after-free for the
	struct/variant case when captured this way — see
	`borrow_checker_pass.py::_is_copy_projected_field`'s docstring for the
	full explanation. Bitcopy types have no refcount to double-own, so
	they sidestep the issue entirely. Note `is_bitcopy` is transitive for
	structs (a Copy struct of entirely-bitcopy fields is itself bitcopy)
	and never true for variants — this accepts a bitcopy Copy STRUCT field
	too, not only scalars.
	"""
	diags: list = []
	is_copy_projected_field = None
	if binding_types is not None and type_table is not None:
		def is_copy_projected_field(key):
			ty = resolve_projected_capture_type(key, binding_types, type_table)
			return ty is not None and bool(type_table.copy_status(ty)) and type_table.is_bitcopy(ty)

	def _is_ref_valued_type(ty) -> bool:
		"""True for `&T` / `&mut T` and `Optional<&T>` / `Optional<&mut T>`."""
		if ty is None or type_table is None:
			return False
		from lang.driftc.core.types_core import TypeKind as _TK
		td = type_table.get(ty)
		if td.kind is _TK.REF:
			return True
		inst = type_table.get_variant_instance(ty)
		if inst is None:
			return False
		optional_base = type_table.get_variant_base(module_id="lang.core", name="Optional")
		if optional_base is None or inst.base_id != optional_base or len(inst.type_args) != 1:
			return False
		return type_table.get(inst.type_args[0]).kind is _TK.REF

	def _unsafe_boxed_capture(lam: H.HLambda) -> tuple[str, str] | None:
		"""If `lam` is a boxed callback whose env would hold a raw pointer
		into the enclosing frame, return (code, message); else None.

		Two hazard classes (both make the closure's env carry a pointer whose
		referent's liveness nothing ties to the closure):
		- a MOVE/COPY capture whose captured VALUE is `&T`/`&mut T`/`Optional<&T>`
		  (implicit read of a ref-typed binding, or explicit `captures(copy
		  ref)` — `&T` is Copy so the plain Copy check passes it);
		- a REF/REF_MUT-KIND capture (e.g. an implicit borrow from a `&self`
		  method call like `x.clone()` on a captured local, which classifies
		  the capture REF ahead of the boxed MOVE default) — the env stores
		  the address of the enclosing frame's slot.
		"""
		if binding_types is None or type_table is None:
			return None
		if not getattr(lam, "capture_as_move", False):
			return None
		from lang.driftc.stage1 import closures as _C
		caps = lam.captures or discover_captures(lam, is_copy_projected_field=is_copy_projected_field).captures
		for cap in caps or []:
			if cap.kind in (_C.HCaptureKind.REF, _C.HCaptureKind.REF_MUT):
				return (
					"E_CALLBACK_BORROWED_CAPTURE",
					"boxed callback implicitly borrows a captured binding and escapes its "
					"defining scope; closures with borrowed captures are non-escaping in v0. "
					"Take ownership instead: `captures(move <name>)` (or `captures(copy "
					"<name>)` for a Copy value), or keep the callback local (only call it, "
					"do not store/return/pass it)",
				)
			if cap.kind not in (_C.HCaptureKind.MOVE, _C.HCaptureKind.COPY):
				continue
			if cap.key.proj:
				ty = resolve_projected_capture_type(cap.key, binding_types, type_table)
			else:
				ty = binding_types.get(int(cap.key.root_local))
			if _is_ref_valued_type(ty):
				return (
					"E_ESCAPE_REF_CAPTURE",
					"boxed callback captures a reference value and escapes its defining "
					"scope; the closure would carry the raw pointer past the frame that "
					"supplied it. Capture the owned value instead, pass the reference as a "
					"call argument, or keep the callback local (only call it, do not "
					"store/return/pass it)",
				)
		return None

	def _check_boxed_capture_escapes(root: H.HNode) -> None:
		"""Reject boxed callbacks with frame-pointer-carrying captures unless
		they provably stay local.

		A wrap (`core.callbackN(lambda)`) is LOCAL iff its value is only ever
		used as a method-call receiver: either invoked in place
		(`callback0(...).call(...)`) or bound with `val cb = callback0(...)`
		where every use of `cb` is a receiver position (`cb.call(...)`).
		Every other position — return value, constructor/call/method
		argument, assignment into a place, array literal element, `move` into
		anything — lets the box (and its raw pointer) outlive or leave the
		frame, which is exactly the use-after-scope this gate exists to stop.
		The receiver-only rule is what keeps the sound synchronous pattern
		(e.g. a `for`-binder `&T` captured by `captures(copy item)` and
		called within the iteration) compiling.
		"""
		wrapper_nodes: list[tuple[H.HCall, H.HLambda]] = []
		receiver_wrap_ids: set[int] = set()
		let_bound: dict[int, tuple[H.HCall, H.HLambda]] = {}
		binding_uses: dict[int, list[str]] = {}

		def _wrap_lambda(call: object) -> H.HLambda | None:
			if not isinstance(call, H.HCall):
				return None
			fn_name = getattr(call.fn, "name", None) or ""
			if not ("callback" in fn_name):
				return None
			inner = None
			if call.args and isinstance(call.args[0], H.HLambda):
				inner = call.args[0]
			elif call.kwargs and isinstance(call.kwargs[0].value, H.HLambda):
				inner = call.kwargs[0].value
			return inner

		def _scan(node: object, receiver_of_methodcall: bool = False) -> None:
			if isinstance(node, H.HLambda):
				# A nested lambda CAPTURING one of our wrap-bindings whisks
				# the box out through its env: record each capture root as an
				# escaping use. Then descend into the body — the user-fn
				# validation pass is the earliest (and in `--test-build-only`
				# mode the ONLY) point where nested wraps are visible, so the
				# scan must see wraps at every nesting depth.
				caps = node.captures or discover_captures(node, is_copy_projected_field=is_copy_projected_field).captures
				for cap in caps or []:
					binding_uses.setdefault(int(cap.key.root_local), []).append("other")
				if node.body_expr is not None:
					_scan(node.body_expr)
				if node.body_block is not None:
					for stmt in node.body_block.statements:
						_scan(stmt)
				return
			if isinstance(node, H.HCall):
				lam = _wrap_lambda(node)
				if lam is not None:
					wrapper_nodes.append((node, lam))
					if receiver_of_methodcall:
						receiver_wrap_ids.add(id(node))
			if isinstance(node, H.HVar) and getattr(node, "binding_id", None) is not None:
				binding_uses.setdefault(int(node.binding_id), []).append(
					"receiver" if receiver_of_methodcall else "other"
				)
			if isinstance(node, H.HLet):
				val = getattr(node, "value", None)
				lam = _wrap_lambda(val)
				bid = getattr(node, "binding_id", None)
				if lam is not None and bid is not None:
					let_bound[int(bid)] = (val, lam)
				_scan(val)
				return
			if isinstance(node, H.HMethodCall):
				_scan(node.receiver, receiver_of_methodcall=True)
				for a in node.args:
					_scan(a)
				for kw in node.kwargs:
					_scan(kw.value)
				return
			for _fname in getattr(node, "__dataclass_fields__", {}) or {}:
				_val = getattr(node, _fname, None)
				if isinstance(_val, (H.HExpr, H.HNode)):
					_scan(_val)
				elif isinstance(_val, list):
					for _item in _val:
						if isinstance(_item, (H.HExpr, H.HNode)):
							_scan(_item)

		_scan(root)
		let_wrap_ids = {id(call) for call, _ in let_bound.values()}
		for call, lam in wrapper_nodes:
			hazard = _unsafe_boxed_capture(lam)
			if hazard is None:
				continue
			if id(call) in receiver_wrap_ids:
				continue  # invoked in place — never leaves the expression
			if id(call) in let_wrap_ids:
				bid = next(b for b, (c, _) in let_bound.items() if id(c) == id(call))
				uses = binding_uses.get(bid, [])
				if uses and all(u == "receiver" for u in uses):
					continue  # bound locally and only ever called
				if not uses:
					continue  # bound and never used — cannot escape
			code, message = hazard
			from lang.driftc.core.diagnostics import Diagnostic
			span = getattr(lam, "span", None)
			for cap in lam.captures or []:
				cspan = getattr(cap, "span", None)
				if cspan is not None and getattr(cspan, "line", None) is not None:
					span = cspan
					break
			diags.append(
				Diagnostic(severity="error", code=code, message=message, phase="typecheck", span=span)
			)

	def _iter_expr_children(e: H.HExpr) -> list:
		children: list = []
		for field_name in getattr(e, "__dataclass_fields__", {}) or {}:
			val = getattr(e, field_name, None)
			if isinstance(val, H.HExpr):
				children.append(val)
			elif isinstance(val, list):
				for item in val:
					if isinstance(item, H.HExpr):
						children.append(item)
		return children

	def _walk_expr(e: H.HExpr) -> None:
		if isinstance(e, H.HLambda):
			res = discover_captures(e, is_copy_projected_field=is_copy_projected_field)
			diags.extend(res.diagnostics)
			if e.body_expr is not None:
				_walk_expr(e.body_expr)
			if e.body_block is not None:
				for stmt in e.body_block.statements:
					_walk_stmt(stmt)
			return
		if isinstance(e, H.HPlaceExpr):
			_walk_expr(e.base)
			for proj in e.projections:
				if isinstance(proj, H.HPlaceIndex):
					_walk_expr(proj.index)
			return
		if isinstance(e, H.HCall):
			_walk_expr(e.fn)
			for arg in e.args:
				_walk_expr(arg)
			for kw in e.kwargs:
				_walk_expr(kw.value)
			return
		if isinstance(e, getattr(H, "HInvoke", ())):
			_walk_expr(e.callee)
			for arg in e.args:
				_walk_expr(arg)
			for kw in e.kwargs:
				_walk_expr(kw.value)
			return
		if isinstance(e, H.HMethodCall):
			_walk_expr(e.receiver)
			for arg in e.args:
				_walk_expr(arg)
			for kw in e.kwargs:
				_walk_expr(kw.value)
			return
		if isinstance(e, H.HExceptionInit):
			for arg in e.pos_args:
				_walk_expr(arg)
			for kw in e.kw_args:
				_walk_expr(kw.value)
			return
		if isinstance(e, H.HMatchExpr):
			_walk_expr(e.scrutinee)
			for arm in e.arms:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
				if arm.result is not None:
					_walk_expr(arm.result)
			return
		if isinstance(e, H.HTryExpr):
			_walk_expr(e.attempt)
			for arm in e.arms:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
				if arm.result is not None:
					_walk_expr(arm.result)
			return
		if hasattr(H, "HUnsafeExpr") and isinstance(e, getattr(H, "HUnsafeExpr")):
			for stmt in e.body.statements:
				_walk_stmt(stmt)
			_walk_expr(e.result)
			return
		if isinstance(e, H.HTernary):
			_walk_expr(e.cond)
			_walk_expr(e.then_expr)
			_walk_expr(e.else_expr)
			return
		for child in _iter_expr_children(e):
			_walk_expr(child)

	def _walk_stmt(s: H.HStmt) -> None:
		if isinstance(s, H.HBlock):
			for stmt in s.statements:
				_walk_stmt(stmt)
		elif hasattr(H, "HUnsafeBlock") and isinstance(s, getattr(H, "HUnsafeBlock")):
			for stmt in s.block.statements:
				_walk_stmt(stmt)
		elif isinstance(s, H.HExprStmt):
			_walk_expr(s.expr)
		elif isinstance(s, H.HLet):
			_walk_expr(s.value)
		elif isinstance(s, H.HAssign):
			_walk_expr(s.target)
			_walk_expr(s.value)
		elif isinstance(s, H.HAugAssign):
			_walk_expr(s.target)
			_walk_expr(s.value)
		elif isinstance(s, H.HIf):
			_walk_expr(s.cond)
			for stmt in s.then_block.statements:
				_walk_stmt(stmt)
			if s.else_block:
				for stmt in s.else_block.statements:
					_walk_stmt(stmt)
		elif isinstance(s, H.HReturn):
			if s.value is not None:
				_walk_expr(s.value)
		elif isinstance(s, H.HLoop):
			for stmt in s.body.statements:
				_walk_stmt(stmt)
		elif isinstance(s, H.HTry):
			for stmt in s.body.statements:
				_walk_stmt(stmt)
			for arm in s.catches:
				for stmt in arm.block.statements:
					_walk_stmt(stmt)
		elif isinstance(s, H.HThrow):
			_walk_expr(s.value)
		elif isinstance(s, H.HMatchExpr):
			_walk_expr(s)
		elif isinstance(s, H.HTryExpr):
			_walk_expr(s)

	if isinstance(node, H.HExpr):
		_walk_expr(node)
	elif isinstance(node, H.HStmt):
		_walk_stmt(node)
	_check_boxed_capture_escapes(node)
	return LambdaValidationResult(diagnostics=diags)
