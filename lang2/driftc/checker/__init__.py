# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Minimal checker stub for lang2.

This module exists solely to give the driver a place to hang checker-provided
metadata (currently: `declared_can_throw`). It is *not* a full type checker and
should be replaced by a real implementation once the type system is wired in.

When the real checker lands, this package will:

* resolve function signatures (`FnResult` return, `throws(...)` clause),
* validate catch-arm event names against the exception catalog, and
* populate a concrete `TypeEnv` for stage4 type-aware checks.

For now we only thread a boolean throw intent per function through to the driver
and validate catch-arm shapes when provided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Callable, FrozenSet, Mapping, Sequence, Set, Tuple, TYPE_CHECKING

from lang2.driftc.core.diagnostics import Diagnostic
from lang2.driftc.core.span import Span
from lang2.driftc.core.types_protocol import TypeEnv
from lang2.driftc.checker.catch_arms import CatchArmInfo, validate_catch_arms
from lang2.driftc.core.type_resolve_common import resolve_opaque_type
from lang2.driftc.core.types_core import TypeTable, TypeId, TypeKind
from lang2.driftc.stage1.hir_utils import collect_catch_arms_from_block
from lang2.driftc.stage1.normalize import normalize_hir

if TYPE_CHECKING:
	from lang2.driftc import stage1 as H


@dataclass
class FnSignature:
	"""
	Placeholder function signature used by the stub checker.

	Only `name`, `return_type`, and optional `throws_events` are represented,
	with placeholders for resolved TypeIds, param types, and throws flags. The
	real checker will replace this with its own type-checked signature
	structure. The TypeId fields are the canonical ones; the raw fields exist
	only for legacy/test scaffolding.
	"""

	name: str
	loc: Optional[Any] = None

	# Display name used at call sites (e.g., method name without type scoping).
	method_name: Optional[str] = None
	# Canonical, type-checked fields (preferred).
	param_type_ids: Optional[list[TypeId]] = None
	return_type_id: Optional[TypeId] = None
	declared_can_throw: Optional[bool] = None
	is_extern: bool = False
	is_intrinsic: bool = False
	param_names: Optional[list[str]] = None
	error_type_id: Optional[TypeId] = None  # resolved error TypeId
	# Method metadata (set when the declaration comes from an `implement Type` block).
	is_method: bool = False
	self_mode: Optional[str] = None  # "value", "ref", "ref_mut" for now; parser maps to this
	impl_target_type_id: Optional[TypeId] = None

	# Legacy/raw fields (to be removed once real type checker is wired).
	return_type: Any = None
	throws_events: Tuple[str, ...] = ()
	param_types: Optional[list[Any]] = None  # raw param type shapes (strings/tuples)
	module: Optional[str] = None


@dataclass
class FnInfo:
	"""
	Per-function checker metadata (placeholder).

	Only `name` and `declared_can_throw` are populated by this stub. Real
	`FnInfo` will carry richer information such as declared event set, return
	type, and source span for diagnostics. `signature` is the canonical source
	of truth for return/param types and throws flags.
	"""

	name: str
	declared_can_throw: bool
	signature: Optional[FnSignature] = None
	inferred_may_throw: bool = False

	# Optional fields reserved for the real checker; left as None here.
	declared_events: Optional[FrozenSet[str]] = None
	span: Optional[Any] = None  # typically a Span/Location
	return_type: Optional[Any] = None  # legacy placeholder (to be TypeId)
	error_type: Optional[Any] = None   # legacy placeholder (to be TypeId)
	return_type_id: Optional[TypeId] = None
	error_type_id: Optional[TypeId] = None


@dataclass
class CheckedProgram:
	"""
	Container returned by the checker.

	In the stub this only carries fn_infos; real implementations will also
	provide a concrete TypeEnv, diagnostics, and the exception catalog for
	catch/throws validation.
	"""

	fn_infos: Dict[str, FnInfo]
	type_table: Optional["TypeTable"] = None
	type_env: Optional[TypeEnv] = None
	exception_catalog: Optional[Dict[str, int]] = None
	diagnostics: List[Diagnostic] = field(default_factory=list)


class Checker:
	"""
	Placeholder checker.

	Accepts a sequence of function declarations and an optional declared_can_throw
	map (defaults to False for all). This input is strictly a testing shim; new
	callers should prefer `signatures` and treat `declared_can_throw` as a
	deprecated convenience. A real checker will compute declared_can_throw from
	signatures (FnResult/throws) and the type system, and validate catch arms
	against an exception catalog. The `declared_can_throw` map is a legacy path
	and slated for removal once real signatures land.
	"""

	def __init__(
		self,
		declared_can_throw: Mapping[str, bool] | None = None,
		signatures: Mapping[str, FnSignature] | None = None,
		exception_catalog: Mapping[str, int] | None = None,
		hir_blocks: Mapping[str, "H.HBlock"] | None = None,  # type: ignore[name-defined]
		type_table: "TypeTable" | None = None,
	) -> None:
		# Until a real type checker exists we support two testing shims:
		# 1) an explicit name -> bool map, or
		# 2) a name -> FnSignature map, from which we can infer can-throw based
		#    on the return type resembling FnResult.
		self._declared_map = declared_can_throw or {}
		self._signatures = signatures or {}
		self._catch_arms = self._normalize_and_collect_catch_arms(hir_blocks or {})
		self._exception_catalog = dict(exception_catalog) if exception_catalog else None
		self._hir_blocks = hir_blocks or {}
		# Use shared TypeTable when supplied; otherwise create a local one.
		self._type_table = type_table or TypeTable()
		# Index signatures by display name for approximate method lookups.
		self._sigs_by_display_name: Dict[str, list[FnSignature]] = {}
		for sig in self._signatures.values():
			disp = sig.method_name or sig.name
			self._sigs_by_display_name.setdefault(disp, []).append(sig)

		def _find_named(kind: TypeKind, name: str) -> TypeId | None:
			"""
			Best-effort lookup on a shared table to avoid minting duplicate TypeIds
			when the resolver already seeded common scalars/errors.
			"""
			for ty_id, ty_def in getattr(self._type_table, "_defs", {}).items():  # type: ignore[attr-defined]
				if ty_def.kind is kind and ty_def.name == name:
					return ty_id
			return None

		# Seed common scalars only when missing on the shared table. Cache them on
		# the table so downstream reuse sees consistent ids.
		self._int_type = _find_named(TypeKind.SCALAR, "Int") or self._type_table.ensure_int()
		self._bool_type = _find_named(TypeKind.SCALAR, "Bool") or self._type_table.ensure_bool()
		self._string_type = _find_named(TypeKind.SCALAR, "String") or self._type_table.ensure_string()
		self._uint_type = _find_named(TypeKind.SCALAR, "Uint") or self._type_table.ensure_uint()
		self._void_type = _find_named(TypeKind.VOID, "Void") or self._type_table.ensure_void()
		self._error_type = _find_named(TypeKind.ERROR, "Error") or self._type_table.ensure_error()
		self._unknown_type = _find_named(TypeKind.UNKNOWN, "Unknown") or self._type_table.ensure_unknown()
		# TODO: remove declared_can_throw shim once real parser/type checker supplies signatures.

	def _normalize_and_collect_catch_arms(self, hir_blocks: Mapping[str, "H.HBlock"]) -> dict[str, list[list[CatchArmInfo]]]:
		"""
		Normalize HIR blocks and collect catch-arm info uniformly for all callers.

		This keeps catch validation consistent between the CLI and stub helpers.
		"""
		catch_arms: dict[str, list[list[CatchArmInfo]]] = {}
		for name, block in hir_blocks.items():
			hir_norm = normalize_hir(block)
			arms = collect_catch_arms_from_block(hir_norm)
			if arms:
				catch_arms[name] = arms
		return catch_arms

	def check(self, fn_decls: Iterable[str]) -> CheckedProgram:
		"""
		Produce a CheckedProgram with FnInfo for each fn name in `fn_decls`.

		This stub also validates any provided catch arms against the
		exception catalog when available, accumulating diagnostics instead
		of raising.
		"""
		fn_infos: Dict[str, FnInfo] = {}
		diagnostics: List[Diagnostic] = []
		known_events: Set[str] = set(self._exception_catalog.keys()) if self._exception_catalog else set()

		for name in fn_decls:
			declared_can_throw = self._declared_map.get(name)
			sig = self._signatures.get(name)
			declared_events: Optional[FrozenSet[str]] = None
			return_type = None
			return_type_id: Optional[TypeId] = None
			error_type_id: Optional[TypeId] = None

			if sig is not None:
				declared_events = frozenset(sig.throws_events) if sig.throws_events else None
				# Prefer pre-resolved TypeIds if supplied; fall back to legacy resolution.
				return_type_id = sig.return_type_id
				error_type_id = sig.error_type_id
				if return_type_id is None:
					return_type_id, error_type_id = self._resolve_signature_types(sig)
					sig.return_type_id = return_type_id
					sig.error_type_id = error_type_id
				elif error_type_id is None:
					# If the signature already carries a FnResult TypeId, derive the error side.
					td = self._type_table.get(return_type_id)
					if td.kind is TypeKind.FNRESULT and len(td.param_types) >= 2:
						error_type_id = td.param_types[1]
						sig.error_type_id = error_type_id

				if sig.param_type_ids is None:
					sig.param_type_ids = self._resolve_param_types(sig)

				# Keep legacy/raw fields for backward compatibility.
				return_type = sig.return_type
				if declared_events is None and sig.throws_events:
					declared_events = frozenset(sig.throws_events)
				if sig.declared_can_throw is None and sig.throws_events:
					sig.declared_can_throw = True
				if sig.declared_can_throw is not None:
					declared_can_throw = sig.declared_can_throw

			if declared_can_throw is None:
				if sig is not None:
					# Prefer resolved type ids to legacy raw shapes.
					if sig.return_type_id is not None:
						td = self._type_table.get(sig.return_type_id)
						declared_can_throw = td.kind is TypeKind.FNRESULT
					else:
						declared_can_throw = False
				else:
					declared_can_throw = False

			catch_arms_groups = self._catch_arms.get(name)
			if catch_arms_groups is not None:
				for arms in catch_arms_groups:
					validate_catch_arms(arms, known_events, diagnostics)

			fn_infos[name] = FnInfo(
				name=name,
				declared_can_throw=declared_can_throw,
				signature=sig,
				declared_events=declared_events,
				return_type=return_type,  # legacy/raw
				return_type_id=return_type_id,
				error_type_id=error_type_id,
			)

			# Consistency check: declared_can_throw should align with the resolved
			# return type shape. This guards legacy bool-map overrides from drifting
			# away from signature intent.
			if declared_can_throw and return_type_id is not None:
				td = self._type_table.get(return_type_id)
				if td.kind is not TypeKind.FNRESULT:
					diagnostics.append(
						Diagnostic(
							message=(
								f"function {name} is marked can-throw but return type {td.name!r} "
								f"is not FnResult"
							),
							severity="error",
							span=None,
						)
					)
			if not declared_can_throw and return_type_id is not None:
				td = self._type_table.get(return_type_id)
				if td.kind is TypeKind.FNRESULT:
					diagnostics.append(
						Diagnostic(
							message=(
								f"function {name} returns FnResult but is not declared can-throw; "
								f"use FnResult return or mark throws accordingly"
							),
							severity="error",
							span=None,
						)
					)

		# Validate result-driven try sugar operands when HIR is available. This is
		# intentionally conservative: only known FnResult-returning calls are
		# accepted; everything else produces a diagnostic so that non-FnResult
		# operands are rejected early.
		for fn_name, hir_block in self._hir_blocks.items():
			if fn_name not in fn_infos:
				continue
			self._validate_try_results(fn_name, hir_block, diagnostics)

		# TODO: real checker will:
		#   - resolve signatures (FnResult/throws),
		#   - collect catch arms per function and validate them against the exception catalog,
		#   - build a concrete TypeEnv and diagnostics list.
		# The real checker will attach type_env, diagnostics, and exception_catalog.

		# Best-effort inferred throw detection: walk HIR to see if any throw or
		# call to a throwing function exists. This is deliberately shallow and
		# treats any throw/call as making the function may-throw; context (try
		# coverage) is not considered in this stub.
		for fn_name, hir_block in self._hir_blocks.items():
			info = fn_infos.get(fn_name)
			if info is None:
				continue
			may_throw, throw_span = self._function_may_throw(hir_block, fn_infos)
			if may_throw:
				info.inferred_may_throw = True
				if not info.declared_can_throw:
					diagnostics.append(
						Diagnostic(
							message=(
								f"function {fn_name} may throw but is not declared can-throw; "
								f"declare a FnResult return type or mark it can-throw explicitly"
							),
							severity="error",
							span=throw_span,
						)
					)

		# Best-effort call arity/type checks based on FnSignature TypeIds. This
		# only visits HCall with a plain HVar callee; arg types are unknown here
		# so only arity is enforced.
		for fn_name, hir_block in self._hir_blocks.items():
			info = fn_infos.get(fn_name)
			if info is None:
				continue
			self._validate_calls(hir_block, fn_infos, diagnostics, current_fn=info)

		# Array/boolean checks share a typing context per function to keep locals
		# consistent across validations.
		for fn_name, hir_block in self._hir_blocks.items():
			info = fn_infos.get(fn_name)
			if info is None:
				continue
			# Each function gets its own typing context so locals/diagnostics are
			# isolated while reusing the shared traversal logic.
			ctx = self._TypingContext(
				checker=self,
				table=self._type_table,
				fn_infos=fn_infos,
				current_fn=info,
				locals={},
				diagnostics=diagnostics,
			)
			self._seed_locals_from_signature(ctx)

			def combined_on_expr(expr: "H.HExpr", typing_ctx: Checker._TypingContext = ctx) -> None:
				self._array_validator_on_expr(expr, typing_ctx)
				self._void_usage_on_expr(expr, typing_ctx)

			def combined_on_stmt(stmt: "H.HStmt", typing_ctx: Checker._TypingContext = ctx) -> None:
				self._bool_validator_on_stmt(stmt, typing_ctx)
				self._void_rules_on_stmt(stmt, typing_ctx)
				self._exception_init_rules_on_stmt(stmt, typing_ctx)

			self._walk_hir(hir_block, ctx, on_expr=combined_on_expr, on_stmt=combined_on_stmt)

		return CheckedProgram(
			fn_infos=fn_infos,
			type_table=self._type_table,
			type_env=None,
			exception_catalog=self._exception_catalog,
			diagnostics=diagnostics,
		)

	def _call_may_throw(self, callee_name: str, fn_infos: Mapping[str, FnInfo]) -> bool:
		"""Determine if a call to `callee_name` may throw, based on FnInfo."""
		info = fn_infos.get(callee_name)
		if info is None:
			return False
		# Prefer explicit declared_can_throw; fall back to inferred flag.
		if info.declared_can_throw:
			return True
		return info.inferred_may_throw

	def _function_may_throw(self, block: "H.HBlock", fn_infos: Mapping[str, FnInfo]) -> tuple[bool, Span]:  # type: ignore[name-defined]
		"""
		Walk a HIR block and conservatively decide if it may throw.

		Any `HThrow` or call to a function marked can-throw sets the flag. This is
		still conservative but now accounts for try/catch coverage when the thrown
		exception is an `HExceptionInit` that matches a catch arm (or a catch-all).
		"""
		from lang2.driftc import stage1 as H
		may_throw = False
		first_span: Span | None = None

		def walk_expr(expr: H.HExpr) -> None:
			nonlocal may_throw
			nonlocal first_span
			if isinstance(expr, H.HCall):
				if isinstance(expr.fn, H.HVar):
					if self._call_may_throw(expr.fn.name, fn_infos):
						may_throw = True
						if first_span is None:
							first_span = Span.from_loc(getattr(expr, "loc", None))
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HMethodCall):
				# Without method resolution, treat as non-throwing unless receiver
				# or args contain throws.
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HTryResult):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HResultOk):
				walk_expr(expr.value)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
			elif isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
			elif isinstance(expr, H.HField):
				walk_expr(expr.subject)
			elif isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
			elif isinstance(expr, H.HDVInit):
				for a in expr.args:
					walk_expr(a)
			# literals/vars are leaf nodes

		def walk_block(b: H.HBlock, caught: set[str] | None = None, catch_all: bool = False) -> None:
			nonlocal may_throw
			nonlocal first_span
			for stmt in b.statements:
				if isinstance(stmt, H.HThrow):
					# If we know this throw's event is caught locally, do not mark may_throw.
					if isinstance(stmt.value, H.HExceptionInit):
						event_fqn = stmt.value.event_fqn
						if catch_all or (caught is not None and event_fqn in caught):
							continue
					elif catch_all:
						continue
					may_throw = True
					if first_span is None:
						first_span = Span.from_loc(getattr(stmt, "loc", None))
					continue
				if isinstance(stmt, H.HTry):
					catch_all_local = any(arm.event_fqn is None for arm in stmt.catches)
					caught_events = {arm.event_fqn for arm in stmt.catches if arm.event_fqn is not None}
					walk_block(stmt.body, caught_events, catch_all_local)
					for arm in stmt.catches:
						walk_block(arm.block)
					continue
				if isinstance(stmt, H.HReturn) and stmt.value is not None:
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HLet):
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HAssign):
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HIf):
					walk_expr(stmt.cond)
					walk_block(stmt.then_block)
					if stmt.else_block:
						walk_block(stmt.else_block)
					continue
				if isinstance(stmt, H.HLoop):
					walk_block(stmt.body, caught, catch_all)
					continue
				if isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.expr)
					continue
				# other statements: continue

		walk_block(block)
		return may_throw, (first_span or Span())

	@dataclass
	class _TypingContext:
		"""Shared typing state reused across validation passes for a function."""

		checker: "Checker"
		table: TypeTable
		fn_infos: Mapping[str, FnInfo]
		current_fn: Optional[FnInfo]
		locals: Dict[str, TypeId]
		diagnostics: Optional[List[Diagnostic]]
		cache: Dict[int, Optional[TypeId]] = field(default_factory=dict)

		def infer(self, expr: "H.HExpr") -> Optional[TypeId]:
			"""
			Best-effort expression typing with shallow recursion.

			This memoizes per object-id so repeated visits (multiple validators or
			re-entrance through nested expressions) do not recompute or re-emit
			diagnostics for the same node.
			"""
			expr_id = id(expr)
			if expr_id in self.cache:
				return self.cache[expr_id]
			result = self._infer_expr_type(expr)
			self.cache[expr_id] = result
			return result

		def report_index_not_int(self) -> None:
			self._append_diag(
				Diagnostic(
					message="array index must be Int",
					severity="error",
					span=None,
				)
			)

		def report_index_subject_not_array(self) -> None:
			self._append_diag(
				Diagnostic(
					message="indexing requires an Array value",
					severity="error",
					span=None,
				)
			)

		def report_empty_array_literal(self) -> None:
			self._append_diag(
				Diagnostic(
					message="empty array literal requires explicit type",
					severity="error",
					span=None,
				)
			)

		def report_mixed_array_literal(self) -> None:
			self._append_diag(
				Diagnostic(
					message="array literal elements do not have a consistent type",
					severity="error",
					span=None,
				)
			)

		def _append_diag(self, diag: Diagnostic) -> None:
			if self.diagnostics is not None:
				self.diagnostics.append(diag)

		def _infer_expr_type(self, expr: "H.HExpr") -> Optional[TypeId]:
			"""
			Very shallow expression type inference for call-arg checking.

			Handles literals, simple calls with HVar callees (using FnSignature),
			and Result.Ok in a function declared to return FnResult. Everything
			else returns None to avoid guessing.

			Diagnostics emitted here are intentionally conservative: they only
			trigger when both sides of an operation are known (string/binop,
			bitwise ops) or when array indexing rules are clearly violated.
			"""
			from lang2.driftc import stage1 as H

			checker = self.checker
			bitwise_ops = {
				H.BinaryOp.BIT_AND,
				H.BinaryOp.BIT_OR,
				H.BinaryOp.BIT_XOR,
				H.BinaryOp.SHL,
				H.BinaryOp.SHR,
			}
			string_binops = {H.BinaryOp.ADD, H.BinaryOp.EQ}

			if isinstance(expr, H.HLiteralInt):
				return checker._int_type
			if isinstance(expr, H.HLiteralBool):
				return checker._bool_type
			if hasattr(H, "HLiteralString") and isinstance(expr, getattr(H, "HLiteralString")):
				return checker._string_type
			if isinstance(expr, H.HVar):
				if expr.name == "String.EMPTY":
					return checker._string_type
				if expr.name in self.locals:
					return self.locals[expr.name]
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
				callee = self.fn_infos.get(expr.fn.name)
				if callee is not None and callee.signature and callee.signature.return_type_id is not None:
					return callee.signature.return_type_id
				return None
			if isinstance(expr, H.HResultOk):
				# If the enclosing function has a FnResult return type, reuse it; otherwise
				# synthesize a generic FnResult<Unknown, Error>.
				if self.current_fn and self.current_fn.signature and self.current_fn.signature.return_type_id is not None:
					return self.current_fn.signature.return_type_id
				return self.table.new_fnresult(checker._unknown_type, checker._error_type)
			if isinstance(expr, H.HBinary):
				left_ty = self._infer_expr_type(expr.left)
				right_ty = self._infer_expr_type(expr.right)
				string_left = left_ty == checker._string_type
				string_right = right_ty == checker._string_type
				if string_left or string_right:
					# Only + and == are valid string binops when both operands are
					# String. Mixed known types produce a diagnostic; unknowns stay
					# silent to avoid guessing.
					if string_left and string_right and expr.op in string_binops:
						if expr.op is H.BinaryOp.ADD:
							return checker._string_type
						if expr.op is H.BinaryOp.EQ:
							return checker._bool_type
					# Only emit a diagnostic when the non-string side is known to be a different type.
					non_string_known = (string_left and right_ty is not None and right_ty != checker._string_type) or (
						string_right and left_ty is not None and left_ty != checker._string_type
					)
					if non_string_known:
						self._append_diag(
							Diagnostic(
								message="string binary ops require String operands and support only + or ==",
								severity="error",
								span=None,
							)
						)
					return None
				if expr.op in bitwise_ops:
					if left_ty == checker._uint_type and right_ty == checker._uint_type:
						return checker._uint_type
					self._append_diag(
						Diagnostic(
							message="bitwise ops require Uint operands",
							severity="error",
							span=None,
						)
					)
					return None
				if left_ty == checker._int_type and right_ty == checker._int_type:
					return checker._int_type
				return None
			if isinstance(expr, H.HUnary):
				return self._infer_expr_type(expr.expr)
			if isinstance(expr, H.HArrayLiteral):
				if not expr.elements:
					self.report_empty_array_literal()
					return None
				elem_types: list[TypeId] = []
				for el in expr.elements:
					el_ty = self._infer_expr_type(el)
					if el_ty is not None:
						elem_types.append(el_ty)
				if not elem_types:
					return None
				first = elem_types[0]
				for el_ty in elem_types[1:]:
					if el_ty != first:
						self.report_mixed_array_literal()
						return self.table.new_array(checker._unknown_type)
				return self.table.new_array(first)
			if isinstance(expr, H.HField):
				subj_ty = None
				if expr.name in ("len", "cap", "capacity"):
					subj_ty = self._infer_expr_type(expr.subject)
				if subj_ty is None:
					return None
				return checker._len_cap_result_type(subj_ty)
			if isinstance(expr, H.HIndex):
				# Special-case Error.attrs[...] to accept string keys and return DiagnosticValue.
				if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
					err_ty = self._infer_expr_type(expr.subject.subject)
					if err_ty is None:
						return None
					err_def = self.table.get(err_ty)
					if err_def.kind is TypeKind.ERROR:
						idx_ty = self._infer_expr_type(expr.index)
						if idx_ty is not None:
							idx_def = self.table.get(idx_ty)
							if idx_def.name != "String":
								self.report_index_not_int()
						return checker._dv
				subject_ty = self._infer_expr_type(expr.subject)
				idx_ty = self._infer_expr_type(expr.index)
				if idx_ty is not None and idx_ty != checker._int_type:
					self.report_index_not_int()
				if subject_ty is None:
					return None
				td = self.table.get(subject_ty)
				if td.kind is TypeKind.ARRAY and td.param_types:
					return td.param_types[0]
				self.report_index_subject_not_array()
				return None
			return None

	def _seed_locals_from_signature(self, ctx: "_TypingContext") -> None:
		"""Populate ctx.locals with parameter types when available."""
		sig = ctx.current_fn.signature if ctx and ctx.current_fn else None
		if not sig or not sig.param_type_ids or not sig.param_names:
			return
		for name, ty in zip(sig.param_names, sig.param_type_ids):
			if ty is not None:
				ctx.locals[name] = ty

	def _infer_hir_expr_type(
		self,
		expr: "H.HExpr",
		fn_infos: Mapping[str, FnInfo],
		current_fn: Optional[FnInfo],
		diagnostics: Optional[List[Diagnostic]] = None,
		locals: Optional[Dict[str, TypeId]] = None,
	) -> Optional[TypeId]:
		ctx = self._TypingContext(
			checker=self,
			table=self._type_table,
			fn_infos=fn_infos,
			current_fn=current_fn,
			locals=locals if locals is not None else {},
			diagnostics=diagnostics,
		)
		return ctx.infer(expr)

	def _validate_calls(
		self,
		block: "H.HBlock",
		fn_infos: Mapping[str, FnInfo],
		diagnostics: List[Diagnostic],
		current_fn: Optional[FnInfo] = None,
	) -> None:
		"""
		Conservatively validate calls in a HIR block using FnSignature TypeIds.

		Currently enforces arity for HCall with HVar callee and attempts basic
		param-type equality when argument types are inferable (literals, simple
		calls, Result.Ok in a FnResult-returning function). Full expression
		typing is still deferred.
		"""
		from lang2.driftc import stage1 as H

		def walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
				callee_info = fn_infos.get(expr.fn.name)
				if callee_info is None:
					return
				arg_type_ids = [self._infer_hir_expr_type(a, fn_infos, current_fn, diagnostics) for a in expr.args]
				self.check_call_signature(callee_info, arg_type_ids, diagnostics, loc=None)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HTryResult):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HResultOk):
				walk_expr(expr.value)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
			elif isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
			elif isinstance(expr, H.HField):
				walk_expr(expr.subject)
			elif isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
			elif isinstance(expr, H.HDVInit):
				for a in expr.args:
					walk_expr(a)
			# literals/vars are leaf nodes

		def walk_block(b: H.HBlock) -> None:
			for stmt in b.statements:
				if isinstance(stmt, H.HReturn) and stmt.value is not None:
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HLet):
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HAssign):
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HIf):
					walk_expr(stmt.cond)
					walk_block(stmt.then_block)
					if stmt.else_block:
						walk_block(stmt.else_block)
					continue
				if isinstance(stmt, H.HLoop):
					walk_block(stmt.body)
					continue
				if isinstance(stmt, H.HTry):
					walk_block(stmt.body)
					for arm in stmt.catches:
						walk_block(arm.block)
					continue
				if isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.expr)
					continue
				# other statements: continue

		walk_block(block)

	def check_call_signature(
		self,
		callee: FnInfo | FnSignature,
		arg_type_ids: list[Optional[TypeId]],
		diagnostics: List[Diagnostic],
		loc: Optional[Any] = None,
	) -> Optional[TypeId]:
		"""
		Best-effort call signature check using FnInfo/FnSignature + TypeIds.

		- Enforces arity when param_type_ids are available.
		- Performs simple TypeId equality checks for args when both sides are known.
		- Returns the callee return_type_id (may be None if unknown).

		Used by `_validate_calls` over HIR for shallow call checking.
		"""
		sig = callee.signature if isinstance(callee, FnInfo) else callee
		if sig.param_type_ids is None or sig.return_type_id is None:
			return sig.return_type_id

		if len(arg_type_ids) != len(sig.param_type_ids):
			diagnostics.append(
				Diagnostic(
					message=(
						f"call to {sig.name} has {len(arg_type_ids)} arguments, "
						f"expected {len(sig.param_type_ids)}"
					),
					severity="error",
					span=loc,
				)
			)
			return sig.return_type_id

		for idx, (arg_ty, param_ty) in enumerate(zip(arg_type_ids, sig.param_type_ids)):
			if arg_ty is None or param_ty is None:
				continue
			if arg_ty != param_ty:
				diagnostics.append(
					Diagnostic(
						message=(
							f"argument {idx} to {sig.name} has type {arg_ty!r}, "
							f"expected {param_ty!r}"
						),
						severity="error",
						span=loc,
					)
				)
		return sig.return_type_id

	def _is_fnresult_return(self, return_type: Any) -> bool:
		"""
		Best-effort predicate to decide if a return type resembles FnResult<_, Error>.

		Legacy heuristic used only when no resolved TypeId is available. For now we
		consider:

		* strings containing 'FnResult'
		* tuples shaped like ('FnResult', ok_ty, err_ty)
		"""
		if isinstance(return_type, str):
			return "FnResult" in return_type
		if isinstance(return_type, tuple) and return_type and return_type[0] == "FnResult":
			return True
		return False

	def _resolve_signature_types(self, sig: FnSignature) -> tuple[Optional[TypeId], Optional[TypeId]]:
		"""
		Naively map a signature's return type into TypeIds using the TypeTable.

		This is a stopgap until real type resolution exists; it recognizes:
		- strings containing 'FnResult' -> FnResult<Int, Error>
		- tuple ('FnResult', ok, err) -> FnResult of naive ok/err mapping
		- strings 'Int'/'Bool' -> scalar types
		- fallback: Unknown
		"""
		rt = sig.return_type
		if isinstance(rt, str):
			if "FnResult" in rt:
				return self._type_table.new_fnresult(self._int_type, self._error_type), self._error_type
			if rt == "Int":
				return self._int_type, None
			if rt == "Bool":
				return self._bool_type, None
			# Unknown string maps to a scalar placeholder
			return self._type_table.new_scalar(rt), None
		if isinstance(rt, tuple):
			if len(rt) == 3 and rt[0] == "FnResult":
				ok = self._map_opaque(rt[1])
				err = self._map_opaque(rt[2])
				return self._type_table.new_fnresult(ok, err), err
			if len(rt) == 2:
				ok = self._map_opaque(rt[0])
				err = self._map_opaque(rt[1])
				return self._type_table.new_fnresult(ok, err), err
		# Fallback unknown
		return self._type_table.new_unknown("UnknownReturn"), None

	def _resolve_param_types(self, sig: FnSignature) -> Optional[list[TypeId]]:
		"""
		Map raw param type shapes (strings/tuples) to TypeIds using the TypeTable.

		Returns None if the signature did not supply param types; otherwise returns
		a list of TypeIds (one per param).
		"""
		if sig.param_types is None:
			return None
		resolved: list[TypeId] = []
		for p in sig.param_types:
			resolved.append(self._map_opaque(p))
		return resolved

	def _map_opaque(self, val: Any) -> TypeId:
		"""
		Naively map an opaque return component into a TypeId.

		This delegates builtin mapping to the shared resolver to keep behavior
		consistent across the compiler. The only local heuristic we retain is
		treating any string containing "Error" as the builtin error type, which
		matches legacy test fixtures.
		"""
		if isinstance(val, str) and "Error" in val:
			return self._error_type
		return resolve_opaque_type(val, self._type_table)

	def _resolve_typeexpr(self, raw: object) -> TypeId:
		"""
		Map a parser TypeExpr-like object (name/args) or simple string/tuple into a
		TypeId using the shared TypeTable. This mirrors the resolver and is used for
		declared local types. The len/cap rule (Array/String → Uint) is centralized
		in the type resolver; this helper simply resolves declared type names.
		"""
		return resolve_opaque_type(raw, self._type_table)

	def _len_cap_result_type(self, subj_ty: TypeId) -> Optional[TypeId]:
		"""Return Uint when length/capacity is requested on Array or String; otherwise None."""
		td = self._type_table.get(subj_ty)
		if td.kind is TypeKind.ARRAY or (td.kind is TypeKind.SCALAR and td.name == "String"):
			return self._type_table.ensure_uint()
		return None

	def _validate_try_results(
		self,
		fn_name: str,
		block: "H.HBlock",
		diagnostics: List[Diagnostic],
	) -> None:
		"""
		Walk a HIR block and require that every HTryResult operand is known to be a
		FnResult-returning expression based on signatures.

		This is deliberately shallow for now: it accepts HCall/HMethodCall whose
		target signature return_type_id is FnResult; everything else is flagged so
		that try-sugar cannot wrap non-FnResult values.
		"""
		from lang2.driftc import stage1 as H

		def report(msg: str) -> None:
			diagnostics.append(Diagnostic(message=msg, severity="error", span=None))

		def is_fnresult_sig(sig: FnSignature | None) -> bool:
			if sig is None or sig.return_type_id is None:
				return False
			td = self._type_table.get(sig.return_type_id)
			return td.kind is TypeKind.FNRESULT

		def validate_try_expr(expr: H.HExpr, span_descr: str) -> None:
			# Only accept simple calls/method calls to signatures we know are FnResult.
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
				cands = self._sigs_by_display_name.get(expr.fn.name, [])
				if len(cands) == 1 and is_fnresult_sig(cands[0]):
					return
			if isinstance(expr, H.HMethodCall):
				cands = self._sigs_by_display_name.get(expr.method_name, [])
				if len(cands) == 1 and is_fnresult_sig(cands[0]):
					return
			report(
				msg=(
					f"function {fn_name} uses try-expression on a non-FnResult operand "
					f"({span_descr}); try sugar requires FnResult<_, Error>"
				)
			)

		def walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HTryResult):
				validate_try_expr(expr.expr, span_descr="try operand")
				walk_expr(expr.expr)
			elif isinstance(expr, H.HCall):
				walk_expr(expr.fn)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
			elif isinstance(expr, H.HUnary):
				# HUnary stores its operand in `expr`, not `operand`.
				walk_expr(expr.expr)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
			# Literals/vars need no action.

		def walk_block(hb: H.HBlock) -> None:
			for stmt in hb.statements:
				if isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.expr)
				elif isinstance(stmt, H.HLet):
					walk_expr(stmt.value)
				elif isinstance(stmt, H.HAssign):
					walk_expr(stmt.value)
				elif isinstance(stmt, H.HIf):
					walk_expr(stmt.cond)
					walk_block(stmt.then_block)
					if stmt.else_block is not None:
						walk_block(stmt.else_block)
				elif isinstance(stmt, H.HLoop):
					walk_block(stmt.body)
				elif isinstance(stmt, H.HReturn):
					if stmt.value is not None:
						walk_expr(stmt.value)
				elif isinstance(stmt, H.HThrow):
					walk_expr(stmt.value)
				elif isinstance(stmt, H.HTry):
					walk_block(stmt.body)
					for arm in stmt.catches:
						walk_block(arm.block)
				# HBreak/HContinue carry no expressions.

		walk_block(block)

	def _walk_hir(
		self,
		block: "H.HBlock",
		ctx: "_TypingContext",
		on_expr: Optional[Callable[["H.HExpr", "_TypingContext"], None]] = None,
		on_stmt: Optional[Callable[["H.HStmt", "_TypingContext"], None]] = None,
	) -> None:
		"""
		Walk a HIR block, invoking callbacks and maintaining locals.

		This is the shared traversal used by all validators so that locals
		mutations (let/assign) are centralized and every validation sees a
		consistent environment.
		"""
		from lang2.driftc import stage1 as H

		def walk_expr(expr: H.HExpr) -> None:
			# Run inference for all expressions up front so shared diagnostics
			# (string/binop, bitwise, indexing, etc.) fire even when no specific
			# validator hook is registered for that node.
			ctx.infer(expr)
			if on_expr:
				on_expr(expr, ctx)
			if isinstance(expr, H.HCall):
				walk_expr(expr.fn)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
			elif isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
			elif isinstance(expr, H.HTryResult):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HResultOk):
				walk_expr(expr.value)
			elif isinstance(expr, H.HField):
				walk_expr(expr.subject)
			elif isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
			elif isinstance(expr, H.HArrayLiteral):
				for el in expr.elements:
					walk_expr(el)
			elif isinstance(expr, H.HDVInit):
				for a in expr.args:
					walk_expr(a)
			# literals/vars are leaf nodes

		def walk_stmt(stmt: H.HStmt) -> None:
			if on_stmt:
				on_stmt(stmt, ctx)
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
			elif isinstance(stmt, H.HLet):
				walk_expr(stmt.value)
				decl_ty: Optional[TypeId] = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					decl_ty = self._resolve_typeexpr(stmt.declared_type_expr)
				value_ty = ctx.infer(stmt.value)
				# Let-binding type consistency lives here so every validator shares
				# the same rule and locals update.
				if decl_ty is not None and value_ty is not None and decl_ty != value_ty:
					ctx._append_diag(
						Diagnostic(
							message="let-binding type does not match declared type",
							severity="error",
							span=None,
						)
					)
				ctx.locals[stmt.name] = decl_ty or value_ty or self._unknown_type
			elif isinstance(stmt, H.HAssign):
				walk_expr(stmt.value)
				value_ty = ctx.infer(stmt.value)
				walk_expr(stmt.target)
				if isinstance(stmt.target, H.HIndex):
					target_ty = ctx.infer(stmt.target)
					if target_ty is not None and value_ty is not None and target_ty != value_ty:
						ctx._append_diag(
							Diagnostic(
								message="assignment type mismatch for indexed array element",
								severity="error",
								span=None,
							)
						)
				elif isinstance(stmt.target, H.HVar) and value_ty is not None:
					# Simple var assignment updates locals so downstream expressions
					# see the new type.
					ctx.locals[stmt.target.name] = value_ty
			elif isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block:
					walk_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				walk_block(stmt.body)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					walk_expr(stmt.value)
			elif isinstance(stmt, H.HThrow):
				walk_expr(stmt.value)
			elif isinstance(stmt, H.HTry):
				walk_block(stmt.body)
				for arm in stmt.catches:
					walk_block(arm.block)
			# HBreak/HContinue carry no expressions.

		def walk_block(hb: H.HBlock) -> None:
			for stmt in hb.statements:
				walk_stmt(stmt)

		walk_block(block)

	def _array_validator_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""Trigger array literal/index inference to surface diagnostics."""
		from lang2.driftc import stage1 as H

		if isinstance(expr, (H.HArrayLiteral, H.HIndex)):
			# Reuse shared infer to emit array-specific diagnostics without extra
			# traversal logic here.
			ctx.infer(expr)

	def _void_usage_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""
		Forbid Void where a value is required (binary ops, call args, unary ops).

		Expression statements are allowed to discard Void-returning calls, so the
		expr-stmt case is handled in the stmt validator.
		"""
		from lang2.driftc import stage1 as H

		def is_void(tid: TypeId | None) -> bool:
			return tid is not None and self._type_table.is_void(tid)

		if isinstance(expr, H.HBinary):
			left_ty = ctx.infer(expr.left)
			right_ty = ctx.infer(expr.right)
			if is_void(left_ty) or is_void(right_ty):
				ctx._append_diag(
					Diagnostic(message="Void value is not allowed in a binary operation", severity="error", span=None)
				)
		elif isinstance(expr, H.HUnary):
			sub_ty = ctx.infer(expr.expr)
			if is_void(sub_ty):
				ctx._append_diag(
					Diagnostic(message="Void value is not allowed in a unary operation", severity="error", span=None)
				)
		elif isinstance(expr, H.HTernary):
			then_ty = ctx.infer(expr.then_expr)
			else_ty = ctx.infer(expr.else_expr)
			if is_void(then_ty) or is_void(else_ty):
				ctx._append_diag(
					Diagnostic(message="Void value is not allowed in a ternary expression", severity="error", span=None)
				)
		elif isinstance(expr, H.HCall):
			for arg in expr.args:
				arg_ty = ctx.infer(arg)
				if is_void(arg_ty):
					ctx._append_diag(
						Diagnostic(message="Void value is not allowed as a function argument", severity="error", span=None)
					)
		elif isinstance(expr, H.HMethodCall):
			for arg in expr.args:
				arg_ty = ctx.infer(arg)
				if is_void(arg_ty):
					ctx._append_diag(
						Diagnostic(message="Void value is not allowed as a method argument", severity="error", span=None)
					)
		elif isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				el_ty = ctx.infer(el)
				if is_void(el_ty):
					ctx._append_diag(
						Diagnostic(message="Void value is not allowed in an array literal", severity="error", span=None)
					)
					break

	def _bool_validator_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""Require Boolean conditions when the type is known."""
		from lang2.driftc import stage1 as H

		if isinstance(stmt, H.HIf):
			cond_ty = ctx.infer(stmt.cond)
			if cond_ty is not None and cond_ty != self._bool_type:
				ctx._append_diag(
					Diagnostic(
						message="if condition must be Bool",
						severity="error",
						span=None,
					)
				)

	def _void_rules_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""
		Enforce Void placement rules on statements:
		- Void return type: only bare `return` allowed.
		- Non-void return type: must return a value.
		- Void cannot be stored in let/assign or declared explicitly.
		"""
		from lang2.driftc import stage1 as H

		def is_void(tid: TypeId | None) -> bool:
			return tid is not None and self._type_table.is_void(tid)

		if isinstance(stmt, H.HReturn):
			ret_tid = None
			if ctx.current_fn and ctx.current_fn.signature:
				ret_tid = ctx.current_fn.signature.return_type_id
			fn_is_void = is_void(ret_tid)
			if stmt.value is None and ret_tid is not None and not fn_is_void:
				ctx._append_diag(
					Diagnostic(message="non-void function must return a value", severity="error", span=None)
				)
			if stmt.value is not None:
				if fn_is_void:
					ctx._append_diag(
						Diagnostic(message="cannot return a value from a Void function", severity="error", span=None)
					)
			return

		if isinstance(stmt, H.HLet):
			decl_ty = None
			if getattr(stmt, "declared_type_expr", None) is not None:
				decl_ty = self._resolve_typeexpr(stmt.declared_type_expr)
			if is_void(decl_ty):
				ctx._append_diag(
					Diagnostic(message="cannot declare a binding of type Void", severity="error", span=None)
				)
			val_ty = ctx.infer(stmt.value)
			if is_void(val_ty):
				ctx._append_diag(
					Diagnostic(message="cannot bind a Void value", severity="error", span=None)
				)
			return

		if isinstance(stmt, H.HAssign):
			val_ty = ctx.infer(stmt.value)
			if is_void(val_ty):
				ctx._append_diag(
					Diagnostic(message="cannot assign a Void value", severity="error", span=None)
				)

	def _exception_init_rules_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""
		Enforce exception field/braces rules:
		- Zero-field exceptions: allow `throw E` and `throw E {}` but reject provided fields.
		- Non-empty exceptions: require braces and exact field set.
		"""
		from lang2.driftc import stage1 as H

		schemas: dict[str, tuple[str, list[str]]] = getattr(self._type_table, "exception_schemas", {})

		def _schema(name: str) -> tuple[str, list[str]] | None:
			return schemas.get(name)

		if not isinstance(stmt, H.HThrow):
			return

		# Exception init with braces present.
		if isinstance(stmt.value, H.HExceptionInit):
			schema = _schema(stmt.value.event_fqn)
			if schema is None:
				return
			_decl_fqn, decl_fields = schema
			provided = list(stmt.value.field_names)
			if not decl_fields:
				if provided:
					ctx._append_diag(
						Diagnostic(
							message=f"exception '{stmt.value.event_fqn}' declares no fields; cannot provide initializer fields",
							severity="error",
							span=getattr(stmt.value, "loc", Span()),
						)
					)
				return
			decl_set = set(decl_fields)
			provided_set = set(provided)
			unknown = provided_set - decl_set
			missing = decl_set - provided_set
			if unknown:
				ctx._append_diag(
					Diagnostic(
						message=f"unknown exception field(s): {', '.join(sorted(unknown))}",
						severity="error",
						span=getattr(stmt.value, "loc", Span()),
					)
				)
			if missing:
				ctx._append_diag(
					Diagnostic(
						message=f"missing exception field(s): {', '.join(sorted(missing))}",
						severity="error",
						span=getattr(stmt.value, "loc", Span()),
					)
				)
			return

		# Bare throw of an exception name without braces.
		if isinstance(stmt.value, H.HVar):
			schema = _schema(stmt.value.name)
			if schema is None:
				return
			_decl_fqn, decl_fields = schema
			if decl_fields:
				ctx._append_diag(
					Diagnostic(
						message=f"exception '{stmt.value.name}' requires fields; use `throw {stmt.value.name} {{ ... }}`",
						severity="error",
						span=getattr(stmt.value, "loc", Span()),
					)
				)
			return

	def _validate_array_exprs(self, block: "H.HBlock", ctx: "_TypingContext") -> None:
		"""Validate array literals/indexing/assignments over a HIR block."""
		self._walk_hir(block, ctx, on_expr=self._array_validator_on_expr)

	def _validate_bool_conditions(self, block: "H.HBlock", ctx: "_TypingContext") -> None:
		"""Require Boolean conditions for if/loop when types are known."""
		self._walk_hir(block, ctx, on_stmt=self._bool_validator_on_stmt)

	def build_type_env_from_ssa(
		self,
		ssa_funcs: Mapping[str, "SsaFunc"],
		signatures: Mapping[str, FnSignature],
	) -> Optional["CheckerTypeEnv"]:
		"""
		Assign TypeIds to SSA values using checker signatures and simple heuristics.

		This is a minimal pass: it handles constants, ConstructResultOk/Err, Call/
		MethodCall, AssignSSA copies, UnaryOp/BinaryOp propagation, and Phi when
		incoming types agree. Unknowns default to `Unknown` TypeId. Returns None if
		no types were assigned.
		"""
		from lang2.driftc.checker.type_env_impl import CheckerTypeEnv
		from lang2.driftc.stage2 import (
			ConstructResultOk,
			ConstructResultErr,
			Call,
			MethodCall,
			ConstInt,
			ConstBool,
			ConstString,
			ConstructError,
			AssignSSA,
			Phi,
			UnaryOpInstr,
			BinaryOpInstr,
			ArrayLit,
			ArrayIndexLoad,
			ArrayLen,
			ArrayCap,
			StringLen,
		)
		signatures_by_display_name = self._sigs_by_display_name
		value_types: Dict[tuple[str, str], TypeId] = {}

		# Helper to fetch a mapped type with Unknown fallback.
		def ty_for(fn: str, val: str) -> TypeId:
			return value_types.get((fn, val), self._unknown_type)

		def is_void_tid(tid: TypeId | None) -> bool:
			if tid is None:
				return False
			try:
				return self._type_table.is_void(tid)
			except KeyError:
				return False

		# Seed parameter types from signatures when available so callers and returns
		# see concrete types for params immediately.
		for fn_name, ssa in ssa_funcs.items():
			sig = signatures.get(fn_name)
			if sig and sig.param_type_ids and ssa.func.params:
				for param_name, ty_id in zip(ssa.func.params, sig.param_type_ids):
					if ty_id is not None:
						value_types[(fn_name, param_name)] = ty_id

		changed = True
		# Fixed-point with a small iteration cap.
		for _ in range(5):
			if not changed:
				break
			changed = False
			for fn_name, ssa in ssa_funcs.items():
				sig = signatures.get(fn_name)
				fn_return_parts: tuple[TypeId, TypeId] | None = None
				fn_is_void = False
				if sig and sig.return_type_id is not None:
					td = self._type_table.get(sig.return_type_id)
					if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
						fn_return_parts = (td.param_types[0], td.param_types[1])
					fn_is_void = self._type_table.is_void(sig.return_type_id)

				for block in ssa.func.blocks.values():
					for instr in block.instructions:
						dest = getattr(instr, "dest", None)
						if isinstance(instr, ConstInt) and dest is not None:
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._int_type
								changed = True
						elif isinstance(instr, ConstBool) and dest is not None:
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._bool_type
								changed = True
						elif isinstance(instr, ConstString) and dest is not None:
							if value_types.get((fn_name, dest)) != self._string_type:
								value_types[(fn_name, dest)] = self._string_type
								changed = True
						elif isinstance(instr, StringLen) and dest is not None:
							if value_types.get((fn_name, dest)) != self._uint_type:
								value_types[(fn_name, dest)] = self._uint_type
								changed = True
						elif isinstance(instr, ArrayLen) and dest is not None:
							if value_types.get((fn_name, dest)) != self._uint_type:
								value_types[(fn_name, dest)] = self._uint_type
								changed = True
						elif isinstance(instr, ArrayCap) and dest is not None:
							if value_types.get((fn_name, dest)) != self._uint_type:
								value_types[(fn_name, dest)] = self._uint_type
								changed = True
						elif isinstance(instr, ArrayIndexLoad) and dest is not None:
							if instr.elem_ty is not None and value_types.get((fn_name, dest)) != instr.elem_ty:
								value_types[(fn_name, dest)] = instr.elem_ty
								changed = True
						elif isinstance(instr, ArrayLit) and dest is not None:
							arr_ty = self._type_table.new_array(instr.elem_ty)
							if value_types.get((fn_name, dest)) != arr_ty:
								value_types[(fn_name, dest)] = arr_ty
								changed = True
						elif isinstance(instr, ConstructResultOk):
							if dest is None:
								continue
							ok_ty = ty_for(fn_name, instr.value)
							err_ty = fn_return_parts[1] if fn_return_parts else self._error_type
							dest_ty = self._type_table.new_fnresult(ok_ty, err_ty)
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ConstructResultErr):
							if dest is None:
								continue
							err_ty = ty_for(fn_name, instr.error)
							ok_ty = fn_return_parts[0] if fn_return_parts else self._unknown_type
							dest_ty = self._type_table.new_fnresult(ok_ty, err_ty)
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ConstructError) and dest is not None:
							if value_types.get((fn_name, dest)) != self._error_type:
								value_types[(fn_name, dest)] = self._error_type
								changed = True
						elif isinstance(instr, Call) and dest is not None:
							callee_sig = signatures.get(instr.fn)
							if callee_sig is not None:
								if callee_sig.return_type_id is None:
									rt_id, err_id = self._resolve_signature_types(callee_sig)
									callee_sig.return_type_id = rt_id
									callee_sig.error_type_id = err_id
								dest_ty = callee_sig.return_type_id or self._unknown_type
								if is_void_tid(dest_ty):
									continue
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, MethodCall) and dest is not None:
							# Intrinsic methods on FnResult: unwrap/unwrap_err/is_err
							recv_ty = value_types.get((fn_name, instr.receiver))
							if recv_ty is not None and self._type_table.get(recv_ty).kind is TypeKind.FNRESULT:
								ok_ty, err_ty = self._type_table.get(recv_ty).param_types
								if instr.method_name == "unwrap":
									dest_ty = ok_ty
								elif instr.method_name == "unwrap_err":
									dest_ty = err_ty
								elif instr.method_name == "is_err":
									dest_ty = self._bool_type
								else:
									dest_ty = self._unknown_type
							else:
								cands = signatures_by_display_name.get(instr.method_name, [])
								callee_sig = cands[0] if len(cands) == 1 else None
								if callee_sig is not None:
									if callee_sig.return_type_id is None:
										rt_id, err_id = self._resolve_signature_types(callee_sig)
										callee_sig.return_type_id = rt_id
										callee_sig.error_type_id = err_id
									dest_ty = callee_sig.return_type_id or self._unknown_type
									if is_void_tid(dest_ty):
										continue
								else:
									dest_ty = self._unknown_type
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, AssignSSA):
							if dest is None:
								continue
							src_ty = value_types.get((fn_name, instr.src))
							if src_ty is not None and value_types.get((fn_name, dest)) != src_ty:
								value_types[(fn_name, dest)] = src_ty
								changed = True
						elif isinstance(instr, UnaryOpInstr):
							if dest is None:
								continue
							operand_ty = ty_for(fn_name, instr.operand)
							if value_types.get((fn_name, dest)) != operand_ty:
								value_types[(fn_name, dest)] = operand_ty
								changed = True
						elif isinstance(instr, BinaryOpInstr):
							if dest is None:
								continue
							left_ty = ty_for(fn_name, instr.left)
							right_ty = ty_for(fn_name, instr.right)
							# If both operands agree, propagate that type; otherwise fall back to Unknown.
							dest_ty = left_ty if left_ty == right_ty else self._unknown_type
							if value_types.get((fn_name, dest)) != dest_ty:
								value_types[(fn_name, dest)] = dest_ty
								changed = True
						elif isinstance(instr, Phi):
							if dest is None:
								continue
							incoming = [value_types.get((fn_name, v)) for v in instr.incoming.values()]
							incoming = [t for t in incoming if t is not None]
							if incoming and all(t == incoming[0] for t in incoming):
								ty = incoming[0]
								if value_types.get((fn_name, dest)) != ty:
									value_types[(fn_name, dest)] = ty
									changed = True

					term = block.terminator
					if hasattr(term, "value") and getattr(term, "value") is not None:
						val = term.value
						# Do not overwrite an existing concrete type; only seed a type for
						# returns that have not been seen yet.
						if (fn_name, val) not in value_types and not fn_is_void:
							if fn_return_parts is not None:
								ty = self._type_table.new_fnresult(fn_return_parts[0], fn_return_parts[1])
							else:
								ty = self._unknown_type
							value_types[(fn_name, val)] = ty
							changed = True

		if not value_types:
			return None
		return CheckerTypeEnv(self._type_table, value_types)
