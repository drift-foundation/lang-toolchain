# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-04
"""
Minimal checker stub for lang.

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
import sys
from typing import Any, Dict, Iterable, List, Optional, Callable, FrozenSet, Mapping, Sequence, Set, Tuple, TYPE_CHECKING

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc import debug as drift_debug

# Checker diagnostics should always carry phase.
def _chk_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)

from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.span import Span
from lang.driftc.core.types_protocol import TypeEnv
from lang.driftc.checker.catch_arms import CatchArmInfo, validate_catch_arms
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.types_core import TypeTable, TypeId, TypeKind, TypeParamId
from lang.driftc.stage1.hir_utils import collect_catch_arms_from_block
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind, IntrinsicKind
from lang.driftc.call_contract import call_arg_exprs_for_param_layout, call_contract_issues, repair_named_hcall_callinfo
from lang.driftc.stage1.normalize import normalize_hir

if TYPE_CHECKING:
	from lang.driftc import stage1 as H


from lang.driftc.core.types_core import UintConst as _UintConst, Uint64Const as _Uint64Const, validate_const_value as _validate_const

def _eval_hir_const_value(expr: "H.HExpr") -> object | None:
	"""Extract a compile-time literal value from an HIR expression, or None.

	Unary '+' is elided during AST→HIR lowering, so only NEG needs handling.
	"""
	from lang.driftc import stage1 as H
	if isinstance(expr, H.HLiteralInt):
		return expr.value
	if isinstance(expr, H.HLiteralFloat):
		return expr.value
	if isinstance(expr, H.HLiteralBool):
		return expr.value
	if isinstance(expr, H.HLiteralString):
		return expr.value
	if hasattr(H, "HLiteralUint") and isinstance(expr, getattr(H, "HLiteralUint")):
		return _UintConst(expr.value)
	if hasattr(H, "HLiteralUint64") and isinstance(expr, getattr(H, "HLiteralUint64")):
		return _Uint64Const(expr.value)
	if isinstance(expr, H.HUnary) and expr.op == H.UnaryOp.NEG:
		inner = expr.expr
		if isinstance(inner, H.HLiteralInt):
			return -inner.value
		if isinstance(inner, H.HLiteralFloat):
			return -inner.value
	if isinstance(expr, H.HArrayLiteral):
		vals = []
		for elem in expr.elements:
			v = _eval_hir_const_value(elem)
			if v is None:
				return None
			vals.append(v)
		return vals
	return None


@dataclass(frozen=True)
class TypeParam:
	"""Function type parameter descriptor."""

	id: TypeParamId
	name: str
	span: Optional[Any] = None


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
	type_params: list[TypeParam] = field(default_factory=list)
	# Canonical, type-checked fields (preferred).
	param_type_ids: Optional[list[TypeId]] = None
	return_type_id: Optional[TypeId] = None
	declared_can_throw: Optional[bool] = None
	declared_throws: bool = False
	declared_unsafe: Optional[bool] = None
	is_extern: bool = False
	is_extern_c: bool = False
	is_intrinsic: bool = False
	intrinsic_kind: IntrinsicKind | None = None
	param_names: Optional[list[str]] = None
	param_mutable: Optional[list[bool]] = None
	param_escape_level: Optional[list[Optional["EscapeLevel"]]] = None
	error_type_id: Optional[TypeId] = None  # resolved error TypeId
	# Method metadata (set when the declaration comes from an `implement Type` block).
	is_method: bool = False
	self_mode: Optional[str] = None  # "value", "ref", "ref_mut" for now; parser maps to this
	impl_target_type_id: Optional[TypeId] = None
	impl_target_type_args: Optional[list[TypeId]] = None
	impl_type_params: list[TypeParam] = field(default_factory=list)
	# Visibility marker (currently only `pub` vs private for method calls).
	is_pub: bool = False
	# Wrapper metadata (boundary Ok-wrap, not user-facing).
	is_wrapper: bool = False
	wraps_target_fn_id: Optional[FunctionId] = None
	# Instantiation marker (generic monomorphization output).
	is_instantiation: bool = False
	# MIR-bound marker (typed pipeline lowering boundary).
	is_mir_bound: bool = False

	# Legacy/raw fields (to be removed once real type checker is wired).
	return_type: Any = None
	throws_events: Tuple[str, ...] = ()
	param_types: Optional[list[Any]] = None  # raw param type shapes (strings/tuples)
	module: Optional[str] = None
	# Module interface marker (Milestone 3): exported functions form an ABI boundary.
	# This flag is set by the workspace resolver when a function name appears in
	# `export { ... }` and the symbol resolves to a value-level callable.
	is_exported_entrypoint: bool = False

	def __post_init__(self) -> None:
		if not self.is_mir_bound:
			self.is_mir_bound = bool(self.is_instantiation or (not self.type_params and not self.impl_type_params))

	def effective_param_escape_level(self, i: int) -> "EscapeLevel":
		from lang.driftc.borrow_checker import EscapeLevel
		if self.param_escape_level is not None and i < len(self.param_escape_level):
			lvl = self.param_escape_level[i]
			if lvl is not None:
				return lvl
		return EscapeLevel.THREAD


@dataclass(frozen=True)
class CheckerInputsById:
	"""
	ID-keyed inputs for the checker stub.

	This keeps FunctionId as the primary identity in the stubbed pipeline and
	allows a single adapter to produce legacy symbol-keyed maps as needed.
	"""

	hir_blocks_by_id: Mapping[FunctionId, "H.HBlock"]  # type: ignore[name-defined]
	signatures_by_id: Mapping[FunctionId, FnSignature]
	call_info_by_callsite_id: Mapping[FunctionId, Mapping[int, CallInfo]]


@dataclass
class FnInfo:
	"""
	Per-function checker metadata (placeholder).

	Only `name` and `declared_can_throw` are populated by this stub. Real
	`FnInfo` will carry richer information such as declared event set, return
	type, and source span for diagnostics. `signature` is the canonical source
	of truth for return/param types and throws flags.
	"""

	fn_id: FunctionId
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


def make_fn_info(
	fn_id: FunctionId,
	sig: FnSignature,
	*,
	declared_can_throw: bool | None = None,
) -> FnInfo:
	if declared_can_throw is None:
		declared_can_throw = True if sig.declared_can_throw is None else bool(sig.declared_can_throw)
	return FnInfo(
		fn_id=fn_id,
		name=function_symbol(fn_id),
		declared_can_throw=declared_can_throw,
		signature=sig,
	)


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


@dataclass
class CheckedProgramById:
	"""
	Checker output keyed by FunctionId.

	This is the primary output for the stubbed pipeline; symbol-keyed maps
	remain an internal implementation detail of the checker stub.
	"""

	fn_infos_by_id: Dict[FunctionId, FnInfo]
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
		declared_can_throw_by_id: Mapping[FunctionId, bool] | None = None,
		signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
		exception_catalog: Mapping[str, int] | None = None,
		hir_blocks_by_id: Mapping[FunctionId, "H.HBlock"] | None = None,  # type: ignore[name-defined]
		type_table: "TypeTable" | None = None,
		call_info_by_callsite_id: Mapping[FunctionId, Mapping[int, CallInfo]] | None = None,
	) -> None:
		declared_by_id = declared_can_throw_by_id or {}
		signatures = signatures_by_id or {}
		hir_blocks = hir_blocks_by_id or {}
		if call_info_by_callsite_id is None:
			raise AssertionError("call_info_by_callsite_id is required for Checker")
		self._declared_by_id = {fn_id: bool(val) for fn_id, val in declared_by_id.items()}
		self._signatures_by_id = {fn_id: sig for fn_id, sig in signatures.items()}
		self._hir_blocks_by_id = {fn_id: block for fn_id, block in hir_blocks.items()}
		self._call_info_by_callsite_id = {
			fn_id: dict(call_info)
			for fn_id, call_info in call_info_by_callsite_id.items()
		}
		for fn_id in self._hir_blocks_by_id.keys():
			self._call_info_by_callsite_id.setdefault(fn_id, {})
		self._catch_arms = self._normalize_and_collect_catch_arms(self._hir_blocks_by_id)
		self._exception_catalog = dict(exception_catalog) if exception_catalog else None
		# Use shared TypeTable when supplied; otherwise create a local one.
		self._type_table = type_table or TypeTable()
		self._ensure_core_types()

	def _ensure_core_types(self) -> None:
		if hasattr(self, "_int_type") and hasattr(self, "_string_type"):
			return

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
		self._float_type = _find_named(TypeKind.SCALAR, "Float") or self._type_table.ensure_float()
		self._bool_type = _find_named(TypeKind.SCALAR, "Bool") or self._type_table.ensure_bool()
		self._string_type = _find_named(TypeKind.SCALAR, "String") or self._type_table.ensure_string()
		self._uint_type = _find_named(TypeKind.SCALAR, "Uint") or self._type_table.ensure_uint()
		self._uint64_type = _find_named(TypeKind.SCALAR, "Uint64") or self._type_table.ensure_uint64()
		self._void_type = _find_named(TypeKind.VOID, "Void") or self._type_table.ensure_void()
		self._error_type = _find_named(TypeKind.ERROR, "Error") or self._type_table.ensure_error()
		self._dv = _find_named(TypeKind.DIAGNOSTICVALUE, "DiagnosticValue") or self._type_table.ensure_diagnostic_value()
		self._unknown_type = _find_named(TypeKind.UNKNOWN, "Unknown") or self._type_table.ensure_unknown()

	def _type_label(self, ty: Optional[TypeId]) -> str:
		if ty is None:
			return "Unknown"
		try:
			return self._type_table.type_key_string(ty)
		except Exception:
			return repr(ty)

	@classmethod
	def run_by_id(
		cls,
		inputs: CheckerInputsById,
		*,
		declared_can_throw_by_id: Mapping[FunctionId, bool] | None = None,
		exception_catalog: Mapping[str, int] | None = None,
		type_table: "TypeTable" | None = None,
		fn_decls_by_id: Iterable[FunctionId] | None = None,
	) -> CheckedProgramById:
		checker = cls(
			declared_can_throw_by_id=declared_can_throw_by_id,
			signatures_by_id=inputs.signatures_by_id,
			exception_catalog=exception_catalog,
			hir_blocks_by_id=inputs.hir_blocks_by_id,
			type_table=type_table,
			call_info_by_callsite_id=inputs.call_info_by_callsite_id,
		)
		if fn_decls_by_id is None:
			decl_ids = list(checker._hir_blocks_by_id.keys())
		else:
			decl_ids = list(fn_decls_by_id)
		return checker.check_by_id(decl_ids)

	def _normalize_and_collect_catch_arms(
		self,
		hir_blocks: Mapping[FunctionId, "H.HBlock"],
	) -> dict[FunctionId, list[list[CatchArmInfo]]]:
		"""
		Collect catch-arm info uniformly for all callers without mutating HIR.

		This keeps catch validation consistent between the CLI and stub helpers
		without running normalization passes that can reassign node ids.
		"""
		catch_arms: dict[FunctionId, list[list[CatchArmInfo]]] = {}
		for fn_id, block in hir_blocks.items():
			arms = collect_catch_arms_from_block(block)
			if arms:
				catch_arms[fn_id] = arms
		return catch_arms

	def check_by_id(self, fn_decls: Iterable[FunctionId]) -> CheckedProgramById:
		"""
		Produce a CheckedProgram with FnInfo for each fn name in `fn_decls`.

		This stub also validates any provided catch arms against the
		exception catalog when available, accumulating diagnostics instead
		of raising.
		"""
		self._ensure_core_types()
		fn_infos: Dict[FunctionId, FnInfo] = {}
		diagnostics: List[Diagnostic] = []
		known_events: Set[str] = set(self._exception_catalog.keys()) if self._exception_catalog else set()
		exc_schemas = getattr(self._type_table, "exception_schemas", None)
		if isinstance(exc_schemas, dict):
			known_events.update(exc_schemas.keys())
		callinfo_ok_by_fn: Dict[FunctionId, bool] = {}
		skip_validation: Set[FunctionId] = set()

		def _collect_callsite_ids(block: "H.HBlock") -> list[int]:
			from lang.driftc import stage1 as H

			ids: list[int] = []

			def walk_expr(expr: H.HExpr) -> None:
				if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HLambda):
					csid = getattr(expr, "callsite_id", None)
					if isinstance(csid, int):
						ids.append(csid)
					lam = expr.fn
					if getattr(lam, "body_expr", None) is not None:
						walk_expr(lam.body_expr)
					if getattr(lam, "body_block", None) is not None:
						walk_block(lam.body_block)
					for arg in expr.args:
						walk_expr(arg)
					for kw in getattr(expr, "kwargs", []) or []:
						walk_expr(kw.value)
					return
				if isinstance(expr, H.HLambda):
					if getattr(expr, "body_expr", None) is not None:
						walk_expr(expr.body_expr)
					if getattr(expr, "body_block", None) is not None:
						walk_block(expr.body_block)
					return
				if isinstance(expr, (H.HCall, H.HMethodCall, H.HInvoke)):
					csid = getattr(expr, "callsite_id", None)
					if isinstance(csid, int):
						ids.append(csid)
					if isinstance(expr, H.HMethodCall):
						walk_expr(expr.receiver)
					elif isinstance(expr, H.HInvoke):
						walk_expr(expr.callee)
					for arg in expr.args:
						walk_expr(arg)
					for kw in getattr(expr, "kwargs", []) or []:
						walk_expr(kw.value)
					return
				if isinstance(expr, H.HResultOk):
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
				elif isinstance(expr, H.HTryExpr):
					walk_expr(expr.attempt)
					for arm in expr.arms:
						walk_block(arm.block)
						if getattr(arm, "result", None) is not None:
							walk_expr(arm.result)
				elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
					walk_block(expr.body)
					walk_expr(expr.result)
				elif getattr(H, "HBlockExpr", None) is not None and isinstance(expr, H.HBlockExpr):
					walk_block(expr.block)
				elif isinstance(expr, H.HMatchExpr):
					walk_expr(getattr(expr, "scrutinee", getattr(expr, "subject", None)))
					for arm in expr.arms:
						walk_block(arm.block)
						if getattr(arm, "result", None) is not None:
							walk_expr(arm.result)

			def walk_stmt(stmt: H.HStmt) -> None:
				if isinstance(stmt, H.HLocalConst):
					walk_expr(stmt.value)
				elif isinstance(stmt, H.HLet):
					if stmt.value is not None:
						walk_expr(stmt.value)
				elif isinstance(stmt, H.HAssign):
					walk_expr(stmt.value)
				elif isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.expr)
				elif isinstance(stmt, H.HReturn):
					if stmt.value is not None:
						walk_expr(stmt.value)
				elif isinstance(stmt, H.HThrow):
					if stmt.value is not None:
						walk_expr(stmt.value)
				elif isinstance(stmt, H.HTry):
					walk_block(stmt.body)
					for arm in stmt.catches:
						walk_block(arm.block)
				elif getattr(H, "HIf", None) is not None and isinstance(stmt, H.HIf):
					walk_expr(stmt.cond)
					walk_block(stmt.then_block)
					if stmt.else_block is not None:
						walk_block(stmt.else_block)
				elif getattr(H, "HLoop", None) is not None and isinstance(stmt, H.HLoop):
					walk_block(stmt.body)
				elif getattr(H, "HWhile", None) is not None and isinstance(stmt, H.HWhile):
					walk_expr(stmt.cond)
					walk_block(stmt.body)
				elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
					walk_block(stmt.block)
				elif getattr(H, "HFor", None) is not None and isinstance(stmt, H.HFor):
					walk_expr(stmt.iterable)
					walk_block(stmt.body)
				elif getattr(H, "HMatchStmt", None) is not None and isinstance(stmt, H.HMatchStmt):
					walk_expr(getattr(stmt, "scrutinee", getattr(stmt, "subject", None)))
					for arm in stmt.arms:
						walk_block(arm.block)
				elif isinstance(stmt, H.HBlock):
					walk_block(stmt)

			def walk_block(block: H.HBlock) -> None:
				for stmt in block.statements:
					walk_stmt(stmt)

			walk_block(block)
			return ids

		for fn_id, hir_block in self._hir_blocks_by_id.items():
			call_info_by_callsite_id = self._call_info_by_callsite_id.get(fn_id)
			call_sites = _collect_callsite_ids(hir_block)
			if not call_sites:
				callinfo_ok_by_fn[fn_id] = True
				continue
			if call_info_by_callsite_id is None:
				callinfo_ok_by_fn[fn_id] = False
				skip_validation.add(fn_id)
				continue
			missing = [csid for csid in call_sites if csid not in call_info_by_callsite_id]
			if missing:
				callinfo_ok_by_fn[fn_id] = False
				skip_validation.add(fn_id)
				continue
			callinfo_ok_by_fn[fn_id] = True

		for fn_id in fn_decls:
			declared_can_throw = self._declared_by_id.get(fn_id)
			sig = self._signatures_by_id.get(fn_id)
			declared_events: Optional[FrozenSet[str]] = None
			return_type = None
			return_type_id: Optional[TypeId] = None
			error_type_id: Optional[TypeId] = None

			if sig is not None:
				declared_events = frozenset(sig.throws_events) if sig.throws_events else None

				return_type_id = sig.return_type_id
				error_type_id = sig.error_type_id
				if return_type_id is None or sig.param_type_ids is None:
					diagnostics.append(
						_chk_diag(
							message=f"typecheck contract failure: signature for '{sig.name}' is missing TypeIds",
							severity="error",
							span=Span.from_loc(getattr(sig, "loc", None)),
						)
					)
				if (sig.declared_can_throw is True) and error_type_id is None:
					diagnostics.append(
						_chk_diag(
							message=f"typecheck contract failure: signature for '{sig.name}' missing error TypeId",
							severity="error",
							span=Span.from_loc(getattr(sig, "loc", None)),
						)
					)

				# Keep legacy/raw fields for backward compatibility.
				return_type = sig.return_type
				if declared_events is None and sig.throws_events:
					declared_events = frozenset(sig.throws_events)
				# Legacy shim: an explicit bool map overrides signatures in tests.
				if declared_can_throw is None and sig.declared_can_throw is not None:
					declared_can_throw = bool(sig.declared_can_throw)

			if declared_can_throw is None:
				diagnostics.append(
					_chk_diag(
						message="typecheck contract failure: signature missing declared_can_throw",
						code="E_INTERNAL_MISSING_DECLARED_CAN_THROW",
						severity="error",
						span=Span.from_loc(getattr(sig, "loc", None)),
					)
				)
				skip_validation.add(fn_id)
				declared_can_throw = True

			# Method receiver validation (spec §3.8).
			#
			# For production correctness, method receiver conventions are validated
			# in the checker (typecheck phase), not during parsing.
			if sig is not None and sig.is_method:
				is_associated_static = sig.self_mode is None
				if is_associated_static and sig.param_type_ids and sig.impl_target_type_id is not None:
					first_param_ty = sig.param_type_ids[0]
					target_tid = sig.impl_target_type_id
					target_inst = self._type_table.get_struct_instance(target_tid)
					def _receiver_like_type(tid: TypeId) -> bool:
						if tid == target_tid:
							return True
						inst = self._type_table.get_struct_instance(tid)
						if inst is not None:
							if inst.base_id == target_tid:
								return True
							if target_inst is not None and inst.base_id == target_inst.base_id and list(inst.type_args) == list(target_inst.type_args):
								return True
						if target_inst is not None and tid == target_inst.base_id:
							return True
						td = self._type_table.get(tid)
						target_td = self._type_table.get(target_tid)
						if td.kind is TypeKind.ARRAY and target_td.kind is TypeKind.ARRAY:
							return True
						return False
					receiver_like = _receiver_like_type(first_param_ty)
					if not receiver_like:
						td_first = self._type_table.get(first_param_ty)
						if td_first.kind is TypeKind.REF and td_first.param_types:
							receiver_like = _receiver_like_type(td_first.param_types[0])
					if receiver_like and (not sig.param_names or sig.param_names[0] != "self"):
						diagnostics.append(
							_chk_diag(
								message=f"first parameter of method '{sig.method_name or sig.name}' must be named 'self'",
								severity="error",
								span=Span.from_loc(getattr(sig, "loc", None)),
							)
						)
				if not is_associated_static:
					# Receiver name: methods must declare a receiver parameter named `self`
					# as their first parameter.
					if not sig.param_names:
						diagnostics.append(
							_chk_diag(
								message=f"method '{sig.method_name or sig.name}' must declare a receiver parameter 'self'",
								severity="error",
								span=Span.from_loc(getattr(sig, "loc", None)),
							)
						)
					elif sig.param_names[0] != "self":
						diagnostics.append(
							_chk_diag(
								message=f"first parameter of method '{sig.method_name or sig.name}' must be named 'self'",
								severity="error",
								span=Span.from_loc(getattr(sig, "loc", None)),
							)
						)
					# Receiver type must match the impl target type according to self_mode.
					# Skip for verified package-origin signatures (no source location
					# AND module registered in the package module map) — these were
					# already validated when compiled in their originating package.
					_skip_pkg_receiver = False
					if getattr(sig, "loc", None) is None:
						_mp = getattr(self._type_table, "module_packages", None)
						if _mp is not None and fn_id.module in _mp:
							_skip_pkg_receiver = True
					if sig.param_type_ids and sig.impl_target_type_id is not None and sig.self_mode is not None and not _skip_pkg_receiver:
						recv_ty = sig.param_type_ids[0]
						expected: TypeId | None = None
						if sig.self_mode == "value":
							expected = sig.impl_target_type_id
						elif sig.self_mode == "ref":
							expected = self._type_table.ensure_ref(sig.impl_target_type_id)
						elif sig.self_mode == "ref_mut":
							expected = self._type_table.ensure_ref_mut(sig.impl_target_type_id)
						if expected is not None:
							target_td = self._type_table.get(sig.impl_target_type_id)
							if target_td.kind is TypeKind.REF:
								if sig.self_mode == "ref" and not target_td.ref_mut:
									expected = sig.impl_target_type_id
								elif sig.self_mode == "ref_mut" and target_td.ref_mut:
									expected = sig.impl_target_type_id
						impl_args = getattr(sig, "impl_target_type_args", None)
						receiver_ok = expected is not None and recv_ty == expected
						if not receiver_ok:
							check_ty = recv_ty
							if sig.self_mode in {"ref", "ref_mut"}:
								td_recv = self._type_table.get(recv_ty)
								if td_recv.kind is TypeKind.REF and td_recv.param_types:
									check_ty = td_recv.param_types[0]
							if not receiver_ok and target_td.kind is TypeKind.ARRAY:
								check_def = self._type_table.get(check_ty)
								if check_def.kind is TypeKind.ARRAY:
									receiver_ok = True
							struct_inst = self._type_table.get_struct_instance(check_ty)
							var_inst = self._type_table.get_variant_instance(check_ty)
							if struct_inst is not None:
								if struct_inst.base_id == sig.impl_target_type_id and (impl_args is None or list(struct_inst.type_args) == list(impl_args)):
									receiver_ok = True
							elif var_inst is not None:
								if var_inst.base_id == sig.impl_target_type_id and (impl_args is None or list(var_inst.type_args) == list(impl_args)):
									receiver_ok = True
							if not receiver_ok and impl_args:
								struct_template_id = self._type_table._struct_template_cache.get((sig.impl_target_type_id, tuple(impl_args)))
								if struct_template_id is not None and struct_template_id == check_ty:
									receiver_ok = True
							if not receiver_ok and impl_args:
								variant_template_id = self._type_table._variant_template_cache.get((sig.impl_target_type_id, tuple(impl_args)))
								if variant_template_id is not None and variant_template_id == check_ty:
									receiver_ok = True
						if expected is not None and not receiver_ok:
							target_name = self._type_table.get(sig.impl_target_type_id).name
							diagnostics.append(
								_chk_diag(
									message=f"receiver type for method '{sig.method_name or sig.name}' must be '{target_name}' (or '&{target_name}' / '&mut {target_name}')",
									severity="error",
									span=Span.from_loc(getattr(sig, "loc", None)),
								)
							)

			catch_arms_groups = self._catch_arms.get(fn_id)
			if catch_arms_groups is not None:
				for arms in catch_arms_groups:
					validate_catch_arms(arms, known_events, diagnostics)

			fn_infos[fn_id] = FnInfo(
				fn_id=fn_id,
				name=function_symbol(fn_id),
				declared_can_throw=declared_can_throw,
				signature=sig,
				declared_events=declared_events,
				return_type=return_type,  # legacy/raw
				return_type_id=return_type_id,
				error_type_id=error_type_id,
			)

		# TODO: real checker will:
		#   - resolve signatures (FnResult/throws),
		#   - collect catch arms per function and validate them against the exception catalog,
		#   - build a concrete TypeEnv and diagnostics list.
		# The real checker will attach type_env, diagnostics, and exception_catalog.

		# Can-throw inference (fixed point, intra-module).
		#
		# The surface language does not expose `FnResult` as a type. Instead, a
		# function either returns normally (`-> T`) or may throw (exceptional
		# control flow). Internally, codegen lowers can-throw functions to return
		# `FnResult<T, Error>` as an ABI carrier.
		#
		# We infer can-throw from HIR, taking try/catch coverage into account:
		# a throw (or call to a can-throw function) that is fully handled by an
		# enclosing try/catch does not force the function itself to be can-throw.
		#
		# `FnSignature.declared_can_throw` is treated as an explicit annotation
		# hook (future `nothrow`/throws syntax). If set to False, we keep the
		# function non-throwing and emit a diagnostic when inference finds an
		# escaping throw.
		first_throw_span_by_fn: dict[FunctionId, Span] = {}
		first_throw_note_by_fn: dict[FunctionId, str] = {}
		call_info_by_callsite_id: Mapping[int, CallInfo] | None = None
		changed = True
		while changed:
			changed = False
			for fn_id, hir_block in self._hir_blocks_by_id.items():
				info = fn_infos.get(fn_id)
				if info is None:
					continue
				if not callinfo_ok_by_fn.get(fn_id, True):
					continue
				call_info_by_callsite_id = self._call_info_by_callsite_id.get(fn_id)
				may_throw, throw_span, throw_note = self._function_may_throw(
					hir_block,
					fn_infos,
					fn_id,
					call_info_by_callsite_id,
				)
				first_throw_span_by_fn.setdefault(fn_id, throw_span)
				if throw_note:
					first_throw_note_by_fn.setdefault(fn_id, throw_note)
				old_inferred = info.inferred_may_throw
				info.inferred_may_throw = may_throw
				if may_throw and not old_inferred:
					changed = True
				explicit = info.signature.declared_can_throw if info.signature is not None else None
				# Legacy test shim: `declared_can_throw` map is treated as an explicit
				# annotation.
				if fn_id in self._declared_by_id:
					explicit = bool(self._declared_by_id[fn_id])
				if explicit is False:
					# Explicit nothrow: keep non-throwing; diagnose below.
					continue
				if may_throw and not info.declared_can_throw:
					info.declared_can_throw = True

		# Emit diagnostics for explicit nothrow signatures that still may throw.
		for fn_id, info in fn_infos.items():
			if info.signature is None:
				continue
			explicit = info.signature.declared_can_throw
			if fn_id in self._declared_by_id:
				explicit = bool(self._declared_by_id[fn_id])
			if explicit is False and info.inferred_may_throw:
				notes: list[str] | None = None
				note = first_throw_note_by_fn.get(fn_id)
				if note:
					notes = [note]
				diagnostics.append(
					_chk_diag(
						message=f"function {function_symbol(fn_id)} is declared nothrow but may throw",
						severity="error",
						span=first_throw_span_by_fn.get(fn_id, Span()),
						notes=notes,
					)
				)

		# Best-effort call arity/type checks based on FnSignature TypeIds. This
		# only visits HCall with a plain HVar callee; arg types are unknown here
		# so only arity is enforced.
		for fn_id, hir_block in self._hir_blocks_by_id.items():
			info = fn_infos.get(fn_id)
			if info is None:
				continue
			if fn_id in skip_validation:
				continue
			if not callinfo_ok_by_fn.get(fn_id, True):
				continue
			self._validate_calls(
				hir_block,
				fn_infos,
				diagnostics,
				call_info_by_callsite_id=self._call_info_by_callsite_id.get(fn_id),
				current_fn=info,
			)

		# Array/boolean checks share a typing context per function to keep locals
		# consistent across validations.
		for fn_id, hir_block in self._hir_blocks_by_id.items():
			info = fn_infos.get(fn_id)
			if info is None:
				continue
			if fn_id in skip_validation:
				continue
			# Each function gets its own typing context so locals/diagnostics are
			# isolated while reusing the shared traversal logic.
			ctx = self._TypingContext(
				checker=self,
				table=self._type_table,
				fn_infos=fn_infos,
				current_fn=info,
				call_info_by_callsite_id=self._call_info_by_callsite_id.get(fn_id),
				locals={},
				diagnostics=diagnostics,
			)
			self._seed_locals_from_signature(ctx)

			def combined_on_expr(expr: "H.HExpr", typing_ctx: Checker._TypingContext = ctx) -> None:
				self._match_validator_on_expr(expr, typing_ctx)
				self._array_validator_on_expr(expr, typing_ctx)
				self._bitwise_validator_on_expr(expr, typing_ctx)
				self._void_usage_on_expr(expr, typing_ctx)

			def combined_on_stmt(stmt: "H.HStmt", typing_ctx: Checker._TypingContext = ctx) -> None:
				self._bool_validator_on_stmt(stmt, typing_ctx)
				self._void_rules_on_stmt(stmt, typing_ctx)
				self._exception_init_rules_on_stmt(stmt, typing_ctx)

			self._walk_hir(hir_block, ctx, on_expr=combined_on_expr, on_stmt=combined_on_stmt)

		return CheckedProgramById(
			fn_infos_by_id=fn_infos,
			type_table=self._type_table,
			type_env=None,
			exception_catalog=self._exception_catalog,
			diagnostics=diagnostics,
		)

	def _call_may_throw(self, callee_id: FunctionId, fn_infos: Mapping[FunctionId, FnInfo]) -> bool:
		"""Determine if a call to `callee_id` may throw, based on FnInfo.

		For non-boundary calls this uses *inferred_may_throw* — the semantic
		answer computed by walking the callee's body. FnSignature.declared_can_throw
		is an ABI flag (True for all non-nothrow functions) and must not be
		used to decide whether a call site actually throws.
		"""
		info = fn_infos.get(callee_id)
		if info is None:
			return False
		# K30: Use inferred_may_throw for the semantic question.
		# Only explicit `nothrow` (declared_can_throw=False on signature)
		# can force non-throwing — and that's already handled by the
		# boundary-call path in _function_may_throw.
		return info.inferred_may_throw

	def _is_boundary_call(
		self,
		callee_id: FunctionId,
		caller_id: FunctionId,
		fn_infos: Mapping[FunctionId, FnInfo],
	) -> bool:
		info = fn_infos.get(callee_id)
		if info is None or info.signature is None:
			return False
		if not (info.signature.is_exported_entrypoint or info.signature.is_extern):
			return False
		module_packages = getattr(self._type_table, "module_packages", None)
		if module_packages is None:
			raise AssertionError("module_packages missing for boundary check (checker bug)")
		callee_pkg = module_packages.get(callee_id.module)
		caller_pkg = module_packages.get(caller_id.module)
		if callee_pkg is None or caller_pkg is None:
			raise AssertionError("module_packages missing entry for boundary check (checker bug)")
		return callee_pkg != caller_pkg

	def _function_may_throw(
		self,
		block: "H.HBlock",
		fn_infos: Mapping[FunctionId, FnInfo],
		current_fn: FunctionId,
		call_info_by_callsite_id: Mapping[int, CallInfo] | None,
		*,
		unknown_calls_throw: bool = False,
		indexing_throws: bool = False,
	) -> tuple[bool, Span, str | None]:  # type: ignore[name-defined]
		"""
		Walk a HIR block and conservatively decide if it may throw.

		Any `HThrow` or call to a function marked can-throw sets the flag. This is
		still conservative but now accounts for try/catch coverage when the thrown
		exception is an `HExceptionInit` that matches a catch arm (or a catch-all).
		"""
		from lang.driftc import stage1 as H
		may_throw = False
		first_span: Span | None = None
		first_note: str | None = None

		def walk_expr(expr: H.HExpr, caught_events: set[str] | None, catch_all: bool) -> None:
			nonlocal may_throw
			nonlocal first_span
			nonlocal first_note
			if isinstance(expr, H.HCall):
				if call_info_by_callsite_id is None:
					raise AssertionError(
						f"BUG: missing CallInfo map for call during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					raise AssertionError(
						f"BUG: missing CallInfo for call during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				call_can_throw = bool(info.sig.can_throw)
				# K30: For inline lambda calls, walk the lambda body directly
				# instead of trusting the ABI-level can_throw (which may be
				# conservatively True when the body only calls nothrow wrappers).
				if call_can_throw and isinstance(expr.fn, H.HLambda):
					lam = expr.fn
					if lam.body_block is not None:
						walk_block(lam.body_block, caught_events, catch_all)
					if lam.body_expr is not None:
						walk_expr(lam.body_expr, caught_events, catch_all)
					for arg in expr.args:
						walk_expr(arg, caught_events, catch_all)
					for kw in getattr(expr, "kwargs", []) or []:
						walk_expr(kw.value, caught_events, catch_all)
					return
				if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
					callee_id = info.target.symbol
					if self._is_boundary_call(callee_id, current_fn, fn_infos):
						callee_info = fn_infos.get(callee_id)
						callee_sig = callee_info.signature if callee_info else None
						if callee_sig is not None and callee_sig.declared_can_throw is False:
							call_can_throw = False
						else:
							# Keep call_can_throw from info.sig.can_throw;
							# do not unconditionally force True.
							if call_can_throw and first_note is None:
								first_note = (
									"cross-module call to exported/extern requires can-throw calling convention"
								)
					elif call_can_throw:
						call_can_throw = self._call_may_throw(callee_id, fn_infos)
				elif call_can_throw and info.target.kind is getattr(CallTargetKind, "TRAIT", None):
					# K30: For generic trait calls, check if the trait method is
					# declared nothrow.  All impls must honour the nothrow contract,
					# so if the trait method itself is nothrow, the call is nothrow.
					trait_key = info.target.trait_key
					method_name = info.target.method_name
					if trait_key is not None and method_name is not None:
						trait_module = getattr(trait_key, "module", None)
						# Scan signatures for matching non-wrapper trait method in this module.
						for _sid, _sig in self._signatures_by_id.items():
							if getattr(_sig, "method_name", None) != method_name:
								continue
							if getattr(_sid, "module", None) != trait_module:
								continue
							if getattr(_sig, "is_wrapper", False):
								continue
							# Found a matching non-wrapper impl. Check declared_can_throw.
							if _sig.declared_can_throw is False:
								call_can_throw = False
							break
				if call_can_throw and not catch_all:
					may_throw = True
					if first_span is None:
						first_span = Span.from_loc(getattr(expr, "loc", None))
					if first_note is None:
						first_note = "call may throw"
				for arg in expr.args:
					walk_expr(arg, caught_events, catch_all)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value, caught_events, catch_all)
			elif isinstance(expr, H.HMethodCall):
				if call_info_by_callsite_id is None:
					raise AssertionError(
						f"BUG: missing CallInfo map for method call during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					raise AssertionError(
						f"BUG: missing CallInfo for method call during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				call_can_throw = info.sig.can_throw
				if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
					fn_info = fn_infos.get(info.target.symbol)
					if call_can_throw and fn_info is not None:
						# K30: For non-boundary direct method calls, use
						# inferred_may_throw (semantic answer). For ABI wrappers,
						# look through to the wrapped method's annotation.
						wrapped_fn_id = getattr(fn_info.signature, "wraps_target_fn_id", None) if fn_info.signature else None
						if wrapped_fn_id is not None:
							wrapped_sig = self._signatures_by_id.get(wrapped_fn_id)
							if wrapped_sig is not None and wrapped_sig.declared_can_throw is False:
								call_can_throw = False
							else:
								call_can_throw = fn_info.inferred_may_throw
						else:
							call_can_throw = fn_info.inferred_may_throw
					elif call_can_throw and fn_info is None:
						# Target not in fn_infos (package method or ABI
						# wrapper).  Fall back to signature metadata,
						# following wraps_target_fn_id for wrappers.
						callee_sig = self._signatures_by_id.get(info.target.symbol)
						if callee_sig is not None:
							if callee_sig.declared_can_throw is False:
								call_can_throw = False
							elif getattr(callee_sig, "wraps_target_fn_id", None) is not None:
								wrapped_sig = self._signatures_by_id.get(callee_sig.wraps_target_fn_id)
								if wrapped_sig is not None and wrapped_sig.declared_can_throw is False:
									call_can_throw = False
				if call_can_throw and not catch_all:
					may_throw = True
					if first_span is None:
						first_span = Span.from_loc(getattr(expr, "loc", None))
					if first_note is None:
						first_note = "method call may throw"
				walk_expr(expr.receiver, caught_events, catch_all)
				for arg in expr.args:
					walk_expr(arg, caught_events, catch_all)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value, caught_events, catch_all)
			elif isinstance(expr, H.HInvoke):
				if call_info_by_callsite_id is None:
					raise AssertionError(
						f"BUG: missing CallInfo map for invoke during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					raise AssertionError(
						f"BUG: missing CallInfo for invoke during nothrow analysis (callsite_id={getattr(expr, 'callsite_id', None)})"
					)
				# K30: For inline lambda invocations, the ABI-level can_throw
				# may be conservatively True even when the body is semantically
				# nothrow (e.g. lambda calls nothrow package wrappers).  Walk
				# the lambda body directly so the walker can inspect each call.
				if isinstance(expr.callee, H.HLambda):
					lam = expr.callee
					if lam.body_block is not None:
						walk_block(lam.body_block, caught_events, catch_all)
					if lam.body_expr is not None:
						walk_expr(lam.body_expr, caught_events, catch_all)
				else:
					if info.sig.can_throw and not catch_all:
						may_throw = True
						if first_span is None:
							first_span = Span.from_loc(getattr(expr, "loc", None))
						if first_note is None:
							first_note = "call may throw"
					walk_expr(expr.callee, caught_events, catch_all)
				for arg in expr.args:
					walk_expr(arg, caught_events, catch_all)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value, caught_events, catch_all)
			elif hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
				# Expression-form try/catch creates a local handler scope for the
				# attempt expression. A catch-all arm handles any propagated throw.
				catch_all_local = any(arm.event_fqn is None for arm in expr.arms)
				caught_local = {arm.event_fqn for arm in expr.arms if arm.event_fqn is not None}
				walk_expr(expr.attempt, caught_local, catch_all_local)
				for arm in expr.arms:
					# Catch-arm bodies are *not* within the local handler scope; throws
					# from these blocks propagate to the outer try context.
					walk_block(arm.block, caught_events, catch_all)
					if arm.result is not None:
						walk_expr(arm.result, caught_events, catch_all)
			elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				walk_block(expr.body, caught_events, catch_all)
				walk_expr(expr.result, caught_events, catch_all)
			elif isinstance(expr, H.HResultOk):
				walk_expr(expr.value, caught_events, catch_all)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left, caught_events, catch_all)
				walk_expr(expr.right, caught_events, catch_all)
			elif isinstance(expr, H.HUnary):
				walk_expr(expr.expr, caught_events, catch_all)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond, caught_events, catch_all)
				walk_expr(expr.then_expr, caught_events, catch_all)
				walk_expr(expr.else_expr, caught_events, catch_all)
			elif isinstance(expr, H.HField):
				walk_expr(expr.subject, caught_events, catch_all)
			elif isinstance(expr, H.HIndex):
				walk_expr(expr.subject, caught_events, catch_all)
				walk_expr(expr.index, caught_events, catch_all)
				if indexing_throws and not catch_all:
					may_throw = True
					if first_span is None:
						first_span = Span.from_loc(getattr(expr, "loc", None))
					if first_note is None:
						first_note = "indexing may throw"
			elif isinstance(expr, H.HDVInit):
				for a in expr.args:
					walk_expr(a, caught_events, catch_all)
			elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				walk_expr(expr.scrutinee, caught_events, catch_all)
				for arm in expr.arms:
					walk_block(arm.block, caught_events, catch_all)
					if getattr(arm, "result", None) is not None:
						walk_expr(arm.result, caught_events, catch_all)
			# literals/vars are leaf nodes

		def walk_block(b: H.HBlock, caught: set[str] | None = None, catch_all: bool = False) -> None:
			nonlocal may_throw
			nonlocal first_span
			for stmt in b.statements:
				if hasattr(H, "HRethrow") and isinstance(stmt, getattr(H, "HRethrow")):
					# Rethrow re-propagates an unknown Error value. We only treat it as
					# handled when a catch-all is in scope.
					if catch_all:
						continue
					may_throw = True
					if first_span is None:
						first_span = Span.from_loc(getattr(stmt, "loc", None))
					continue
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
						# Catch blocks still live within the outer try context (if any),
						# so propagate the current caught/catch_all flags rather than
						# resetting them.
						walk_block(arm.block, caught, catch_all)
					continue
				if isinstance(stmt, H.HReturn) and stmt.value is not None:
					walk_expr(stmt.value, caught, catch_all)
					continue
				if isinstance(stmt, H.HLocalConst):
					walk_expr(stmt.value, caught, catch_all)
					continue
				if isinstance(stmt, H.HLet):
					walk_expr(stmt.value, caught, catch_all)
					continue
				if isinstance(stmt, H.HAssign):
					walk_expr(stmt.value, caught, catch_all)
					continue
				if isinstance(stmt, H.HIf):
					walk_expr(stmt.cond, caught, catch_all)
					walk_block(stmt.then_block, caught, catch_all)
					if stmt.else_block:
						walk_block(stmt.else_block, caught, catch_all)
					continue
				if isinstance(stmt, H.HLoop):
					walk_block(stmt.body, caught, catch_all)
					continue
				if isinstance(stmt, H.HExprStmt):
					walk_expr(stmt.expr, caught, catch_all)
					continue
				if isinstance(stmt, H.HBlock):
					walk_block(stmt, caught, catch_all)
					continue
				if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
					walk_block(stmt.block, caught, catch_all)
					continue
				if isinstance(stmt, H.HAugAssign):
					walk_expr(stmt.value, caught, catch_all)
					continue
				if hasattr(H, "HAssert") and isinstance(stmt, getattr(H, "HAssert")):
					walk_expr(stmt.cond, caught, catch_all)
					if stmt.msg is not None:
						walk_expr(stmt.msg, caught, catch_all)
					continue
			# other statements (HBreak/HContinue): continue

		walk_block(block)
		return may_throw, (first_span or Span()), first_note

	def _expr_may_throw(
		self,
		expr: "H.HExpr",
		fn_infos: Mapping[FunctionId, FnInfo],
		current_fn: FunctionId,
		call_info_by_callsite_id: Mapping[int, CallInfo] | None,
		*,
		unknown_calls_throw: bool = False,
		indexing_throws: bool = False,
	) -> bool:
		from lang.driftc import stage1 as H

		block = H.HBlock(statements=[H.HExprStmt(expr=expr)])
		may_throw, _span, _note = self._function_may_throw(
			block,
			fn_infos,
			current_fn,
			call_info_by_callsite_id,
			unknown_calls_throw=unknown_calls_throw,
			indexing_throws=indexing_throws,
		)
		return may_throw

	@dataclass
	class _TypingContext:
		"""Shared typing state reused across validation passes for a function."""

		checker: "Checker"
		table: TypeTable
		fn_infos: Mapping[FunctionId, FnInfo]
		current_fn: Optional[FnInfo]
		call_info_by_callsite_id: Mapping[int, CallInfo] | None
		locals: Dict[str, TypeId]
		diagnostics: Optional[List[Diagnostic]]
		report_unknown_names: bool = True
		cache: Dict[int, Optional[TypeId]] = field(default_factory=dict)
		suppress_index_copy_check_expr_ids: set[int] = field(default_factory=set)

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

		def report_index_not_int(self, span: object | None = None) -> None:
			self._append_diag(
				_chk_diag(
				message="array index must be an Int",
					severity="error",
					span=Span.from_loc(span),
				)
			)

		def report_error_attr_key_not_string(self, span: object | None = None) -> None:
			self._append_diag(
				_chk_diag(
					message="Error.attrs expects a String key",
					severity="error",
					span=Span.from_loc(span),
					code="E-ERROR-ATTR-KEY-NOT-STRING",
				)
			)

		def report_index_subject_not_array(self, span: object | None = None) -> None:
			self._append_diag(
				_chk_diag(
					message="indexing requires an Array value",
					severity="error",
					span=Span.from_loc(span),
				)
			)

		def report_empty_array_literal(self, span: object | None = None) -> None:
			self._append_diag(
				_chk_diag(
					message="empty array literal requires explicit type",
					severity="error",
					span=Span.from_loc(span),
				)
			)

		def report_mixed_array_literal(self, span: object | None = None) -> None:
			self._append_diag(
				_chk_diag(
					message="array literal elements do not have a consistent type",
					severity="error",
					span=Span.from_loc(span),
				)
			)

		def _unwrap_ref_typeid(self, ty: TypeId) -> TypeId:
			td = self.table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				return td.param_types[0]
			return ty

		def _is_error_subject(self, expr: "H.HExpr") -> Optional[bool]:
			err_ty = self._infer_expr_type(expr)
			if err_ty is None:
				return None
			err_ty = self._unwrap_ref_typeid(err_ty)
			err_def = self.table.get(err_ty)
			return err_def.kind is TypeKind.ERROR

		def _append_diag(self, diag: Diagnostic) -> None:
			if self.diagnostics is not None:
				self.diagnostics.append(diag)

		def _infer_expr_type(self, expr: "H.HExpr") -> Optional[TypeId]:
			"""
			Very shallow expression type inference for call-arg checking.

			This intentionally handles only a small set of expression shapes:

			- primitive literals
			- local variables (as seeded by signatures/let bindings)
			- direct function calls `foo(...)` when a signature is available
			- selected builtins (`String.EMPTY`)
			- f-strings (always `String`, with MVP validation)
			- `try/catch` expression (MVP typing rules only)

			Everything else returns `None` to avoid guessing.
			"""
			from lang.driftc import stage1 as H

			checker = self.checker
			bitwise_ops = {
				H.BinaryOp.BIT_AND,
				H.BinaryOp.BIT_OR,
				H.BinaryOp.BIT_XOR,
				H.BinaryOp.SHL,
				H.BinaryOp.SHR,
			}
			comparison_ops = {
				H.BinaryOp.EQ,
				H.BinaryOp.NE,
				H.BinaryOp.LT,
				H.BinaryOp.LE,
				H.BinaryOp.GT,
				H.BinaryOp.GE,
			}
			bool_ops = {H.BinaryOp.AND, H.BinaryOp.OR}
			string_binops = {
				H.BinaryOp.ADD,
				H.BinaryOp.EQ,
				H.BinaryOp.NE,
				H.BinaryOp.LT,
				H.BinaryOp.LE,
				H.BinaryOp.GT,
				H.BinaryOp.GE,
			}
			def _contains_type_params(ty: TypeId, seen: set[int]) -> bool:
				if int(ty) in seen:
					return False
				seen.add(int(ty))
				td0 = self.table.get(ty)
				if td0.kind is TypeKind.TYPEVAR:
					return True
				for child in td0.param_types:
					if _contains_type_params(child, seen):
						return True
				return False

			if isinstance(expr, H.HLiteralInt):
				return checker._int_type
			if hasattr(H, "HLiteralUint") and isinstance(expr, getattr(H, "HLiteralUint")):
				return checker._uint_type
			if hasattr(H, "HLiteralUint64") and isinstance(expr, getattr(H, "HLiteralUint64")):
				return checker._uint64_type
			if hasattr(H, "HLiteralFloat") and isinstance(expr, getattr(H, "HLiteralFloat")):
				return checker._float_type
			if isinstance(expr, H.HLiteralBool):
				return checker._bool_type
			if hasattr(H, "HLiteralString") and isinstance(expr, getattr(H, "HLiteralString")):
				return checker._string_type

			if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				match_ty: Optional[TypeId] = None
				prev_report_unknown = self.report_unknown_names
				self.report_unknown_names = False
				try:
					for arm in expr.arms:
						arm_ty: Optional[TypeId] = None
						if getattr(arm, "result", None) is not None:
							arm_ty = self._infer_expr_type(arm.result)
						else:
							last = arm.block.statements[-1] if arm.block.statements else None
							if isinstance(last, H.HExprStmt):
								arm_ty = self._infer_expr_type(last.expr)
						if arm_ty is None:
							continue
						if match_ty is None:
							match_ty = arm_ty
						elif match_ty != arm_ty:
							return None
					return match_ty
				finally:
					self.report_unknown_names = prev_report_unknown

			if hasattr(H, "HFString") and isinstance(expr, getattr(H, "HFString")):
				for hole in expr.holes:
					if hole.spec.strip():
						self._append_diag(
							_chk_diag(
								message=f"E-FSTR-BAD-SPEC: format spec is not supported yet (have '{hole.spec}')",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
					hole_ty = self._infer_expr_type(hole.expr)
					if hole_ty is None:
						continue
					if hole_ty not in (
						checker._bool_type,
						checker._int_type,
						checker._uint_type,
						checker._float_type,
						checker._string_type,
					):
						self._append_diag(
							_chk_diag(
								message="E-FSTR-UNSUPPORTED-TYPE: f-string hole type is not supported in v1",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
				return checker._string_type

			if isinstance(expr, H.HVar):
				if expr.name == "String.EMPTY":
					return checker._string_type
				if expr.name in self.locals:
					return self.locals[expr.name]
				# Bare names can also denote function symbols (e.g. function-value
				# positions like `return g;`). Those are not local-variable errors.
				for fn_id in checker._signatures_by_id.keys():
					if expr.name == fn_id.name or expr.name == function_symbol(fn_id):
						return None
				# Emit unknown-name diagnostics only for user-style local identifiers.
				# We intentionally skip toolchain modules and constructor/type-like
				# symbols until checker name resolution is fully generalized.
				mod = getattr(getattr(self.current_fn, "signature", None), "module", None)
				is_toolchain_mod = isinstance(mod, str) and (mod.startswith("std.") or mod.startswith("lang."))
				is_local_ident = (
					bool(expr.name)
					and "." not in expr.name
					and expr.name[0].islower()
				)
				if not is_toolchain_mod and is_local_ident:
					if self.report_unknown_names:
						self._append_diag(
							_chk_diag(
								message=f"unknown name '{expr.name}'",
								severity="error",
								span=getattr(expr, "loc", Span()),
								code="E-UNKNOWN-NAME",
							)
						)
						return checker._unknown_type
					return None
			if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
				# `move <place>` yields the underlying value type (best-effort).
				return self._infer_expr_type(expr.subject)

			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				base_ty = self._infer_expr_type(expr.base) if expr.base is not None else None
				if base_ty is None:
					return None
				cur_ty = base_ty
				for proj in expr.projections:
					cur_def = self.table.get(cur_ty)
					if cur_def.kind is TypeKind.REF and cur_def.param_types:
						cur_ty = cur_def.param_types[0]
						cur_def = self.table.get(cur_ty)
					if isinstance(proj, H.HPlaceField):
						if cur_def.kind is TypeKind.STRUCT:
							info = self.table.struct_field(cur_ty, proj.name)
							if info is not None:
								_, cur_ty = info
							else:
								return None
						elif proj.name in ("len", "cap", "capacity"):
							return checker._len_cap_result_type(cur_ty)
						else:
							return None
					else:
						return None
				return cur_ty

			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
				if self.call_info_by_callsite_id is None:
					self._append_diag(
						_chk_diag(
								message="typecheck contract failure: missing CallInfo map for call typing",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
					return None
				info = self.call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					fn_name = function_symbol(self.current_fn.fn_id) if self.current_fn is not None else "<unknown>"
					csid = getattr(expr, "callsite_id", None)
					self._append_diag(
						_chk_diag(
								message=f"typecheck contract failure: missing CallInfo for call typing in {fn_name} (callsite_id={csid})",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
					return None
				if info.sig.user_ret_type == checker._unknown_type and getattr(expr, "force_inferred_type", None) is not None:
					return expr.force_inferred_type
				return info.sig.user_ret_type
			if isinstance(expr, (H.HMethodCall, H.HInvoke)):
				if self.call_info_by_callsite_id is None:
					self._append_diag(
						_chk_diag(
								message="typecheck contract failure: missing CallInfo map for call typing",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
					return None
				info = self.call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					fn_name = function_symbol(self.current_fn.fn_id) if self.current_fn is not None else "<unknown>"
					csid = getattr(expr, "callsite_id", None)
					self._append_diag(
						_chk_diag(
								message=f"typecheck contract failure: missing CallInfo for call typing in {fn_name} (callsite_id={csid})",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
					return None
				if info.sig.user_ret_type == checker._unknown_type and getattr(expr, "force_inferred_type", None) is not None:
					return expr.force_inferred_type
				return info.sig.user_ret_type
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HLambda):
				lam = expr.fn
				prev_locals = dict(self.locals)
				prev_report_unknown = self.report_unknown_names
				try:
					self.report_unknown_names = False
					mod = None
					if self.current_fn is not None and self.current_fn.signature is not None:
						mod = getattr(self.current_fn.signature, "module", None)
					for p in lam.params:
						ptype = None
						if getattr(p, "type", None) is not None:
							ptype = checker._resolve_typeexpr(p.type, module_id=mod)
						self.locals[p.name] = ptype
					expected_ret: TypeId | None = None
					if getattr(lam, "ret_type", None) is not None:
						expected_ret = checker._resolve_typeexpr(lam.ret_type, module_id=mod)
					if lam.body_expr is not None:
						body_ty = self._infer_expr_type(lam.body_expr)
						if expected_ret is not None and body_ty is not None and body_ty != expected_ret:
							self._append_diag(
								_chk_diag(
									message="lambda return type does not match body type",
									severity="error",
									span=getattr(lam, "span", Span()),
								)
							)
						return expected_ret if expected_ret is not None else body_ty
					if lam.body_block is not None and lam.body_block.statements:
						last = lam.body_block.statements[-1]
						if isinstance(last, H.HExprStmt):
							body_ty = self._infer_expr_type(last.expr)
							if expected_ret is not None and body_ty is not None and body_ty != expected_ret:
								self._append_diag(
									_chk_diag(
										message="lambda return type does not match body type",
										severity="error",
										span=getattr(lam, "span", Span()),
									)
								)
							return expected_ret if expected_ret is not None else body_ty
						if isinstance(last, H.HReturn) and last.value is not None:
							body_ty = self._infer_expr_type(last.value)
							if expected_ret is not None and body_ty is not None and body_ty != expected_ret:
								self._append_diag(
									_chk_diag(
										message="lambda return type does not match body type",
										severity="error",
										span=getattr(lam, "span", Span()),
									)
								)
							return expected_ret if expected_ret is not None else body_ty
					if expected_ret is not None:
						self._append_diag(
							_chk_diag(
								message="lambda with explicit return type must return a value",
								severity="error",
								span=getattr(lam, "span", Span()),
							)
						)
						return expected_ret
					return None
				finally:
					self.report_unknown_names = prev_report_unknown
					self.locals = prev_locals
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HQualifiedMember):
				if self.call_info_by_callsite_id is not None:
					info = self.call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
					if info is not None:
						return info.sig.user_ret_type
				# Qualified member calls like `Optional<Int>::Some(1)` produce an
				# instance of the referenced variant type. This is used for match
				# scrutinee typing and basic argument checks in the stub pipeline.
				mod = None
				if self.current_fn is not None and self.current_fn.signature is not None:
					mod = getattr(self.current_fn.signature, "module", None)
				base_ty = checker._resolve_typeexpr(expr.fn.base_type_expr, module_id=mod)
				td = self.table.get(base_ty)
				if td.kind is not TypeKind.VARIANT:
					return None

				# If this is already a concrete instantiation (i.e., not a declared
				# generic base), return it directly.
				if self.table.get_variant_instance(base_ty) is not None:
					return base_ty

				schema = self.table.variant_schemas.get(base_ty)
				if schema is None:
					return base_ty
				# Generic base with no explicit type args: try inference from ctor args.
				if not schema.type_params:
					return base_ty

				# Find the constructor schema so we can unify its field types with the
				# call argument types.
				arm_schema = None
				for a in schema.arms:
					if a.name == expr.fn.member:
						arm_schema = a
						break
				if arm_schema is None:
					return None

				# Best-effort inference: unify constructor field `GenericTypeExpr`s
				# against the inferred argument types. This is sufficient for MVP
				# generics used by Optional/Result and keeps the stub pipeline usable.
				type_args: list[TypeId | None] = [None for _ in schema.type_params]

				def unify(gty, actual: TypeId) -> None:
					# Type parameter reference.
					if getattr(gty, "param_index", None) is not None:
						idx = int(gty.param_index)
						if 0 <= idx < len(type_args):
							prev = type_args[idx]
							if prev is None:
								type_args[idx] = actual
							elif prev != actual:
								# Conflicting constraints: leave as-is; caller will treat
								# inference as failed.
								type_args[idx] = None
						return
					name = getattr(gty, "name", "")
					args = list(getattr(gty, "args", []) or [])
					if name in ("&", "&mut") and args:
						ad = self.table.get(actual)
						if ad.kind is TypeKind.REF and ad.param_types:
							unify(args[0], ad.param_types[0])
						return
					if name == "Array" and args:
						ad = self.table.get(actual)
						if ad.kind is TypeKind.ARRAY and ad.param_types:
							unify(args[0], ad.param_types[0])
						return
					if name == "Optional" and args:
						inst = self.table.get_variant_instance(actual)
						if inst is not None and inst.base_id == base_ty and inst.type_args:
							unify(args[0], inst.type_args[0])
						return
					# Other named nodes: we don't currently infer through them in v1.

				# Positional args only for MVP inference in the stub (named args are
				# validated by the typed checker).
				for field, arg_expr in zip(arm_schema.fields, expr.args):
					arg_ty = self._infer_expr_type(arg_expr)
					if arg_ty is None:
						continue
					unify(field.type_expr, arg_ty)

				if any(t is None for t in type_args):
					return None
				try:
					return self.table.ensure_instantiated(base_ty, [t for t in type_args if t is not None])
				except Exception:
					return None

			if isinstance(expr, H.HResultOk):
				if (
					self.current_fn
					and self.current_fn.signature
					and self.current_fn.signature.return_type_id is not None
				):
					return self.current_fn.signature.return_type_id
				return self.table.new_fnresult(checker._unknown_type, checker._error_type)

			if isinstance(expr, H.HBinary):
				left_ty = self._infer_expr_type(expr.left)
				right_ty = self._infer_expr_type(expr.right)
				string_left = left_ty == checker._string_type
				string_right = right_ty == checker._string_type
				if string_left or string_right:
					if string_left and string_right and expr.op in string_binops:
						if expr.op is H.BinaryOp.ADD:
							return checker._string_type
						# String comparisons are bytewise and yield Bool.
						return checker._bool_type
					non_string_known = (string_left and right_ty is not None and right_ty != checker._string_type) or (
						string_right and left_ty is not None and left_ty != checker._string_type
					)
					if non_string_known:
						self._append_diag(
							_chk_diag(
								message="string binary ops require String operands and support only +, ==, !=, <, <=, >, >=",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					return None
				if left_ty == checker._bool_type and right_ty == checker._bool_type:
					if expr.op in bool_ops:
						return checker._bool_type
					if expr.op in (H.BinaryOp.EQ, H.BinaryOp.NE):
						return checker._bool_type
					return None
				if left_ty == checker._int_type and right_ty == checker._int_type:
					if expr.op in comparison_ops:
						return checker._bool_type
					return checker._int_type
				if left_ty == checker._uint_type and right_ty == checker._uint_type:
					if expr.op in comparison_ops:
						return checker._bool_type
					return checker._uint_type
				if left_ty == checker._uint64_type and right_ty == checker._uint64_type:
					if expr.op in comparison_ops:
						return checker._bool_type
					return checker._uint64_type
				if expr.op in (H.BinaryOp.EQ, H.BinaryOp.NE) and left_ty is not None and right_ty is not None and left_ty == right_ty:
					left_def = self.table.get(left_ty)
					if left_def.kind in (TypeKind.VARIANT, TypeKind.INTERFACE, TypeKind.ARRAY, TypeKind.RAW_PTR, TypeKind.FNRESULT):
						self._append_diag(
							_chk_diag(
								message="==/!= are only supported for Bool, Int, Float, String in v1",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
				return None

			if isinstance(expr, H.HUnary):
				sub_ty = self._infer_expr_type(expr.expr)
				if expr.op is H.UnaryOp.DEREF:
					if sub_ty is None:
						return None
					sub_def = self.table.get(sub_ty)
					if sub_def.kind is TypeKind.REF and sub_def.param_types:
						return sub_def.param_types[0]
					return None
				if expr.op in (H.UnaryOp.NOT,):
					return checker._bool_type
				if expr.op is H.UnaryOp.NEG:
					if sub_ty in (checker._int_type, checker._float_type):
						return sub_ty
					return None
				if expr.op is H.UnaryOp.BIT_NOT:
					if sub_ty in (checker._uint_type, checker._uint64_type):
						return sub_ty
					return None
				return sub_ty

			if hasattr(H, "HCast") and isinstance(expr, getattr(H, "HCast")):
				te = getattr(expr, "target_type_expr", None)
				if te is not None:
					mod = getattr(getattr(self.current_fn, "signature", None), "module", None) if self.current_fn and self.current_fn.signature else None
					try:
						return checker._resolve_typeexpr(te, module_id=mod)
					except (KeyError, AttributeError, TypeError):
						pass
				return None

			if isinstance(expr, H.HArrayLiteral):
				if not expr.elements:
					self.report_empty_array_literal(getattr(expr, "loc", None))
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
						self.report_mixed_array_literal(getattr(expr, "loc", None))
						return self.table.new_array(checker._unknown_type)
				return self.table.new_array(first)

			if isinstance(expr, H.HField):
				subj_ty = None
				if expr.name in ("len", "cap", "capacity"):
					subj_ty = self._infer_expr_type(expr.subject)
				if subj_ty is None:
					subj_ty = self._infer_expr_type(expr.subject)
					if subj_ty is None:
						return None
					subj_def = self.table.get(subj_ty)
					if subj_def.kind is TypeKind.REF and subj_def.param_types:
						subj_ty = subj_def.param_types[0]
						subj_def = self.table.get(subj_ty)
					if subj_def.kind is TypeKind.STRUCT:
						info = self.table.struct_field(subj_ty, expr.name)
						if info is not None:
							_, field_ty = info
							return field_ty
					return None
				return checker._len_cap_result_type(subj_ty)

			if isinstance(expr, H.HIndex):
				if hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
					subject = expr.subject
					if (
						len(subject.projections) == 1
						and isinstance(subject.projections[0], H.HPlaceField)
						and subject.projections[0].name == "captures"
					):
						is_err = self._is_error_subject(subject.base)
						if is_err is None:
							return None
						if is_err:
							frame_ty = self._infer_expr_type(expr.index)
							if frame_ty is not None:
								frame_def = self.table.get(frame_ty)
								if frame_def.name != "String":
									self._append_diag(
										_chk_diag(
											message="Error.captures expects a String frame key",
											severity="error",
											span=None,
										)
									)
							return checker._dv
				if hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
					subject = expr.subject
					for idx, proj in enumerate(subject.projections):
						if not (isinstance(proj, H.HPlaceField) and proj.name == "captures"):
							continue
						if idx + 1 >= len(subject.projections) or not isinstance(subject.projections[idx + 1], H.HPlaceIndex):
							continue
						if idx + 2 != len(subject.projections):
							continue
						is_err = self._is_error_subject(subject.base)
						if is_err is None:
							return None
						if is_err:
							frame_ty = self._infer_expr_type(subject.projections[idx + 1].index)
							if frame_ty is not None:
								frame_def = self.table.get(frame_ty)
								if frame_def.name != "String":
									self._append_diag(
										_chk_diag(
											message="Error.captures expects a String frame key",
											severity="error",
											span=None,
										)
									)
							idx_ty = self._infer_expr_type(expr.index)
							if idx_ty is not None:
								idx_def = self.table.get(idx_ty)
								if idx_def.name != "String":
									self._append_diag(
										_chk_diag(
											message="Error.captures expects a String local key",
											severity="error",
											span=None,
										)
									)
							return checker._dv
				if (
					isinstance(expr.subject, H.HIndex)
					and isinstance(expr.subject.subject, H.HField)
					and expr.subject.subject.name == "captures"
				):
					is_err = self._is_error_subject(expr.subject.subject.subject)
					if is_err is None:
						return None
					if is_err:
						frame_ty = self._infer_expr_type(expr.subject.index)
						if frame_ty is not None:
							frame_def = self.table.get(frame_ty)
							if frame_def.name != "String":
								self._append_diag(
									_chk_diag(
										message="Error.captures expects a String frame key",
										severity="error",
										span=None,
									)
								)
						idx_ty = self._infer_expr_type(expr.index)
						if idx_ty is not None:
							idx_def = self.table.get(idx_ty)
							if idx_def.name != "String":
								self._append_diag(
									_chk_diag(
										message="Error.captures expects a String local key",
										severity="error",
										span=None,
									)
								)
						return checker._dv
				if isinstance(expr.subject, H.HField) and expr.subject.name == "captures":
					is_err = self._is_error_subject(expr.subject.subject)
					if is_err is None:
						return None
					if is_err:
						frame_ty = self._infer_expr_type(expr.index)
						if frame_ty is not None:
							frame_def = self.table.get(frame_ty)
							if frame_def.name != "String":
								self._append_diag(
									_chk_diag(
										message="Error.captures expects a String frame key",
										severity="error",
										span=None,
									)
								)
						# Intermediate map view (frame-selected captures) for outer index.
						return checker._dv
				if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
					is_err = self._is_error_subject(expr.subject.subject)
					if is_err is None:
						return None
					if is_err:
						idx_ty = self._infer_expr_type(expr.index)
						if idx_ty is not None:
							idx_def = self.table.get(idx_ty)
							if idx_def.name != "String":
								self.report_error_attr_key_not_string(getattr(expr.index, "loc", None))
						return checker._dv
				subject_ty = self._infer_expr_type(expr.subject)
				idx_ty = self._infer_expr_type(expr.index)
				if idx_ty is not None and idx_ty != checker._int_type:
					self.report_index_not_int(getattr(expr.index, "loc", None))
				if subject_ty is None:
					return None
				td = self.table.get(subject_ty)
				if td.kind is TypeKind.REF and td.param_types:
					td = self.table.get(td.param_types[0])
				if td.kind is TypeKind.ARRAY and td.param_types:
					elem_ty = td.param_types[0]
					elem_name = self.table.get(elem_ty).name
					copy_span = Span.from_loc(getattr(expr, "loc", None))
					if copy_span.line is None:
						copy_span = Span.from_loc(getattr(expr.subject, "loc", None))
					if copy_span.line is None:
						copy_span = Span.from_loc(getattr(expr.index, "loc", None))
					if copy_span.line is None and self.current_fn is not None and getattr(self.current_fn, "signature", None) is not None:
						copy_span = Span.from_loc(getattr(self.current_fn.signature, "loc", None))
					if id(expr) not in self.suppress_index_copy_check_expr_ids:
						copy_status = self.table.copy_status(elem_ty)
						if copy_status is None:
							reason = self.table.copy_unknown_reason(elem_ty)
							if _contains_type_params(elem_ty, set()):
								return elem_ty
							self._append_diag(
								_chk_diag(
									message=f"cannot copy value of type '{elem_name}': Copy is unknown ({reason})",
									severity="error",
									span=copy_span,
									code="E-COPY-UNKNOWN",
								)
							)
						elif not copy_status:
							self._append_diag(
								_chk_diag(
									message=f"cannot copy value of type '{elem_name}' (use move <expr>)",
									severity="error",
									span=copy_span,
								)
							)
					return elem_ty
				self.report_index_subject_not_array(getattr(expr.subject, "loc", None))
				return None
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				return self._infer_expr_type(expr.result)
			if hasattr(H, "HTryExpr") and isinstance(expr, getattr(H, "HTryExpr")):
				attempt_ty = self._infer_expr_type(expr.attempt)
				if attempt_ty is not None and self.table.is_void(attempt_ty):
					self._append_diag(
						_chk_diag(
							message="try/catch attempt must produce a value (not Void)",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return None

				if self.current_fn is not None:
					call_info_by_callsite_id = self.checker._call_info_by_callsite_id.get(self.current_fn.fn_id)
					attempt_may_throw = self.checker._expr_may_throw(
						expr.attempt,
						self.fn_infos,
						self.current_fn.fn_id,
						call_info_by_callsite_id,
						unknown_calls_throw=True,
						indexing_throws=True,
					)
					if not attempt_may_throw:
						def _has_call(node: "H.HExpr") -> bool:
							if isinstance(node, (H.HCall, H.HMethodCall, H.HInvoke)):
								return True
							if isinstance(node, H.HUnary):
								return _has_call(node.expr)
							if isinstance(node, H.HBinary):
								return _has_call(node.left) or _has_call(node.right)
							if isinstance(node, H.HTernary):
								return _has_call(node.cond) or _has_call(node.then_expr) or _has_call(node.else_expr)
							if isinstance(node, H.HField):
								return _has_call(node.subject)
							if isinstance(node, H.HIndex):
								return _has_call(node.subject) or _has_call(node.index)
							if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
								for proj in node.projections:
									if isinstance(proj, H.HPlaceIndex) and _has_call(proj.index):
										return True
								return False
							if isinstance(node, H.HArrayLiteral):
								return any(_has_call(el) for el in node.elements)
							if isinstance(node, H.HResultOk):
								return _has_call(node.value)
							if hasattr(H, "HMatchExpr") and isinstance(node, getattr(H, "HMatchExpr")):
								if _has_call(node.scrutinee):
									return True
								for arm in node.arms:
									if arm.result is not None and _has_call(arm.result):
										return True
								return False
							return False
						if _has_call(expr.attempt):
							return attempt_ty
						self._append_diag(
							_chk_diag(
								message="'try' applied to a nothrow expression (catch is unreachable)",
								severity="error",
								span=getattr(expr, "loc", Span()),
								notes=["expression contains no throwing operations"],
							)
						)
						return None

				seen_events: set[str] = set()
				catch_all_seen = False

				def arm_result_type(
					block: "H.HBlock", result_expr: Optional["H.HExpr"], arm_span: Span
				) -> Optional[TypeId]:
					for stmt in block.statements:
						if isinstance(stmt, (H.HReturn, H.HBreak, H.HContinue, H.HRethrow)):
							self._append_diag(
								_chk_diag(
									message=(
										"control-flow statement not allowed in try-expression catch block; "
										"use statement try { ... } catch { ... } instead"
									),
									severity="error",
									span=getattr(stmt, "loc", arm_span),
									code="E-TRYEXPR-CONTROLFLOW",
								)
							)
							return None
					if result_expr is None:
						self._append_diag(
							_chk_diag(
								message="catch arm must produce a value",
								severity="error",
								span=arm_span,
							)
						)
						return None
					return self._infer_expr_type(result_expr)

				result_ty = attempt_ty

				for arm in expr.arms:
					if arm.event_fqn is None:
						if catch_all_seen:
							self._append_diag(
								_chk_diag(
									message="catch-all must be the last catch arm",
									severity="error",
									span=arm.loc,
								)
							)
						catch_all_seen = True
					else:
						if arm.event_fqn in seen_events:
							self._append_diag(
								_chk_diag(
									message=f"duplicate catch arm for event {arm.event_fqn}",
									severity="error",
									span=arm.loc,
								)
							)
						if catch_all_seen:
							self._append_diag(
								_chk_diag(
									message="catch-all before this arm makes it unreachable",
									severity="error",
									span=arm.loc,
								)
							)
						seen_events.add(arm.event_fqn)

					prev = None
					if arm.binder:
						prev = self.locals.get(arm.binder)
						self.locals[arm.binder] = self.table.ensure_error()
					arm_ty = arm_result_type(arm.block, arm.result, arm.loc)
					if arm.binder:
						if prev is None:
							self.locals.pop(arm.binder, None)
						else:
							self.locals[arm.binder] = prev
					if result_ty is None and arm_ty is not None:
						result_ty = arm_ty
					elif result_ty is not None and arm_ty is not None and arm_ty != result_ty:
						expected_label = self.table.get(result_ty).name
						arm_label = self.table.get(arm_ty).name
						if attempt_ty is not None:
							message = f"catch arm type {arm_label} does not match attempt type {expected_label}"
						else:
							message = f"catch arm type {arm_label} does not match other catch arm type {expected_label}"
						self._append_diag(
							_chk_diag(
								message=message,
								severity="error",
								span=arm.loc,
							)
						)
				return result_ty

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
		fn_infos: Mapping[FunctionId, FnInfo],
		current_fn: Optional[FnInfo],
		diagnostics: Optional[List[Diagnostic]] = None,
		locals: Optional[Dict[str, TypeId]] = None,
	) -> Optional[TypeId]:
		ctx = self._TypingContext(
			checker=self,
			table=self._type_table,
			fn_infos=fn_infos,
			current_fn=current_fn,
			call_info_by_callsite_id=self._call_info_by_callsite_id.get(current_fn.fn_id) if current_fn else None,
			locals=locals if locals is not None else {},
			diagnostics=diagnostics,
			report_unknown_names=False,
		)
		return ctx.infer(expr)

	def _validate_calls(
		self,
		block: "H.HBlock",
		fn_infos: Mapping[FunctionId, FnInfo],
		diagnostics: List[Diagnostic],
		call_info_by_callsite_id: Mapping[int, CallInfo] | None,
		current_fn: Optional[FnInfo] = None,
	) -> None:
		"""
		Conservatively validate calls in a HIR block using FnSignature TypeIds.

		Currently enforces arity for HCall with HVar callee and attempts basic
		param-type equality when argument types are inferable (literals, simple
		calls, Result.Ok in a FnResult-returning function). Full expression
		typing is still deferred.
		"""
		from lang.driftc import stage1 as H

		def walk_expr(expr: H.HExpr) -> None:
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HLambda):
				lam = expr.fn
				if expr.kwargs:
					diagnostics.append(
						_chk_diag(
							message="lambda calls do not support keyword arguments",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
				if len(expr.args) != len(lam.params):
					diagnostics.append(
						_chk_diag(
							message=(
								f"lambda expects {len(lam.params)} arguments, got {len(expr.args)}"
							),
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
				self._infer_hir_expr_type(expr, fn_infos, current_fn, diagnostics)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, (H.HCall, H.HMethodCall, H.HInvoke)):
				if call_info_by_callsite_id is None:
					diagnostics.append(
						_chk_diag(
								message="typecheck contract failure: missing CallInfo map for call validation",
							severity="error",
							span=getattr(expr, "loc", None),
						)
					)
					return
				info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if info is None:
					note = None
					if isinstance(expr, H.HMethodCall):
						note = f"callsite_id={getattr(expr, 'callsite_id', None)} method={getattr(expr, 'method_name', None)}"
					elif isinstance(expr, H.HCall):
						note = f"callsite_id={getattr(expr, 'callsite_id', None)} call"
					elif isinstance(expr, H.HInvoke):
						note = f"callsite_id={getattr(expr, 'callsite_id', None)} invoke"
					if note is None:
						note = f"callsite_id={getattr(expr, 'callsite_id', None)}"
						diagnostics.append(_chk_diag(message="typecheck contract failure: missing CallInfo for call validation", severity="error", span=getattr(expr, "loc", None), notes=[note]))
					return
				info = self._repair_named_call_callinfo(expr, info, call_info_by_callsite_id)
				_sig_info = current_fn.signature if current_fn is not None else None
				_is_mir_bound = bool(_sig_info is not None and _sig_info.is_mir_bound)
				if _is_mir_bound and info.target.kind is CallTargetKind.TRAIT:
					msg = "typecheck contract failure: call resolved to trait target in typed mode"
					if isinstance(expr, H.HMethodCall):
						msg = "typecheck contract failure: method call resolved to trait target in typed mode"
					diagnostics.append(_chk_diag(message=msg, severity="error", span=getattr(expr, "loc", None)))
					return
				if not self._validate_callinfo_target_shape(expr, info, diagnostics):
					if isinstance(expr, H.HMethodCall):
						walk_expr(expr.receiver)
					elif isinstance(expr, H.HInvoke):
						walk_expr(expr.callee)
					for arg in expr.args:
						walk_expr(arg)
					for kw in getattr(expr, "kwargs", []) or []:
						walk_expr(kw.value)
					return
				if not self._validate_callinfo_param_layout(expr, info, diagnostics, current_fn=current_fn):
					if isinstance(expr, H.HMethodCall):
						walk_expr(expr.receiver)
					elif isinstance(expr, H.HInvoke):
						walk_expr(expr.callee)
					for arg in expr.args:
						walk_expr(arg)
					for kw in getattr(expr, "kwargs", []) or []:
						walk_expr(kw.value)
					return
				kwargs = getattr(expr, "kwargs", []) or []
				skip_type_check = bool(kwargs)
				arg_exprs = call_arg_exprs_for_param_layout(expr, info)
				arg_type_ids = [
					self._infer_hir_expr_type(a, fn_infos, current_fn, diagnostics) for a in arg_exprs
				]
				uses_forced = False
				for idx, arg_expr in enumerate(arg_exprs):
					forced = getattr(arg_expr, "force_inferred_type", None)
					if forced is None:
						continue
					uses_forced = True
					if idx < len(arg_type_ids) and arg_type_ids[idx] == self._unknown_type:
						arg_type_ids[idx] = forced
				if uses_forced:
					skip_type_check = True
				callee_name = None
				if info.target.kind is CallTargetKind.DIRECT and info.target.symbol is not None:
					callee_name = function_symbol(info.target.symbol)
				before_len = len(diagnostics)
				self.check_call_signature(
					info.sig,
					arg_type_ids,
					diagnostics,
					loc=Span.from_loc(getattr(expr, "loc", None)),
					callee_name=callee_name,
					skip_type_check=skip_type_check,
				)
				if len(diagnostics) > before_len:
					for d in diagnostics[before_len:]:
						if d.severity != "error":
							continue
						notes = list(d.notes or [])
						if current_fn is not None:
							notes.append(f"fn={function_symbol(current_fn.fn_id)}")
						notes.append(f"callsite_id={getattr(expr, 'callsite_id', None)}")
						if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
							notes.append(f"call_name={expr.fn.name}")
						elif isinstance(expr, H.HMethodCall):
							notes.append(f"method_name={expr.method_name}")
						d.notes = notes
				if isinstance(expr, H.HMethodCall):
					walk_expr(expr.receiver)
				elif isinstance(expr, H.HInvoke):
					walk_expr(expr.callee)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
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
			elif isinstance(expr, H.HTryExpr):
				walk_expr(expr.attempt)
				for arm in expr.arms:
					walk_block(arm.block)
					if getattr(arm, "result", None) is not None:
						walk_expr(arm.result)
			elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				walk_block(expr.body)
				walk_expr(expr.result)
			elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				walk_expr(expr.scrutinee)
				for arm in expr.arms:
					walk_block(arm.block)
					if getattr(arm, "result", None) is not None:
						walk_expr(arm.result)
			# literals/vars are leaf nodes

		def walk_block(b: H.HBlock) -> None:
			for stmt in b.statements:
				if isinstance(stmt, H.HReturn) and stmt.value is not None:
					walk_expr(stmt.value)
					continue
				if isinstance(stmt, H.HLocalConst):
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

	def _repair_named_call_callinfo(
		self,
		expr: "H.HExpr",
		info: CallInfo,
		call_info_by_callsite_id: Mapping[int, CallInfo] | None,
	) -> CallInfo:
		repaired = repair_named_hcall_callinfo(
			expr,
			info,
			self._signatures_by_id,
			verify_target_sig_match=False,
			allow_arity_fallback=False,
			preserve_instantiated_target=False,
			rewrite_sig_on_param_count_mismatch=False,
		)
		csid = getattr(expr, "callsite_id", None)
		if isinstance(csid, int) and isinstance(call_info_by_callsite_id, dict):
			call_info_by_callsite_id[csid] = repaired
		return repaired

	def _validate_callinfo_param_layout(
		self,
		expr: "H.HExpr",
		info: CallInfo,
		diagnostics: List[Diagnostic],
		*,
		current_fn: Optional[FnInfo] = None,
	) -> bool:
		from lang.driftc import stage1 as H
		for issue in call_contract_issues(expr, info):
			if issue.code == "E_CALLINFO_PARAM_LAYOUT" or issue.code == "E_CALLINFO_INCLUDES_CALLEE_INVALID":
				notes = list(issue.notes)
				if current_fn is not None:
					notes.append(f"fn={function_symbol(current_fn.fn_id)}")
				if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HVar):
					notes.append(f"call_name={expr.fn.name}")
				elif isinstance(expr, H.HMethodCall):
					notes.append(f"method_name={expr.method_name}")
				elif isinstance(expr, H.HInvoke):
					notes.append("invoke")
				diagnostics.append(
					_chk_diag(
						message=issue.message,
						severity="error",
						span=issue.span,
						notes=notes,
					)
				)
				return False
		return True

	def _validate_callinfo_target_shape(
		self,
		expr: "H.HExpr",
		info: CallInfo,
		diagnostics: List[Diagnostic],
	) -> bool:
		for issue in call_contract_issues(expr, info):
			if issue.code == "E_CALLINFO_INVOKE_TARGET_KIND" or issue.code == "E_CALLINFO_METHOD_CONSTRUCTOR_TARGET":
				diagnostics.append(
					_chk_diag(
						message=issue.message,
						severity="error",
						span=issue.span,
						notes=list(issue.notes),
					)
				)
				return False
		return True

	def _canonicalize_forward_nominal(self, ty: TypeId) -> TypeId:
		"""K26: resolve FORWARD_NOMINAL TypeId to its concrete struct/variant counterpart."""
		td = self._type_table.get(ty)
		if td.kind is not TypeKind.FORWARD_NOMINAL:
			return ty
		resolved = self._type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name) or self._type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
		if resolved is None or resolved == ty:
			resolved = self._type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=td.name) or self._type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=td.name)
		if resolved is None or resolved == ty:
			return ty
		if td.param_types:
			canon_args = [self._canonicalize_forward_nominal(a) for a in td.param_types]
			try:
				if resolved in self._type_table.struct_bases:
					return self._type_table.ensure_struct_instantiated(resolved, canon_args)
				if resolved in getattr(self._type_table, "variant_schemas", {}):
					return self._type_table.ensure_variant_instantiated(resolved, canon_args)
			except (ValueError, KeyError):
				return ty
		return resolved

	def check_call_signature(
		self,
		callee: FnInfo | FnSignature | "CallSig",
		arg_type_ids: list[Optional[TypeId]],
		diagnostics: List[Diagnostic],
		loc: Optional[Any] = None,
		*,
		callee_name: Optional[str] = None,
		skip_type_check: bool = False,
	) -> Optional[TypeId]:
		"""
		Best-effort call signature check using FnInfo/FnSignature + TypeIds.

		- Enforces arity when param_type_ids are available.
		- Performs simple TypeId equality checks for args when both sides are known.
		- Returns the callee return_type_id (may be None if unknown).

		Used by `_validate_calls` over HIR for shallow call checking.
		"""
		from lang.driftc.stage1.call_info import CallSig

		if isinstance(callee, CallSig):
			param_types = list(callee.param_types)
			ret_type = callee.user_ret_type
			if callee_name is None:
				callee_name = "<call>"
		else:
			sig = callee.signature if isinstance(callee, FnInfo) else callee
			if sig.param_type_ids is None or sig.return_type_id is None:
				return sig.return_type_id
			param_types = list(sig.param_type_ids)
			ret_type = sig.return_type_id
			if callee_name is None:
				callee_name = sig.name or "<call>"

		if len(arg_type_ids) != len(param_types):
			diagnostics.append(
				_chk_diag(
					message=(
						f"call to {callee_name} has {len(arg_type_ids)} arguments, "
						f"expected {len(param_types)}"
					),
					severity="error",
					span=loc,
				)
			)
			return ret_type

		# Generic signatures may still carry uninstantiated type params in the
		# stubbed pipeline, so avoid mismatched TypeId checks here.
		if skip_type_check:
			return ret_type

		if not isinstance(callee, CallSig):
			if getattr(sig, "type_params", None) or getattr(sig, "impl_type_params", None):
				return sig.return_type_id

		for idx, (arg_ty, param_ty) in enumerate(zip(arg_type_ids, param_types)):
			if arg_ty is None or param_ty is None:
				continue
			if arg_ty == self._unknown_type or param_ty == self._unknown_type:
				continue
			if arg_ty != param_ty:
				try:
					arg_def = self._type_table.get(arg_ty)
					param_def = self._type_table.get(param_ty)
				except Exception:
					arg_def = None
					param_def = None
				# K26: FORWARD_NOMINAL types from package imports may differ in TypeId
				# from their concrete struct/variant counterparts but represent the same
				# type.  Resolve both sides and compare structurally.
				if arg_def is not None and param_def is not None:
					_a = arg_ty
					_p = param_ty
					if arg_def.kind is TypeKind.FORWARD_NOMINAL:
						_a = self._canonicalize_forward_nominal(arg_ty)
					if param_def.kind is TypeKind.FORWARD_NOMINAL:
						_p = self._canonicalize_forward_nominal(param_ty)
					if _a == _p:
						continue
				# Implicit reborrow: allow &mut T where &T is expected.
				if (
					arg_def is not None and param_def is not None
					and arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
					and arg_def.ref_mut is True and param_def.ref_mut is False
					and arg_def.param_types and param_def.param_types
					and arg_def.param_types[0] == param_def.param_types[0]
				):
					continue
				if param_def is not None and param_def.kind is TypeKind.INTERFACE:
					# Owned interface coercion: concrete implementation values are
					# coercible to interface-typed parameters.
					if arg_def is not None and arg_def.kind is not TypeKind.INTERFACE:
						continue
					# Interface upcast/retarget is allowed when destination is a view
					# of the source interface value.
					if arg_def is not None and arg_def.kind is TypeKind.INTERFACE:
						try:
							param_inst = self._type_table.get_interface_instance(param_ty)
							param_base = param_inst.base_id if param_inst is not None else param_ty
							view_map = self._type_table.interface_instance_view_map(arg_ty)
							if view_map.get(param_base) == param_ty:
								continue
						except Exception:
							pass
				diagnostics.append(
					_chk_diag(
						message=(
							f"argument {idx} to {callee_name} has type {self._type_label(arg_ty)}, "
							f"expected {self._type_label(param_ty)}"
						),
						severity="error",
						span=loc,
					)
				)
		return ret_type

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

	def _resolve_typeexpr(self, raw: object, *, module_id: str | None = None) -> TypeId:
		"""
		Map a parser TypeExpr-like object (name/args) or simple string/tuple into a
		TypeId using the shared TypeTable. This mirrors the resolver and is used for
		declared local types. The len/cap rule (Array/String → Int) is centralized
		in the type resolver; this helper simply resolves declared type names.
		"""
		return resolve_opaque_type(raw, self._type_table, module_id=module_id)

	def _len_cap_result_type(self, subj_ty: TypeId) -> Optional[TypeId]:
		"""Return Int when length/capacity is requested on Array or String; otherwise None."""
		td = self._type_table.get(subj_ty)
		if td.kind is TypeKind.ARRAY or (td.kind is TypeKind.SCALAR and td.name == "String"):
			return self._type_table.ensure_int()
		return None

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
		from lang.driftc import stage1 as H
		from contextlib import contextmanager

		@contextmanager
		def _scoped_locals():
			saved = dict(ctx.locals)
			try:
				yield
			finally:
				ctx.locals = saved

		def walk_expr(expr: H.HExpr) -> None:
			# Run inference for all expressions up front so shared diagnostics fire
			# even when no specific validator hook is registered for that node.
			ctx.infer(expr)
			if on_expr:
				on_expr(expr, ctx)

			if isinstance(expr, H.HCall):
				# Do not infer plain callee vars here; unresolved-call diagnostics are
				# produced via CallInfo/call validation, and walking callee HVar nodes
				# would misclassify prelude/builtin callable names as unknown locals.
				if not isinstance(expr.fn, H.HVar):
					walk_expr(expr.fn)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
			elif isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
			elif isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
			elif isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
			elif isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
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
			elif hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				# `match` is an expression; traverse scrutinee and then each arm with
				# a scoped view of locals that includes any pattern binders.
				walk_expr(expr.scrutinee)
				for arm in expr.arms:
					with _scoped_locals():
						# If the checker has normalized binder field indices, use scrutinee
						# type to seed binder types. This keeps type inference for arm
						# expressions meaningful for downstream validators.
						scrut_ty = ctx.infer(expr.scrutinee)
						inst = None
						scrut_ref_mut: bool | None = None
						if scrut_ty is not None:
							scrut_def = ctx.table.get(scrut_ty)
							if scrut_def.kind is TypeKind.REF and scrut_def.param_types:
								inner = scrut_def.param_types[0]
								inner_def = ctx.table.get(inner)
								if inner_def.kind is TypeKind.VARIANT:
									inst = ctx.table.get_variant_instance(inner)
									scrut_ref_mut = bool(scrut_def.ref_mut)
							elif scrut_def.kind is TypeKind.VARIANT:
								inst = ctx.table.get_variant_instance(scrut_ty)
						if inst is not None and arm.ctor is not None:
							arm_def = inst.arms_by_name.get(arm.ctor)
						else:
							arm_def = None

						field_indices = list(getattr(arm, "binder_field_indices", []) or [])
						for idx, bname in enumerate(getattr(arm, "binders", []) or []):
							bty = self._unknown_type
							if arm_def is not None and idx < len(field_indices):
								fidx = field_indices[idx]
								if 0 <= fidx < len(arm_def.field_types):
									bty = arm_def.field_types[fidx]
									if scrut_ref_mut is not None:
										bty = ctx.table.ensure_ref_mut(bty) if scrut_ref_mut else ctx.table.ensure_ref(bty)
							ctx.locals[bname] = bty

						prev_report_unknown = ctx.report_unknown_names
						ctx.report_unknown_names = False
						try:
							walk_block(arm.block)
							if getattr(arm, "result", None) is not None:
								walk_expr(arm.result)
						finally:
							ctx.report_unknown_names = prev_report_unknown
			elif hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				with _scoped_locals():
					walk_block(expr.body)
					walk_expr(expr.result)
			# literals/vars are leaf nodes

		def walk_stmt(stmt: H.HStmt) -> None:
			if on_stmt:
				on_stmt(stmt, ctx)
			if isinstance(stmt, H.HLocalConst):
				# Local const: validate initializer is a compile-time literal,
				# register binding type. No storage is allocated; each use site
				# re-materializes the value as a MIR literal.
				walk_expr(stmt.value)
				decl_ty = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					mod = getattr(ctx.current_fn.signature, "module", None) if ctx.current_fn and ctx.current_fn.signature else None
					decl_ty = self._resolve_typeexpr(stmt.declared_type_expr, module_id=mod)
					if decl_ty is not None and stmt.binding_id is not None:
						ctx.locals[int(stmt.binding_id)] = decl_ty
				# Validate the initializer is a compile-time literal.
				val = _eval_hir_const_value(stmt.value)
				if val is None:
					ctx._append_diag(
						_chk_diag(
							message=(
								f"const '{stmt.name}' initializer must be a compile-time literal in v1 "
								"(Int/Uint/Bool/String/Float, optionally with unary '+' or '-')"
							),
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				elif decl_ty is not None:
					ok, val, err = _validate_const(self._type_table, stmt.name, decl_ty, val)
					if not ok:
						ctx._append_diag(_chk_diag(message=err, severity="error", span=getattr(stmt, "loc", Span())))
				return
			elif isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
			elif isinstance(stmt, H.HLet):
				walk_expr(stmt.value)
				decl_ty: Optional[TypeId] = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					mod = getattr(ctx.current_fn.signature, "module", None) if ctx.current_fn and ctx.current_fn.signature else None
					decl_ty = self._resolve_typeexpr(stmt.declared_type_expr, module_id=mod)
				value_ty = ctx.infer(stmt.value)

				def _iface_assignable(src: TypeId, dst: TypeId) -> bool:
					if src == dst:
						return True
					src_def = self._type_table.get(src)
					dst_def = self._type_table.get(dst)
					if src_def.kind is not TypeKind.INTERFACE or dst_def.kind is not TypeKind.INTERFACE:
						return False
					try:
						dst_inst = self._type_table.get_interface_instance(dst)
						dst_base = dst_inst.base_id if dst_inst is not None else dst
						view_map = self._type_table.interface_instance_view_map(src)
						return view_map.get(dst_base) == dst
					except Exception:
						return False

				# MVP: allow `Uint` locals to be initialized from an integer literal.
				if (
					decl_ty is not None
					and decl_ty == self._type_table.ensure_uint()
					and isinstance(stmt.value, H.HLiteralInt)
				):
					value_ty = decl_ty
				if decl_ty is not None and value_ty is not None and decl_ty != value_ty:
					if self._type_table.get(decl_ty).kind is TypeKind.INTERFACE:
						if self._type_table.get(value_ty).kind is TypeKind.INTERFACE:
							if _iface_assignable(value_ty, decl_ty):
								ctx.locals[stmt.name] = decl_ty
								return
						else:
							ctx.locals[stmt.name] = decl_ty
							return
					ctx._append_diag(
						_chk_diag(
							message="let-binding type does not match declared type",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				ctx.locals[stmt.name] = decl_ty or value_ty or self._unknown_type
			elif isinstance(stmt, H.HAssign):
				walk_expr(stmt.value)
				value_ty = ctx.infer(stmt.value)
				if isinstance(stmt.target, H.HIndex):
					ctx.suppress_index_copy_check_expr_ids.add(id(stmt.target))
					try:
						walk_expr(stmt.target)
						target_ty = ctx.infer(stmt.target)
					finally:
						ctx.suppress_index_copy_check_expr_ids.discard(id(stmt.target))
					if target_ty is not None and value_ty is not None and target_ty != value_ty:
						ctx._append_diag(
							_chk_diag(
								message="assignment type mismatch for indexed array element",
								severity="error",
								span=getattr(stmt.target, "loc", getattr(stmt, "loc", Span())),
							)
						)
				else:
					walk_expr(stmt.target)
				if isinstance(stmt.target, H.HVar) and value_ty is not None:
					ctx.locals[stmt.target.name] = value_ty
			elif isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				with _scoped_locals():
					walk_block(stmt.then_block)
				if stmt.else_block:
					with _scoped_locals():
						walk_block(stmt.else_block)
			elif hasattr(H, "HMatchExpr") and isinstance(stmt, getattr(H, "HMatchExpr")):
				# Defensive: match is an expression node; it should appear under
				# HExprStmt, but allow traversal if a legacy shape places it as a stmt.
				walk_expr(stmt)
			elif isinstance(stmt, H.HLoop):
				with _scoped_locals():
					walk_block(stmt.body)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					walk_expr(stmt.value)
			elif isinstance(stmt, H.HThrow):
				walk_expr(stmt.value)
			elif isinstance(stmt, H.HTry):
				with _scoped_locals():
					walk_block(stmt.body)
				for arm in stmt.catches:
					with _scoped_locals():
						if arm.binder:
							ctx.locals[arm.binder] = self._type_table.ensure_error()
						walk_block(arm.block)
			elif isinstance(stmt, H.HBlock):
				with _scoped_locals():
					walk_block(stmt)
			elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				with _scoped_locals():
					walk_block(stmt.block)
			elif isinstance(stmt, H.HAugAssign):
				walk_expr(stmt.value)
			elif hasattr(H, "HAssert") and isinstance(stmt, getattr(H, "HAssert")):
				walk_expr(stmt.cond)
				if stmt.msg is not None:
					walk_expr(stmt.msg)
			# HBreak/HContinue/HRethrow carry no expressions.

		def walk_block(hb: H.HBlock) -> None:
			for stmt in hb.statements:
				walk_stmt(stmt)

		walk_block(block)

	def _match_validator_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""
		Normalize and validate `match` constructor patterns.

		Stage2 lowering is assert-only for pattern→field mapping: any constructor
		arm that binds fields must carry `binder_field_indices` computed from the
		scrutinee's concrete variant instance. This validator is the stub-checker
		owner of that normalization so the pipeline stays robust even before the
		full typed checker is the only frontend.
		"""
		from lang.driftc import stage1 as H

		if not (hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr"))):
			return

		scrut_ty = ctx.infer(expr.scrutinee)
		if scrut_ty is None:
			needs_indices = False
			for arm in expr.arms:
				binders = list(getattr(arm, "binders", []) or [])
				if not binders:
					continue
				field_indices = list(getattr(arm, "binder_field_indices", []) or [])
				if len(field_indices) < len(binders):
					needs_indices = True
					break
			if not needs_indices:
				return
			ctx._append_diag(
				_chk_diag(
					message="match scrutinee type is unknown",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return
		scrut_def = ctx.table.get(scrut_ty)
		if scrut_def.kind is TypeKind.REF and scrut_def.param_types:
			scrut_ty = scrut_def.param_types[0]
			scrut_def = ctx.table.get(scrut_ty)
		if scrut_def.kind is not TypeKind.VARIANT:
			ctx._append_diag(
				_chk_diag(
					message="match scrutinee must have a variant type",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return

		inst = ctx.table.get_variant_instance(scrut_ty)
		if inst is None:
			ctx._append_diag(
				_chk_diag(
					message="match scrutinee variant instance is missing",
					severity="error",
					span=getattr(expr, "loc", Span()),
				)
			)
			return

		seen_ctors: set[str] = set()
		default_seen = False
		for arm in getattr(expr, "arms", []) or []:
			# Default arm.
			if arm.ctor is None:
				default_seen = True
				continue
			if default_seen:
				ctx._append_diag(
					_chk_diag(
						message="match arm after default is unreachable",
						severity="error",
						span=getattr(arm, "loc", getattr(expr, "loc", Span())),
					)
				)
			if arm.ctor in seen_ctors:
				ctx._append_diag(
					_chk_diag(
						message=f"duplicate match arm for constructor '{arm.ctor}'",
						severity="error",
						span=getattr(arm, "loc", getattr(expr, "loc", Span())),
					)
				)
			seen_ctors.add(arm.ctor)

			arm_def = inst.arms_by_name.get(arm.ctor)
			if arm_def is None:
				ctx._append_diag(
					_chk_diag(
						message=f"unknown constructor '{arm.ctor}' in match",
						severity="error",
						span=getattr(arm, "loc", getattr(expr, "loc", Span())),
					)
				)
				continue

			form = getattr(arm, "pattern_arg_form", "positional")
			field_indices: list[int] = []

			if form == "bare":
				# Bare ctor patterns are allowed only for zero-field constructors.
				if arm_def.field_types:
					ctx._append_diag(
						_chk_diag(
							message=(
								"E-MATCH-PAT-BARE: bare constructor pattern is only allowed for zero-field constructors; "
								f"use '{arm.ctor}()' to ignore payload"
							),
							severity="error",
							span=getattr(arm, "loc", getattr(expr, "loc", Span())),
						)
					)
			elif form == "paren":
				if arm.binders:
					ctx._append_diag(
						_chk_diag(
							message=f"constructor pattern '{arm.ctor}()' must not bind fields",
							severity="error",
							span=getattr(arm, "loc", getattr(expr, "loc", Span())),
						)
					)
			elif form == "named":
				field_names = list(getattr(arm_def, "field_names", []) or [])
				field_types = list(getattr(arm_def, "field_types", []) or [])
				binder_fields = getattr(arm, "binder_fields", None)
				if binder_fields is None:
					ctx._append_diag(
						_chk_diag(
							message=f"named constructor pattern '{arm.ctor}' is missing field names",
							severity="error",
							span=getattr(arm, "loc", getattr(expr, "loc", Span())),
						)
					)
				else:
					seen_fields: set[str] = set()
					for fname in binder_fields:
						if fname in seen_fields:
							ctx._append_diag(
								_chk_diag(
									message=f"duplicate field '{fname}' in constructor pattern '{arm.ctor}'",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
							continue
						seen_fields.add(fname)
						if fname not in field_names:
							ctx._append_diag(
								_chk_diag(
									message=(
										f"unknown field '{fname}' in constructor pattern '{arm.ctor}'; "
										f"available fields: {', '.join(field_names)}"
									),
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
							continue
						field_indices.append(field_names.index(fname))
					# Typecheck-stage normalization: store mapping only when lengths align.
					if len(binder_fields) != len(arm.binders):
						ctx._append_diag(
							_chk_diag(
								message=f"constructor pattern '{arm.ctor}' expects {len(binder_fields)} binders, got {len(arm.binders)}",
								severity="error",
								span=getattr(arm, "loc", Span()),
							)
						)
						field_indices = []
			else:
				# Positional binders (exact arity in v1).
				field_types = list(getattr(arm_def, "field_types", []) or [])
				if len(arm.binders) != len(field_types):
					ctx._append_diag(
						_chk_diag(
							message=f"constructor pattern '{arm.ctor}' expects {len(field_types)} binders, got {len(arm.binders)}",
							severity="error",
							span=getattr(arm, "loc", getattr(expr, "loc", Span())),
						)
					)
				else:
					field_indices = list(range(len(arm.binders)))

			# Store normalized binder→field-index mapping for stage2 lowering.
			arm.binder_field_indices = list(field_indices)

	def _array_validator_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""Trigger array literal/index inference to surface diagnostics."""
		from lang.driftc import stage1 as H

		if isinstance(expr, (H.HArrayLiteral, H.HIndex)):
			tid = ctx.infer(expr)
			if isinstance(expr, H.HArrayLiteral) and expr.elements and tid is not None:
				elem_ty = ctx.table.get(tid).param_types[0] if ctx.table.get(tid).param_types else None
				if drift_debug.enabled("array_literal_ty"):
					if elem_ty is None:
						print(f"[drift:debug][array_literal_ty] array_literal tid={tid} elem_ty=None", file=sys.stderr)
					else:
						td = ctx.table.get(elem_ty)
						inst_note = ""
						if td.kind is TypeKind.STRUCT:
							inst = ctx.table.get_struct_instance(elem_ty)
							if inst is None:
								inst_note = " inst=None"
							else:
								inst_note = f" inst_fields={inst.field_types}"
						if td.kind is TypeKind.VARIANT:
							inst = ctx.table.get_variant_instance(elem_ty)
							if inst is None:
								inst_note = " inst=None"
							else:
								inst_note = f" inst_arms={[list(arm.field_types) for arm in inst.arms]}"
						print(f"[drift:debug][array_literal_ty] array_literal tid={tid} elem_ty={elem_ty} kind={td.kind.name} name={td.name} module={td.module_id}{inst_note}", file=sys.stderr)
				if elem_ty is not None:
					copy_status = ctx.table.copy_status(elem_ty)
					if copy_status is None:
						reason = ctx.table.copy_unknown_reason(elem_ty)
						ctx._append_diag(
							_chk_diag(
								message=f"array literal element type Copy proof is unknown ({reason})",
								severity="error",
								span=getattr(expr, "loc", Span()),
								code="E-ARRAY-LITERAL-COPY-UNKNOWN",
							)
						)
					elif not copy_status:
						ctx._append_diag(
							_chk_diag(
								message="array literal element type must be Copy",
								severity="error",
								span=getattr(expr, "loc", Span()),
								code="E-ARRAY-LITERAL-NON-COPY",
							)
						)

	def _bitwise_validator_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""
		Validate bitwise operators and shifts.

		MVP rule: `~`, `&`, `|`, `^`, `<<`, `>>` require `Uint` or `Uint64` operands.
		"""
		from lang.driftc import stage1 as H

		uint_ty = self._uint_type

		if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.BIT_NOT:
			inner_ty = ctx.infer(expr.expr)
			if inner_ty is not None and inner_ty not in (uint_ty, self._uint64_type):
				ctx._append_diag(
					_chk_diag(
						message="bitwise operators require Uint or Uint64 operands",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
			return

		if isinstance(expr, H.HBinary) and expr.op in (
			H.BinaryOp.BIT_AND,
			H.BinaryOp.BIT_OR,
			H.BinaryOp.BIT_XOR,
			H.BinaryOp.SHL,
			H.BinaryOp.SHR,
		):
			left_ty = ctx.infer(expr.left)
			right_ty = ctx.infer(expr.right)
			if left_ty is not None and right_ty is not None and not (left_ty == right_ty and left_ty in (uint_ty, self._uint64_type)):
				ctx._append_diag(
					_chk_diag(
						message="bitwise operators require Uint or Uint64 operands",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
			return

	def _void_usage_on_expr(self, expr: "H.HExpr", ctx: "_TypingContext") -> None:
		"""
		Forbid Void where a value is required (binary ops, call args, unary ops).

		Expression statements are allowed to discard Void-returning calls; that
		expr-stmt special-case is enforced in the statement validator.
		"""
		from lang.driftc import stage1 as H

		def is_void(tid: TypeId | None) -> bool:
			return tid is not None and self._type_table.is_void(tid)

		if isinstance(expr, H.HBinary):
			left_ty = ctx.infer(expr.left)
			right_ty = ctx.infer(expr.right)
			if is_void(left_ty) or is_void(right_ty):
				ctx._append_diag(
					_chk_diag(
						message="Void value is not allowed in a binary operation",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
			return

		if isinstance(expr, H.HUnary):
			sub_ty = ctx.infer(expr.expr)
			if is_void(sub_ty):
				ctx._append_diag(
					_chk_diag(
						message="Void value is not allowed in a unary operation",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
			return

		if isinstance(expr, H.HTernary):
			then_ty = ctx.infer(expr.then_expr)
			else_ty = ctx.infer(expr.else_expr)
			if is_void(then_ty) or is_void(else_ty):
				ctx._append_diag(
					_chk_diag(
						message="Void value is not allowed in a ternary expression",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
			return

		if isinstance(expr, H.HCall):
			call_info = None
			if ctx.call_info_by_callsite_id is not None:
				call_info = ctx.call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
			param_types = list(call_info.sig.param_types) if call_info is not None else []
			for idx, arg in enumerate(expr.args):
				arg_ty = ctx.infer(arg)
				if is_void(arg_ty):
					expected = param_types[idx] if idx < len(param_types) else None
					if expected is not None and is_void(expected):
						continue
					ctx._append_diag(
						_chk_diag(
							message="Void value is not allowed as a function argument",
							severity="error",
							span=getattr(arg, "loc", getattr(expr, "loc", Span())),
						)
					)
			for kw in getattr(expr, "kwargs", []) or []:
				arg_ty = ctx.infer(kw.value)
				if is_void(arg_ty):
					# Keyword args are not mapped to params here; keep strict unless
					# call info is missing.
					ctx._append_diag(
						_chk_diag(
							message="Void value is not allowed as a function argument",
							severity="error",
							span=getattr(kw.value, "loc", getattr(expr, "loc", Span())),
						)
					)
			return

		if isinstance(expr, H.HMethodCall):
			call_info = None
			if ctx.call_info_by_callsite_id is not None:
				call_info = ctx.call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
			param_types = list(call_info.sig.param_types) if call_info is not None else []
			offset = 1 if len(param_types) == len(expr.args) + 1 else 0
			for idx, arg in enumerate(expr.args):
				arg_ty = ctx.infer(arg)
				if is_void(arg_ty):
					expected_idx = idx + offset
					expected = param_types[expected_idx] if expected_idx < len(param_types) else None
					if expected is not None and is_void(expected):
						continue
					ctx._append_diag(
						_chk_diag(
							message="Void value is not allowed as a method argument",
							severity="error",
							span=getattr(arg, "loc", getattr(expr, "loc", Span())),
						)
					)
			for kw in getattr(expr, "kwargs", []) or []:
				arg_ty = ctx.infer(kw.value)
				if is_void(arg_ty):
					ctx._append_diag(
						_chk_diag(
							message="Void value is not allowed as a method argument",
							severity="error",
							span=getattr(kw.value, "loc", getattr(expr, "loc", Span())),
						)
					)
			return

		if isinstance(expr, H.HArrayLiteral):
			for el in expr.elements:
				el_ty = ctx.infer(el)
				if is_void(el_ty):
					ctx._append_diag(
						_chk_diag(
							message="Void value is not allowed in an array literal",
							severity="error",
							span=getattr(el, "loc", getattr(expr, "loc", Span())),
						)
					)
					break
			return

	def _bool_validator_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""Require Boolean conditions when the type is known."""
		from lang.driftc import stage1 as H

		if isinstance(stmt, H.HIf):
			cond_ty = ctx.infer(stmt.cond)
			if cond_ty is not None and cond_ty != self._bool_type:
				ctx._append_diag(
					_chk_diag(
						message="if condition must be Bool",
						severity="error",
						span=getattr(stmt.cond, "loc", getattr(stmt, "loc", Span())),
					)
				)

	def _void_rules_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""
		Enforce Void placement rules on statements:
		- Void return type: only bare `return` allowed.
		- Non-void return type: must return a value.
		- Void cannot be stored in let/assign or declared explicitly.
		"""
		from lang.driftc import stage1 as H

		def is_void(tid: TypeId | None) -> bool:
			return tid is not None and self._type_table.is_void(tid)

		if isinstance(stmt, H.HReturn):
			ret_tid = None
			if ctx.current_fn and ctx.current_fn.signature:
				ret_tid = ctx.current_fn.signature.return_type_id
			fn_is_void = is_void(ret_tid)
			if stmt.value is None and ret_tid is not None and not fn_is_void:
				ctx._append_diag(
					_chk_diag(
						message="non-void function must return a value",
						severity="error",
						span=getattr(stmt, "loc", Span()),
					)
				)
			if stmt.value is not None and fn_is_void:
				val_tid = ctx.infer(stmt.value)
				if val_tid is None or is_void(val_tid):
					return
				ctx._append_diag(
					_chk_diag(
						message="cannot return a value from a Void function",
						severity="error",
						span=getattr(stmt.value, "loc", getattr(stmt, "loc", Span())),
					)
				)
			return

		if isinstance(stmt, H.HLocalConst):
			# Local const: the main typed walk_stmt handles validation.
			# Here we just infer the value type so any sub-expression
			# diagnostics fire.
			ctx.infer(stmt.value)
			return

		if isinstance(stmt, H.HLet):
			decl_ty = None
			if getattr(stmt, "declared_type_expr", None) is not None:
				mod = getattr(ctx.current_fn.signature, "module", None) if ctx.current_fn and ctx.current_fn.signature else None
				decl_ty = self._resolve_typeexpr(stmt.declared_type_expr, module_id=mod)
			if is_void(decl_ty):
				ctx._append_diag(
					_chk_diag(
						message="cannot declare a binding of type Void",
						severity="error",
						span=getattr(stmt, "loc", Span()),
					)
				)
			val_ty = None
			if isinstance(stmt.value, H.HArrayLiteral) and not stmt.value.elements and decl_ty is not None:
				decl_def = self._type_table.get(decl_ty)
				if decl_def.kind is TypeKind.ARRAY:
					ctx.cache[id(stmt.value)] = decl_ty
					val_ty = decl_ty
			if val_ty is None:
				val_ty = ctx.infer(stmt.value)
			return

		if isinstance(stmt, H.HAssign):
			val_ty = ctx.infer(stmt.value)
			return

	def _exception_init_rules_on_stmt(self, stmt: "H.HStmt", ctx: "_TypingContext") -> None:
		"""
		Enforce exception constructor argument rules for `throw`.

		Surface syntax uses constructor-call form only:
		  throw E(...)

		Argument mapping rules:
		- positional arguments map to declared exception fields in declaration order
		- keyword arguments fill remaining fields
		- exact coverage required (no missing/unknown/duplicates)

		Attribute payload rule (Phase 2, MVP):
		- exception field values must implement Diagnostic; primitive literals
		  are allowed and wrapped into DiagnosticValue during lowering.
		"""
		from lang.driftc import stage1 as H
		from lang.driftc.core.exception_ctor_args import KwArg as _KwArg, resolve_exception_ctor_args

		schemas: dict[str, tuple[str, list[str]]] = getattr(self._type_table, "exception_schemas", {})

		def _schema(name: str) -> tuple[str, list[str]] | None:
			return schemas.get(name)

		if not isinstance(stmt, H.HThrow):
			return

		if isinstance(stmt.value, H.HExceptionInit):
			schema = _schema(stmt.value.event_fqn)
			schema_fields: list[str] | None
			if schema is None:
				schema_fields = None
			else:
				_decl_fqn, schema_fields = schema

			resolved, diags = resolve_exception_ctor_args(
				event_fqn=stmt.value.event_fqn,
				declared_fields=schema_fields,
				pos_args=[(a, getattr(a, "loc", Span())) for a in stmt.value.pos_args],
				kw_args=[
					_KwArg(name=kw.name, value=kw.value, name_span=getattr(kw, "loc", Span()))
					for kw in stmt.value.kw_args
				],
				span=getattr(stmt.value, "loc", Span()),
			)
			for d in diags:
				ctx._append_diag(d)

			values_to_validate = [v for _name, v in resolved]
			if schema_fields is None:
				values_to_validate = list(stmt.value.pos_args) + [kw.value for kw in stmt.value.kw_args]

			for fexpr in values_to_validate:
				if isinstance(fexpr, H.HMove):
					fexpr_check = fexpr.subject
					if hasattr(H, "HPlaceExpr") and isinstance(fexpr_check, getattr(H, "HPlaceExpr")):
						fexpr_check = fexpr_check.base
				else:
					fexpr_check = fexpr
				if isinstance(fexpr_check, H.HDVInit):
					continue
				if isinstance(fexpr_check, (H.HLiteralInt, H.HLiteralBool)):
					continue
				if hasattr(H, "HLiteralString") and isinstance(fexpr_check, getattr(H, "HLiteralString")):
					continue
				val_ty = ctx.infer(fexpr_check)
				if val_ty is None:
					continue
				val_nom_ty = val_ty
				td = ctx.table.get(val_nom_ty)
				if td.kind is TypeKind.REF and td.param_types:
					val_nom_ty = td.param_types[0]
				if val_nom_ty is not None and ctx.table.is_diagnostic(val_nom_ty):
					continue
				ctx._append_diag(
					_chk_diag(
						message="exception field value must implement Diagnostic",
						severity="error",
						span=getattr(fexpr, "loc", getattr(stmt.value, "loc", Span())),
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
		ssa_funcs: Mapping[FunctionId, "SsaFunc"],
		signatures: Mapping[FunctionId, FnSignature],
		*,
		can_throw_by_id: Mapping[FunctionId, bool] | None = None,
		diagnostics: list[Diagnostic] | None = None,
	) -> Optional["CheckerTypeEnv"]:
		"""
		Assign TypeIds to SSA values using checker signatures and simple heuristics.

		This is a minimal pass: it handles constants, ConstructResultOk/Err, Call,
		AssignSSA copies, UnaryOp/BinaryOp propagation, and Phi when incoming types
		agree. Unknowns default to `Unknown` TypeId.

		Can-throw note (important for long-term correctness):
		- `FnSignature.declared_can_throw` is an *explicitness* signal (surface
		  language intent), not the checker's effective "this function may throw"
		  result.
		- The stub checker computes the effective can-throw bit per function and
		  stores it on `FnInfo.declared_can_throw`.
		- Typed SSA interpretation (for throw checks and LLVM lowering) needs the
		  *effective* can-throw bit so calls/returns are typed as the internal ABI
		  `FnResult<T, Error>` only when a function actually can throw.

		Pass `can_throw_by_id={FunctionId: bool}` (usually from `FnInfo`) to keep
		type inference aligned with the checker without mutating signatures.

		Returns None if no types were assigned.
		"""
		self._ensure_core_types()
		from lang.driftc.checker.type_env_impl import CheckerTypeEnv
		from lang.driftc.stage2 import (
			LoadLocal,
			StoreLocal,
			AddrOfLocal,
			AddrOfField,
			LoadRef,
			StoreRef,
			ConstructStruct,
			ConstructIface,
			StructGetField,
			ConstructVariant,
			VariantTag,
			VariantGetField,
			ConstructResultOk,
			ConstructResultErr,
			ResultIsErr,
			ResultOk,
			ResultErr,
			Call,
			CallIndirect,
			CallIface,
			FnPtrConst,
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
			ArrayAlloc,
			ArraySetLen,
			StringLen,
			StringByteAt,
		)
		value_types: Dict[tuple[FunctionId, str], TypeId] = {}
		reported_return_mismatch: set[tuple[FunctionId, str]] = set()

		# Helper to fetch a mapped type with Unknown fallback.
		def ty_for(fn: FunctionId, val: str) -> TypeId:
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
		for fn_id, ssa in ssa_funcs.items():
			sig = signatures.get(fn_id)
			if sig and sig.param_type_ids and ssa.func.params:
				if len(ssa.func.params) != len(sig.param_type_ids) and diagnostics is not None:
					diagnostics.append(
						_chk_diag(
							message=(
									"typecheck contract failure: SSA param arity does not match signature for "
								f"{function_symbol(fn_id)} (ssa {len(ssa.func.params)} vs sig {len(sig.param_type_ids)})"
							),
							severity="error",
						)
					)
				else:
					for param_name, ty_id in zip(ssa.func.params, sig.param_type_ids):
						if ty_id is not None:
							value_types[(fn_id, param_name)] = ty_id

		changed = True
		# Fixed-point with a small iteration cap across all functions.
		for _ in range(5):
			if not changed:
				break
			changed = False
			for fn_id, ssa in ssa_funcs.items():
				sig = signatures.get(fn_id)
				fn_return_parts: tuple[TypeId, TypeId] | None = None
				fn_is_void = False

				# Decide can-throw for typing purposes.
				# - Prefer the checker-provided effective mapping when available.
				# - Otherwise fall back to any explicit signature flag (legacy).
				if can_throw_by_id is not None:
					if fn_id in can_throw_by_id:
						fn_is_can_throw = can_throw_by_id[fn_id]
					else:
						if diagnostics is not None:
							diagnostics.append(
								_chk_diag(
									message=(
											"typecheck contract failure: missing can-throw classification for "
										f"{function_symbol(fn_id)}"
									),
									severity="error",
								)
							)
						# Conservative: treat missing classification as can-throw.
						fn_is_can_throw = True
				else:
					fn_is_can_throw = bool(sig.declared_can_throw) if sig else False

				if sig and sig.return_type_id is not None:
					# Surface return type is `T`. If the function is can-throw, the
					# internal ABI return is `FnResult<T, Error>`.
					fn_is_void = self._type_table.is_void(sig.return_type_id)
					if fn_is_can_throw:
						fn_return_parts = (sig.return_type_id, sig.error_type_id or self._error_type)
					else:
						# Legacy: older tests used FnResult as a surface return type.
						td = self._type_table.get(sig.return_type_id)
						if td.kind is TypeKind.FNRESULT and len(td.param_types) == 2:
							fn_return_parts = (td.param_types[0], td.param_types[1])

				for block_name, block in ssa.func.blocks.items():
					for instr in block.instructions:
						dest = getattr(instr, "dest", None)
						if isinstance(instr, StoreLocal):
							# Memory locals (address-taken) remain as StoreLocal even after SSA.
							src_ty = ty_for(fn_id, instr.value)
							if src_ty != self._unknown_type and value_types.get((fn_id, instr.local)) != src_ty:
								value_types[(fn_id, instr.local)] = src_ty
								changed = True
						elif isinstance(instr, LoadLocal) and dest is not None:
							# Propagate local type to the load destination.
							local_ty = value_types.get((fn_id, instr.local))
							if local_ty is not None and value_types.get((fn_id, dest)) != local_ty:
								value_types[(fn_id, dest)] = local_ty
								changed = True
						elif isinstance(instr, AddrOfLocal) and dest is not None:
							# Taking an address yields a reference type.
							local_ty = value_types.get((fn_id, instr.local))
							if local_ty is not None:
								ref_ty = (
									self._type_table.ensure_ref_mut(local_ty)
									if getattr(instr, "is_mut", False)
									else self._type_table.ensure_ref(local_ty)
								)
								if value_types.get((fn_id, dest)) != ref_ty:
									value_types[(fn_id, dest)] = ref_ty
									changed = True
						elif isinstance(instr, AddrOfField) and dest is not None:
							# Address-of field yields a reference to the field type.
							ref_ty = (
								self._type_table.ensure_ref_mut(instr.field_ty)
								if getattr(instr, "is_mut", False)
								else self._type_table.ensure_ref(instr.field_ty)
							)
							if value_types.get((fn_id, dest)) != ref_ty:
								value_types[(fn_id, dest)] = ref_ty
								changed = True
						elif isinstance(instr, LoadRef) and dest is not None:
							# Deref load result type is the element TypeId carried by the MIR.
							if value_types.get((fn_id, dest)) != instr.inner_ty:
								value_types[(fn_id, dest)] = instr.inner_ty
								changed = True
						elif isinstance(instr, StoreRef):
							# Stores do not define a value; no typing needed.
							pass
						elif isinstance(instr, ConstructVariant) and dest is not None:
							# Constructing a variant yields the declared variant TypeId.
							if value_types.get((fn_id, dest)) != instr.variant_ty:
								value_types[(fn_id, dest)] = instr.variant_ty
								changed = True
						elif isinstance(instr, VariantTag) and dest is not None:
							# Variant tags are compared as integers in v1; use Uint for the tag.
							if value_types.get((fn_id, dest)) != self._uint_type:
								value_types[(fn_id, dest)] = self._uint_type
								changed = True
						elif isinstance(instr, VariantGetField) and dest is not None:
							# Extracting a field yields the field TypeId carried by the MIR.
							if value_types.get((fn_id, dest)) != instr.field_ty:
								value_types[(fn_id, dest)] = instr.field_ty
								changed = True
						elif isinstance(instr, ConstInt) and dest is not None:
							if (fn_id, dest) not in value_types:
								value_types[(fn_id, dest)] = self._int_type
								changed = True
						elif isinstance(instr, ConstBool) and dest is not None:
							if (fn_id, dest) not in value_types:
								value_types[(fn_id, dest)] = self._bool_type
								changed = True
						elif isinstance(instr, ConstString) and dest is not None:
							if value_types.get((fn_id, dest)) != self._string_type:
								value_types[(fn_id, dest)] = self._string_type
								changed = True
						elif isinstance(instr, FnPtrConst) and dest is not None:
							params = list(instr.call_sig.param_types)
							ret = instr.call_sig.user_ret_type
							fn_ty = self._type_table.ensure_function(
								params,
								ret,
								can_throw=bool(instr.call_sig.can_throw),
							)
							if value_types.get((fn_id, dest)) != fn_ty:
								value_types[(fn_id, dest)] = fn_ty
								changed = True
						elif isinstance(instr, StringLen) and dest is not None:
							if value_types.get((fn_id, dest)) != self._int_type:
								value_types[(fn_id, dest)] = self._int_type
								changed = True
						elif isinstance(instr, StringByteAt) and dest is not None:
							byte_ty = self._type_table.ensure_byte()
							if value_types.get((fn_id, dest)) != byte_ty:
								value_types[(fn_id, dest)] = byte_ty
								changed = True
						elif isinstance(instr, ArrayLen) and dest is not None:
							if value_types.get((fn_id, dest)) != self._int_type:
								value_types[(fn_id, dest)] = self._int_type
								changed = True
						elif isinstance(instr, ArrayCap) and dest is not None:
							if value_types.get((fn_id, dest)) != self._int_type:
								value_types[(fn_id, dest)] = self._int_type
								changed = True
						elif isinstance(instr, ArrayIndexLoad) and dest is not None:
							if instr.elem_ty is not None and value_types.get((fn_id, dest)) != instr.elem_ty:
								value_types[(fn_id, dest)] = instr.elem_ty
								changed = True
						elif isinstance(instr, ArrayLit) and dest is not None:
							arr_ty = self._type_table.new_array(instr.elem_ty)
							if value_types.get((fn_id, dest)) != arr_ty:
								value_types[(fn_id, dest)] = arr_ty
								changed = True
						elif isinstance(instr, ArrayAlloc) and dest is not None:
							arr_ty = self._type_table.new_array(instr.elem_ty)
							if value_types.get((fn_id, dest)) != arr_ty:
								value_types[(fn_id, dest)] = arr_ty
								changed = True
						elif isinstance(instr, ArraySetLen) and dest is not None:
							arr_ty = ty_for(fn_id, instr.array)
							if value_types.get((fn_id, dest)) != arr_ty:
								value_types[(fn_id, dest)] = arr_ty
								changed = True
						elif isinstance(instr, ConstructStruct) and dest is not None:
							# Struct construction yields the nominal struct TypeId carried by MIR.
							if value_types.get((fn_id, dest)) != instr.struct_ty:
								value_types[(fn_id, dest)] = instr.struct_ty
								changed = True
						elif isinstance(instr, ConstructIface) and dest is not None:
							# Interface construction yields the interface TypeId carried by MIR.
							if value_types.get((fn_id, dest)) != instr.iface_ty:
								value_types[(fn_id, dest)] = instr.iface_ty
								changed = True
						elif isinstance(instr, StructGetField) and dest is not None:
							# Struct field access yields the field TypeId carried by MIR.
							if value_types.get((fn_id, dest)) != instr.field_ty:
								value_types[(fn_id, dest)] = instr.field_ty
								changed = True
						elif isinstance(instr, ConstructResultOk):
							if dest is None:
								continue
							ok_ty = fn_return_parts[0] if fn_return_parts else ty_for(fn_id, instr.value)
							err_ty = fn_return_parts[1] if fn_return_parts else self._error_type
							dest_ty = self._type_table.ensure_fnresult(ok_ty, err_ty)
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ConstructResultErr):
							if dest is None:
								continue
							err_ty = ty_for(fn_id, instr.error)
							ok_ty = fn_return_parts[0] if fn_return_parts else self._unknown_type
							dest_ty = self._type_table.ensure_fnresult(ok_ty, err_ty)
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ConstructError) and dest is not None:
							if value_types.get((fn_id, dest)) != self._error_type:
								value_types[(fn_id, dest)] = self._error_type
								changed = True
						elif isinstance(instr, ResultIsErr):
							# result.is_err() -> Bool
							if dest is not None and value_types.get((fn_id, dest)) != self._bool_type:
								value_types[(fn_id, dest)] = self._bool_type
								changed = True
						elif isinstance(instr, ResultOk):
							if dest is None:
								continue
							res_ty = ty_for(fn_id, instr.result)
							if self._type_table.get(res_ty).kind is TypeKind.FNRESULT:
								ok_ty, _err_ty = self._type_table.get(res_ty).param_types
								dest_ty = ok_ty
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, ResultErr):
							if dest is None:
								continue
							res_ty = ty_for(fn_id, instr.result)
							if self._type_table.get(res_ty).kind is TypeKind.FNRESULT:
								_ok_ty, err_ty = self._type_table.get(res_ty).param_types
								dest_ty = err_ty
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, Call) and dest is not None:
							callee_id = instr.fn_id
							callee_sig = signatures.get(callee_id)
							if callee_sig is not None:
								callee_can_throw = bool(instr.can_throw)
								ok_tid = callee_sig.return_type_id or self._unknown_type
								err_tid = callee_sig.error_type_id or self._error_type
								if callee_can_throw:
									dest_ty = self._type_table.ensure_fnresult(ok_tid, err_tid)
								else:
									dest_ty = ok_tid
									if is_void_tid(dest_ty):
										continue
							else:
								dest_ty = self._unknown_type
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, CallIndirect) and dest is not None:
							if instr.can_throw:
								ok_ty = instr.user_ret_type or self._unknown_type
								err_ty = self._error_type
								dest_ty = self._type_table.ensure_fnresult(ok_ty, err_ty)
							else:
								dest_ty = instr.user_ret_type or self._unknown_type
								if is_void_tid(dest_ty):
									continue
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, CallIface) and dest is not None:
							if instr.can_throw:
								ok_ty = instr.user_ret_type or self._unknown_type
								err_ty = self._error_type
								dest_ty = self._type_table.ensure_fnresult(ok_ty, err_ty)
							else:
								dest_ty = instr.user_ret_type or self._unknown_type
								if is_void_tid(dest_ty):
									continue
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, AssignSSA):
							if dest is None:
								continue
							src_ty = value_types.get((fn_id, instr.src))
							if src_ty is not None and value_types.get((fn_id, dest)) != src_ty:
								value_types[(fn_id, dest)] = src_ty
								changed = True
						elif isinstance(instr, UnaryOpInstr):
							if dest is None:
								continue
							operand_ty = ty_for(fn_id, instr.operand)
							if value_types.get((fn_id, dest)) != operand_ty:
								value_types[(fn_id, dest)] = operand_ty
								changed = True
						elif isinstance(instr, BinaryOpInstr):
							if dest is None:
								continue
							left_ty = ty_for(fn_id, instr.left)
							right_ty = ty_for(fn_id, instr.right)
							from lang.driftc.stage1.hir_nodes import BinaryOp as HBinaryOp
							if instr.op in (
								HBinaryOp.EQ,
								HBinaryOp.NE,
								HBinaryOp.LT,
								HBinaryOp.LE,
								HBinaryOp.GT,
								HBinaryOp.GE,
								HBinaryOp.AND,
								HBinaryOp.OR,
							):
								dest_ty = self._bool_type
							else:
								# Arithmetic/bitwise ops preserve operand scalar type when operands agree.
								dest_ty = left_ty if left_ty == right_ty else self._unknown_type
							if value_types.get((fn_id, dest)) != dest_ty:
								value_types[(fn_id, dest)] = dest_ty
								changed = True
						elif isinstance(instr, Phi):
							if dest is None:
								continue
							incoming = [value_types.get((fn_id, v)) for v in instr.incoming.values()]
							incoming = [t for t in incoming if t is not None]
							if incoming and all(t == incoming[0] for t in incoming):
								ty = incoming[0]
								if value_types.get((fn_id, dest)) != ty:
									value_types[(fn_id, dest)] = ty
									changed = True

					term = block.terminator
					if hasattr(term, "value") and getattr(term, "value") is not None:
						val = term.value
						# Seed return types even if they were previously Unknown.
						if not fn_is_void:
							existing = value_types.get((fn_id, val))
							if fn_return_parts is not None:
								desired = self._type_table.ensure_fnresult(fn_return_parts[0], fn_return_parts[1])
							elif sig and sig.return_type_id is not None:
								desired = sig.return_type_id
							else:
								desired = self._unknown_type
							if existing is not None and existing != self._unknown_type and existing != desired:
								if diagnostics is not None and (fn_id, val) not in reported_return_mismatch:
									reported_return_mismatch.add((fn_id, val))
									allow_int_uint = False
									allow_fnresult_unknown_err = False
									try:
										existing_def = self._type_table.get(existing)
										desired_def = self._type_table.get(desired)
										if (
											existing_def.kind is TypeKind.FNRESULT
											and desired_def.kind is TypeKind.FNRESULT
											and existing_def.param_types
											and desired_def.param_types
											and existing_def.param_types[0] == desired_def.param_types[0]
										):
											existing_err = existing_def.param_types[1]
											desired_err = desired_def.param_types[1]
											if (
												self._type_table.get(existing_err).kind is TypeKind.UNKNOWN
												and self._type_table.get(desired_err).kind is TypeKind.ERROR
											):
												allow_fnresult_unknown_err = True
										if (
											existing_def.kind is TypeKind.SCALAR
											and desired_def.kind is TypeKind.SCALAR
											and {existing_def.name, desired_def.name} <= {"Int", "Uint"}
										):
											allow_int_uint = True
									except KeyError:
										allow_int_uint = False
									if allow_int_uint or allow_fnresult_unknown_err:
										value_types[(fn_id, val)] = desired
										changed = True
										continue
									diagnostics.append(
										_chk_diag(
											message=(
													"typecheck contract failure: SSA return type does not match declared signature "
												f"for {function_symbol(fn_id)} in {block_name} "
												f"({existing} vs {desired})"
											),
											severity="error",
										)
									)
								value_types[(fn_id, val)] = desired
								changed = True
							elif existing is None or existing == self._unknown_type:
								value_types[(fn_id, val)] = desired
								changed = True

		if not value_types:
			return None
		return CheckerTypeEnv(self._type_table, value_types)

	def build_type_env_from_ssa_by_id(
		self,
		ssa_funcs: Mapping[FunctionId, "SsaFunc"],
		signatures_by_id: Mapping[FunctionId, FnSignature],
		*,
		can_throw_by_id: Mapping[FunctionId, bool] | None = None,
		diagnostics: list[Diagnostic] | None = None,
	) -> Optional["CheckerTypeEnv"]:
		return self.build_type_env_from_ssa(
			ssa_funcs,
			signatures_by_id,
			can_throw_by_id=can_throw_by_id,
			diagnostics=diagnostics,
		)
