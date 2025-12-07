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
from typing import Any, Dict, Iterable, List, Optional, FrozenSet, Mapping, Sequence, Set, Tuple, TYPE_CHECKING

from lang2.core.diagnostics import Diagnostic
from lang2.core.types_protocol import TypeEnv
from lang2.checker.catch_arms import CatchArmInfo, validate_catch_arms
from lang2.core.types_core import TypeTable, TypeId, TypeKind

if TYPE_CHECKING:
	from lang2 import stage1 as H


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

	# Canonical, type-checked fields (preferred).
	param_type_ids: Optional[list[TypeId]] = None
	return_type_id: Optional[TypeId] = None
	declared_can_throw: Optional[bool] = None
	is_extern: bool = False
	is_intrinsic: bool = False
	error_type_id: Optional[TypeId] = None  # resolved error TypeId

	# Legacy/raw fields (to be removed once real type checker is wired).
	return_type: Any = None
	throws_events: Tuple[str, ...] = ()
	param_types: Optional[list[Any]] = None  # raw param type shapes (strings/tuples)


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
		catch_arms: Mapping[str, Sequence[CatchArmInfo]] | None = None,
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
		self._catch_arms = catch_arms or {}
		self._exception_catalog = dict(exception_catalog) if exception_catalog else None
		self._hir_blocks = hir_blocks or {}
		# Use shared TypeTable when supplied; otherwise create a local one.
		self._type_table = type_table or TypeTable()
		# Seed common scalars only when creating a fresh table.
		self._int_type = getattr(self._type_table, "_int_type", None) or self._type_table.new_scalar("Int")
		self._bool_type = getattr(self._type_table, "_bool_type", None) or self._type_table.new_scalar("Bool")
		self._error_type = getattr(self._type_table, "_error_type", None) or self._type_table.new_error("Error")
		self._unknown_type = getattr(self._type_table, "_unknown_type", None) or self._type_table.new_unknown("Unknown")
		# Cache seeds on the table so downstream reuse sees the same ids.
		self._type_table._int_type = self._int_type  # type: ignore[attr-defined]
		self._type_table._bool_type = self._bool_type  # type: ignore[attr-defined]
		self._type_table._error_type = self._error_type  # type: ignore[attr-defined]
		self._type_table._unknown_type = self._unknown_type  # type: ignore[attr-defined]
		# TODO: remove declared_can_throw shim once real parser/type checker supplies signatures.

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
				if sig.return_type_id is None or sig.error_type_id is None:
					return_type_id, error_type_id = self._resolve_signature_types(sig)
					sig.return_type_id = return_type_id
					sig.error_type_id = error_type_id
				else:
					return_type_id = sig.return_type_id
					error_type_id = sig.error_type_id

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

			catch_arms = self._catch_arms.get(name)
			if catch_arms is not None:
				validate_catch_arms(catch_arms, known_events, diagnostics)

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
			if self._function_may_throw(hir_block, fn_infos):
				info.inferred_may_throw = True
				if not info.declared_can_throw:
					diagnostics.append(
						Diagnostic(
							message=f"function {fn_name} may throw but is not declared throws",
							severity="error",
							span=None,
						)
					)

		# Best-effort call arity/type checks based on FnSignature TypeIds. This
		# only visits HCall with a plain HVar callee; arg types are unknown here
		# so only arity is enforced.
		for fn_name, hir_block in self._hir_blocks.items():
			info = fn_infos.get(fn_name)
			if info is None:
				continue
			self._validate_calls(hir_block, fn_infos, diagnostics)

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

	def _function_may_throw(self, block: "H.HBlock", fn_infos: Mapping[str, FnInfo]) -> bool:  # type: ignore[name-defined]
		"""
		Walk a HIR block and conservatively decide if it may throw.

		Any `HThrow` or call to a function marked can-throw sets the flag. This is
		context-insensitive: try/catch coverage is ignored in this stub.
		"""
		from lang2 import stage1 as H

		may_throw = False

		def walk_expr(expr: H.HExpr) -> None:
			nonlocal may_throw
			if isinstance(expr, H.HCall):
				if isinstance(expr.fn, H.HVar):
					if self._call_may_throw(expr.fn.name, fn_infos):
						may_throw = True
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

		def walk_block(b: H.HBlock) -> None:
			nonlocal may_throw
			for stmt in b.statements:
				if isinstance(stmt, H.HThrow):
					may_throw = True
					continue
				if isinstance(stmt, H.HTry):
					walk_block(stmt.body)
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
					walk_block(stmt.body)
					continue
				if isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.value)
					continue
				# other statements: continue

		walk_block(block)
		return may_throw

	def _validate_calls(
		self,
		block: "H.HBlock",
		fn_infos: Mapping[str, FnInfo],
		diagnostics: List[Diagnostic],
	) -> None:
		"""
		Conservatively validate calls in a HIR block using FnSignature TypeIds.

		Currently enforces arity for HCall with HVar callee; type checking of
		arguments is deferred until expression typing is available.
		"""
		from lang2 import stage1 as H

		def walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
				callee_info = fn_infos.get(expr.fn.name)
				if callee_info is None:
					return
				# Arg types unknown here -> arity check only.
				self.check_call_signature(callee_info, [None] * len(expr.args), diagnostics, loc=None)
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
					walk_expr(stmt.value)
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

		This is a helper for future call/type-checking passes; it is not invoked
		in the current stub pipeline.
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
		"""Naively map an opaque return component into a TypeId."""
		if isinstance(val, str):
			if val == "Int":
				return self._int_type
			if val == "Bool":
				return self._bool_type
			if "Error" in val:
				return self._error_type
			return self._type_table.new_scalar(val)
		if isinstance(val, tuple):
			if len(val) == 2:
				ok = self._map_opaque(val[0])
				err = self._map_opaque(val[1])
				return self._type_table.new_fnresult(ok, err)
			if len(val) >= 3 and val[0] == "FnResult":
				ok = self._map_opaque(val[1])
				err = self._map_opaque(val[2])
				return self._type_table.new_fnresult(ok, err)
		return self._type_table.new_unknown(str(val))

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
		from lang2 import stage1 as H

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
				sig = self._signatures.get(expr.fn.name)
				if is_fnresult_sig(sig):
					return
			if isinstance(expr, H.HMethodCall):
				sig = self._signatures.get(expr.method_name)
				if is_fnresult_sig(sig):
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
					walk_block(stmt.block)
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
		from lang2.checker.type_env_impl import CheckerTypeEnv
		from lang2.stage2 import (
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
		)
		value_types: Dict[tuple[str, str], TypeId] = {}

		# Helper to fetch a mapped type with Unknown fallback.
		def ty_for(fn: str, val: str) -> TypeId:
			return value_types.get((fn, val), self._unknown_type)

		changed = True
		# Fixed-point with a small iteration cap.
		for _ in range(5):
			if not changed:
				break
			changed = False
			for fn_name, ssa in ssa_funcs.items():
				sig = signatures.get(fn_name)
				fn_return_parts: tuple[TypeId, TypeId] | None = None
				if sig and sig.return_type_id is not None:
					td = self._type_table.get(sig.return_type_id)
					if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
						fn_return_parts = (td.param_types[0], td.param_types[1])

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
							if (fn_name, dest) not in value_types:
								value_types[(fn_name, dest)] = self._type_table.new_scalar("String")
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
								callee_sig = signatures.get(instr.method_name)
								if callee_sig is not None:
									if callee_sig.return_type_id is None:
										rt_id, err_id = self._resolve_signature_types(callee_sig)
										callee_sig.return_type_id = rt_id
										callee_sig.error_type_id = err_id
									dest_ty = callee_sig.return_type_id or self._unknown_type
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
						if (fn_name, val) not in value_types:
							if fn_return_parts is not None:
								ty = self._type_table.new_fnresult(fn_return_parts[0], fn_return_parts[1])
							else:
								ty = self._unknown_type
							value_types[(fn_name, val)] = ty
							changed = True

		if not value_types:
			return None
		return CheckerTypeEnv(self._type_table, value_types)
