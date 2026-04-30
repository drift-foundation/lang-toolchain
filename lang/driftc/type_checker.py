#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""
Minimal typed checker skeleton for lang.

This is a real checker scaffold that:
- Allocates ParamId/LocalId/BindingId for bindings.
- Infers types for basic expressions (literals, vars, lets, borrows, calls).
- Produces a TypedFn record with expression TypeIds and binding identity.

It is intentionally small; it will grow to cover full Drift semantics. Borrow
checker integration will consume TypedFn once this matures.
"""

from __future__ import annotations

import os
import sys
from collections import ChainMap

from dataclasses import dataclass, field, replace, fields, is_dataclass
from enum import Enum, auto
from typing import Dict, List, Optional, Mapping, Sequence, Set, Tuple

from lang.driftc import stage1 as H
from lang.driftc import debug as drift_debug
from lang.driftc.stage1.call_info import CallInfo, CallSig, CallTarget, CallTargetKind, IntrinsicKind
from lang.driftc.stage1.node_ids import assign_node_ids
from lang.driftc.stage1.capture_discovery import discover_captures
from lang.driftc.stage1.place_expr import place_expr_from_lvalue_expr
from lang.driftc.checker import FnSignature, TypeParam, user_facing_binding_name
from lang.driftc.checker.typed_validator import validate_typed_hir
from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.core.types_core import TypeParamId
from lang.driftc.instantiation.key import build_instantiation_key, instantiation_key_hash


# Typecheck diagnostics should always carry phase.

FIXED_WIDTH_TYPE_NAMES = {
	"Int8",
	"Int16",
	# Int32 deliberately excluded: available in user code for C FFI interop
	# (C `int` is 32-bit on all major platforms).
	"Int64",
	"Uint8",
	"Uint16",
	# Uint32 deliberately excluded: available in user code for C FFI interop
	# (C `unsigned int` is 32-bit on all major platforms).
	# Uint64/u64 deliberately excluded: available in user code for portable
	# 64-bit unsigned arithmetic (crypto, hashing, bit manipulation).
	"F32",
	"F64",
	"Float32",
	"Float64",
}
def _tc_diag(*args, **kwargs):
	if "phase" not in kwargs:
		if len(args) >= 3:
			args = list(args)
			if args[2] is None:
				args[2] = "typecheck"
			return Diagnostic(*args, **kwargs)
		kwargs["phase"] = "typecheck"
	elif kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)

from lang.driftc.core.span import Span
from lang.driftc.core.types_core import (
	TypeId,
	TypeTable,
	TypeKind,
	VariantInstance,
	VariantSchema,
	TypeParamId,
	VariantArmSchema,
	VariantFieldSchema,
)
from lang.driftc.core.function_id import (
	FunctionId,
	FunctionRefId,
	FunctionRefKind,
	FnNameKey,
	fn_name_key,
	function_symbol,
)
from lang.driftc.core.function_key import FunctionKey
from lang.driftc.core.type_resolve_common import resolve_opaque_type
from lang.driftc.core.type_subst import Subst, apply_subst
from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.borrow_checker import (
	DerefProj,
	FieldProj,
	IndexProj,
	Place,
	PlaceBase,
	PlaceKind,
	place_from_expr,
	places_overlap,
)
from lang.driftc.method_registry import CallableDecl, CallableKind, CallableRegistry, CallableSignature, ModuleId, SelfMode, Visibility
from lang.driftc.impl_index import GlobalImplIndex, ImplMeta
from lang.driftc.infer import (
	InferConstraint,
	InferConstraintOrigin,
	InferBindingEvidence,
	InferConflictEvidence,
	InferContext,
	InferError,
	InferErrorKind,
	InferResult,
	InferTrace,
	format_infer_failure,
)
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex, TraitImplCandidate
from lang.driftc.traits.world import (
	TraitKey,
	TraitWorld,
	TypeKey,
	normalize_type_key,
	trait_key_from_expr,
	type_key_from_typeid,
)
from lang.driftc.traits.linked_world import (
	LinkedWorld,
	RequireEnv,
	BOOL_TRUE,
	build_require_env,
	link_trait_worlds,
)
from lang.driftc.method_resolver import MethodResolution, ResolutionError
from lang.driftc.checker.call_resolver import MethodCallResult, _try_wrap_arg_for_callback_field, make_call_ctx, make_method_ctx, make_resolver_ctx, resolve_call_expr, resolve_method_call, resolve_qualified_member_call, resolve_qualified_member_ufcs
from lang.driftc.parser import ast as parser_ast
from lang.driftc.traits.solver import (
	Env as TraitEnv,
	Obligation,
	ObligationOrigin,
	ObligationOriginKind,
	ProofFailure,
	ProofFailureReason,
	ProofStatus,
	prove_expr,
	prove_obligation,
)
from lang.driftc.traits.world import type_key_from_expr

# Identifier aliases for clarity.
ParamId = int
LocalId = int
GuardKey = int
DeferredGuardKey = Tuple[GuardKey, str]


# `Copy` lives in `std.core.copy` (per the std.core file split) and is
# re-exported by `std.core`.  Both module ids are accepted as the
# canonical Copy trait so test stubs that inline `pub trait Copy` in
# `module std.core` and the production stdlib both validate.
_CORE_COPY_MODULES: frozenset[str] = frozenset({"std.core", "std.core.copy"})


def _is_core_copy_trait_key(key: object) -> bool:
	return getattr(key, "module", None) in _CORE_COPY_MODULES and getattr(key, "name", None) == "Copy"


@dataclass
class TypedFn:
	"""Typed view of a single function's HIR."""

	fn_id: FunctionId
	name: str
	params: List[ParamId]
	param_bindings: List[int]
	locals: List[LocalId]
	body: H.HBlock
	expr_types: Dict[int, TypeId]  # keyed by node_id
	binding_for_var: Dict[int, int]  # keyed by node_id
	binding_types: Dict[int, TypeId]  # binding_id -> TypeId
	binding_names: Dict[int, str]  # binding_id -> name
	binding_mutable: Dict[int, bool]  # binding_id -> declared var?
	binding_place_kind: Dict[int, PlaceKind] = field(default_factory=dict)  # binding_id -> place kind
	call_resolutions: Dict[int, CallableDecl | MethodResolution] = field(default_factory=dict)
	call_info_by_callsite_id: Dict[int, "CallInfo"] = field(default_factory=dict)
	instantiations_by_callsite_id: Dict[int, "CallInstantiation"] = field(default_factory=dict)
	instantiations_by_node_id: Dict[int, "CallInstantiation"] = field(default_factory=dict)
	iface_coercions: Dict[int, TypeId] = field(default_factory=dict)
	preseed_type_params: Dict[str, TypeId] = field(default_factory=dict)


@dataclass(frozen=True)
class CallInstantiation:
	"""Resolved instantiation info for a call-site."""

	target_key: FunctionKey
	type_args: Tuple[TypeId, ...]


@dataclass
class TypeCheckResult:
	"""Result of type checking a function."""

	typed_fn: TypedFn
	diagnostics: List[Diagnostic] = field(default_factory=list)
	deferred_guard_diags: Dict[DeferredGuardKey, List[Diagnostic]] = field(default_factory=dict)
	guard_outcomes: Dict[GuardKey, ProofStatus] = field(default_factory=dict)


class ThunkKind(Enum):
	OK_WRAP = auto()
	BOUNDARY = auto()


@dataclass
class ThunkSpec:
	"""Synthetic thunk for function value lowering."""

	thunk_fn_id: FunctionId
	target_fn_id: FunctionId
	param_types: tuple[TypeId, ...]
	return_type: TypeId
	kind: ThunkKind


@dataclass(frozen=True)
class LambdaFnSpec:
	"""Synthetic function for a captureless lambda coerced to a fn pointer."""

	fn_id: FunctionId
	origin_fn_id: FunctionId | None
	lambda_expr: "H.HLambda"
	param_types: tuple[TypeId, ...]
	return_type: TypeId
	can_throw: bool
	call_info_by_callsite_id: dict[int, "CallInfo"]


class TypeChecker:
	"""
	Minimal HIR type checker that assigns binding IDs and basic types.

	This is a skeleton: it understands literals, vars, lets, borrows, calls, and
	a small set of builtin constructs (f-strings, exceptions, DiagnosticValue
	helpers).
	"""

	def __init__(self, type_table: Optional[TypeTable] = None, *, allow_unsafe: bool = False, allow_unsafe_without_block: bool = False, unsafe_trusted_modules: set[str] | None = None, pkg_unsafe_modules: set[str] | None = None, semantic_world: object | None = None, source_modules: set[str] | None = None):
		self.type_table = type_table or TypeTable()
		self.semantic_world = semantic_world
		# Modules compiled from source in this compilation unit (not from packages).
		# Used to avoid false ABI boundary enforcement between source-compiled modules.
		self._source_modules: set[str] = source_modules or set()
		self._uint = self.type_table.ensure_uint()
		self._uint64 = self.type_table.ensure_uint64()
		self._int = self.type_table.ensure_int()
		self._float = self.type_table.ensure_float()
		self._bool = self.type_table.ensure_bool()
		self._string = self.type_table.ensure_string()
		self._void = self.type_table.ensure_void()
		self._error = self.type_table.ensure_error()
		self._dv = self.type_table.ensure_diagnostic_value()
		self._unknown = self.type_table.ensure_unknown()
		self._thunk_specs: dict[tuple[ThunkKind, FunctionId, tuple[TypeId, ...], TypeId], ThunkSpec] = {}
		self._lambda_fn_specs: dict[FunctionId, LambdaFnSpec] = {}
		# Binding ids (params and locals) share a single id-space.
		self._next_binding_id: int = 1
		self._defaulted_phase_count: int = 0
		self._allow_unsafe = bool(allow_unsafe)
		self._allow_unsafe_without_block = bool(allow_unsafe_without_block)
		self._unsafe_trusted_modules = set(unsafe_trusted_modules or [])
		# Package modules that need unsafe permission (producer already
		# validated) but NOT full toolchain trust (no rawbuffer intrinsics).
		self._pkg_unsafe_modules = set(pkg_unsafe_modules or [])
	def _is_toolchain_trusted_module(self, module_name: str | None) -> bool:
		return bool(module_name) and module_name in self._unsafe_trusted_modules
	def _is_pkg_unsafe_allowed(self, module_name: str | None) -> bool:
		return bool(module_name) and module_name in self._pkg_unsafe_modules

	def _stamp_diag_phase(self, diag: Diagnostic) -> None:
		if diag.phase is None:
			diag.phase = "typecheck"
			self._defaulted_phase_count += 1

	def defaulted_phase_count(self) -> int:
		return self._defaulted_phase_count

	def _optional_variant_type(self, inner_ty: TypeId) -> TypeId:
		opt_base = self.type_table.ensure_optional_base()
		if self.type_table.has_typevar(inner_ty):
			return self.type_table.ensure_variant_template(opt_base, [inner_ty])
		return self.type_table.ensure_variant_instantiated(opt_base, [inner_ty])

	def thunk_specs(self) -> list[ThunkSpec]:
		return list(self._thunk_specs.values())

	def lambda_fn_specs(self) -> list[LambdaFnSpec]:
		return list(self._lambda_fn_specs.values())

	def _pretty_type_name(self, ty: TypeId, *, current_module: str | None) -> str:
		"""
		Render a user-facing type name for diagnostics.

		This is intentionally small: enough for MVP error messages without
		committing to a full surface type renderer.
		"""
		td = self.type_table.get(ty)
		name = td.name
		if td.module_id and current_module and td.module_id not in {current_module, "lang.core"}:
			name = f"{td.module_id}.{name}"
		if td.kind is TypeKind.FUNCTION:
			param_types = list(td.param_types[:-1]) if td.param_types else []
			ret_type = td.param_types[-1] if td.param_types else self._unknown
			params = ", ".join(self._pretty_type_name(t, current_module=current_module) for t in param_types)
			ret = self._pretty_type_name(ret_type, current_module=current_module)
			if td.can_throw():
				return f"Fn({params}) -> {ret}"
			return f"Fn({params}) nothrow -> {ret}"
		# For nominal kinds with monomorphized instances, the user-facing
		# type-args live in the instance map — NOT in `td.param_types`.
		# STRUCT's `td.param_types` holds field types; INTERFACE's is
		# typically empty (instance args only in `interface_instances`);
		# VARIANT can also disagree.  Reading `td.param_types` here was
		# the 0.31.20-era STRUCT-vs-VARIANT pretty-printer bug, also
		# surfaced for INTERFACE in 0.31.31 by the bookkeeper /
		# web-rest middleware diagnostic where the third Callback3
		# type-arg rendered as bare `std.core.Callback2` (no args).
		type_args: list[TypeId] = []
		if td.kind is TypeKind.STRUCT:
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None:
				type_args = list(inst.type_args)
		elif td.kind is TypeKind.INTERFACE:
			inst = self.type_table.get_interface_instance(ty)
			if inst is not None:
				type_args = list(inst.type_args)
		elif td.kind is TypeKind.VARIANT:
			inst = self.type_table.get_variant_instance(ty)
			if inst is not None:
				type_args = list(inst.type_args)
		elif td.param_types:
			# REF / RAW_PTR / ARRAY etc. — `td.param_types` is the
			# correct shape for these.
			type_args = list(td.param_types)
		if type_args:
			args = ", ".join(self._pretty_type_name(t, current_module=current_module) for t in type_args)
			return f"{name}<{args}>"
		return name

	def _seed_binding_id_counter(self, body: H.HBlock) -> None:
		"""
		Ensure new binding ids won't collide with ids already present in HIR.

		Stage1 may assign binding ids during parsing/normalization. The type
		checker also allocates ids for temps introduced in later rewrites (e.g.
		borrow materialization). We must avoid reusing ids that already appear in
		the input HIR.
		"""
		max_id = 0

		def bump(obj: object) -> None:
			nonlocal max_id
			bid = getattr(obj, "binding_id", None)
			if isinstance(bid, int) and bid > max_id:
				max_id = bid

		def walk_expr(expr: H.HExpr) -> None:
			bump(expr)
			if isinstance(expr, H.HVar):
				return
			if isinstance(expr, H.HUnary):
				walk_expr(expr.expr)
				return
			if isinstance(expr, H.HBinary):
				walk_expr(expr.left)
				walk_expr(expr.right)
				return
			if isinstance(expr, H.HTernary):
				walk_expr(expr.cond)
				walk_expr(expr.then_expr)
				walk_expr(expr.else_expr)
				return
			if isinstance(expr, H.HBorrow):
				walk_expr(expr.subject)
				return
			if isinstance(expr, getattr(H, "HMove", ())):
				walk_expr(expr.subject)
				return
			if isinstance(expr, getattr(H, "HCopy", ())):
				walk_expr(expr.subject)
				return
			if isinstance(expr, H.HCall):
				walk_expr(expr.fn)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, getattr(H, "HInvoke", ())):
				walk_expr(expr.callee)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, getattr(H, "HTypeApp", ())):
				walk_expr(expr.fn)
				return
			if isinstance(expr, H.HMethodCall):
				walk_expr(expr.receiver)
				for arg in expr.args:
					walk_expr(arg)
				for kw in getattr(expr, "kwargs", []) or []:
					walk_expr(kw.value)
				return
			if isinstance(expr, H.HField):
				walk_expr(expr.subject)
				return
			if isinstance(expr, H.HIndex):
				walk_expr(expr.subject)
				walk_expr(expr.index)
				return
			if isinstance(expr, getattr(H, "HPlaceExpr", ())):
				bump(expr.base)
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceIndex):
						walk_expr(proj.index)
				return
			if isinstance(expr, H.HArrayLiteral):
				for elem in expr.elements:
					walk_expr(elem)
				return
			if isinstance(expr, H.HFString):
				for hole in expr.holes:
					walk_expr(hole.expr)
				return
			if isinstance(expr, H.HLambda):
				for param in expr.params:
					bump(param)
				for cap in expr.explicit_captures or []:
					bump(cap)
				if expr.body_expr is not None:
					walk_expr(expr.body_expr)
				if expr.body_block is not None:
					walk_block(expr.body_block)
				return
			if isinstance(expr, H.HResultOk):
				walk_expr(expr.value)
				return
			if isinstance(expr, H.HTryExpr):
				walk_expr(expr.attempt)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return
			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				walk_block(expr.body)
				walk_expr(expr.result)
				return
			if isinstance(expr, H.HMatchExpr):
				walk_expr(expr.scrutinee)
				for arm in expr.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)
				return

		def walk_stmt(stmt: H.HStmt) -> None:
			bump(stmt)
			if isinstance(stmt, H.HLocalConst):
				return  # literal initializer, no expressions to walk
			if isinstance(stmt, H.HLet):
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HAssign):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
				walk_expr(stmt.target)
				walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
				return
			if isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					walk_expr(stmt.value)
				return
			if isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block is not None:
					walk_block(stmt.else_block)
				return
			if isinstance(stmt, H.HLoop):
				walk_block(stmt.body)
				return
			if isinstance(stmt, H.HBlock):
				walk_block(stmt)
				return
			if hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				walk_block(stmt.block)
				return
			if isinstance(stmt, H.HTry):
				walk_block(stmt.body)
				for arm in stmt.catches:
					walk_block(arm.block)
				return
			if isinstance(stmt, H.HThrow):
				walk_expr(stmt.value)
				return

		def walk_block(block: H.HBlock) -> None:
			for stmt in block.statements:
				walk_stmt(stmt)

		walk_block(body)
		self._next_binding_id = max_id + 1

	def _format_ctor_signature_list(
		self,
		*,
		schema: VariantSchema,
		instance: VariantInstance | None,
		current_module: str | None,
	) -> list[str]:
		"""
		Return a stable, user-facing list of constructor “signatures”.

		Pinned formatting rules (MVP):
		- Sort by constructor name, then arity.
		- Render as: `CtorName(arg1, arg2)` with no extra spaces.
		- If a payload type is unknown/unrenderable, show `_`.

		When `instance` is available we prefer concrete field types; otherwise we
		fall back to schema generic expressions (`T`, `Array<T>`, etc.).
		"""

		def _render_generic(g: GenericTypeExpr) -> str:
			if g.param_index is not None:
				idx = int(g.param_index)
				if 0 <= idx < len(schema.type_params):
					return schema.type_params[idx]
				return "_"
			name = g.name
			args = list(g.args or [])
			if not args:
				return name
			return f"{name}<{', '.join(_render_generic(a) for a in args)}>"

		arms = sorted(schema.arms, key=lambda a: (a.name, len(a.fields)))
		out: list[str] = []
		for arm in arms:
			field_parts: list[str] = []
			if instance is not None:
				inst_arm = instance.arms_by_name.get(arm.name)
				if inst_arm is not None:
					for ft in inst_arm.field_types:
						field_parts.append(self._pretty_type_name(ft, current_module=current_module))
			if not field_parts:
				for f in arm.fields:
					field_parts.append(_render_generic(f.type_expr))
			out.append(f"`{arm.name}({', '.join(field_parts)})`")
		return out

	def validate_no_recursive_value_types(self, *, diagnostics: list[Diagnostic]) -> None:
		"""Reject struct/variant declarations whose field-type transitive closure
		forms a cycle in which every edge is by-value (no `Arc`/`Array`/`&`/etc.
		indirection).

		Closes `issues/recursive-value-struct-accepted/`.

		Algorithm (kind-based, no name allowlist):

		1. For each STRUCT instance and each VARIANT instance in the type table,
		   build a directed by-value edge set. An edge `A → B` exists when `B`
		   is a STRUCT or VARIANT type id reachable from one of `A`'s field
		   types (struct field, or variant arm payload field) without crossing
		   an indirection-bearing kind: REF, RAW_PTR, ARRAY, FUNCTION, INTERFACE.
		2. Run Tarjan SCC on the resulting graph.
		3. Any SCC of size > 1, OR a single-node SCC with a self-loop, is a
		   recursive value-type cycle. Emit one diagnostic per offending type
		   in each cycle, naming the cycle members and suggesting an
		   indirection wrapper. The primary suggestion is `Arc<...>`; when
		   the offending field is `Optional<Self>`, the suggestion preserves
		   the user's `Optional` wrapper as `Optional<Arc<Self>>`.
		"""
		table = self.type_table
		if table is None:
			return

		_INDIRECTION_KINDS = {
			TypeKind.REF,
			TypeKind.RAW_PTR,
			TypeKind.ARRAY,
			TypeKind.FUNCTION,
			TypeKind.INTERFACE,
		}

		def _resolve_forward(tid: int) -> int:
			# Resolve FORWARD_NOMINAL to its concrete struct/variant tid if
			# the table has one registered for the same (module_id, name).
			seen: set[int] = set()
			cur = tid
			while cur not in seen:
				seen.add(cur)
				td = table.get(cur)
				if td.kind is not TypeKind.FORWARD_NOMINAL:
					return cur
				mod = getattr(td, "module_id", None)
				name = td.name
				nominal = (
					table.get_nominal(kind=TypeKind.STRUCT, module_id=mod, name=name)
					or table.get_nominal(kind=TypeKind.VARIANT, module_id=mod, name=name)
				)
				if nominal is None or nominal == cur:
					return cur
				cur = nominal
			return cur

		def _by_value_children(field_type_ids: list[int]) -> set[int]:
			"""For each field type id, classify and collect by-value
			STRUCT/VARIANT children (with FORWARD_NOMINAL resolved)."""
			out: set[int] = set()
			for ft in field_type_ids:
				if ft is None:
					continue
				resolved = _resolve_forward(ft)
				td = table.get(resolved)
				if td.kind in _INDIRECTION_KINDS:
					continue
				if td.kind is TypeKind.STRUCT or td.kind is TypeKind.VARIANT:
					out.add(int(resolved))
			return out

		# Build the by-value graph for every STRUCT/VARIANT instance.
		edges: dict[int, set[int]] = {}
		nodes: set[int] = set()

		for tid, inst in (getattr(table, "struct_instances", {}) or {}).items():
			node_id = int(tid)
			nodes.add(node_id)
			edges[node_id] = _by_value_children(list(getattr(inst, "field_types", []) or []))

		for tid, inst in (getattr(table, "variant_instances", {}) or {}).items():
			node_id = int(tid)
			nodes.add(node_id)
			arms_field_types: list[int] = []
			for arm in getattr(inst, "arms", []) or []:
				arms_field_types.extend(getattr(arm, "field_types", []) or [])
			edges[node_id] = _by_value_children(arms_field_types)

		if not nodes:
			return

		# Iterative Tarjan SCC. Avoids Python recursion on deep type graphs.
		index_of: dict[int, int] = {}
		lowlink: dict[int, int] = {}
		on_stack: set[int] = set()
		scc_stack: list[int] = []
		sccs: list[list[int]] = []
		next_index = 0

		# Each work-stack entry is (node, child_iter_state). State is the
		# index into the children list at which to resume the post-loop body.
		def _strongconnect(start: int) -> None:
			nonlocal next_index
			work: list[tuple[int, int]] = [(start, 0)]
			# Pre-visit setup for the root node.
			index_of[start] = next_index
			lowlink[start] = next_index
			next_index += 1
			scc_stack.append(start)
			on_stack.add(start)
			while work:
				v, idx = work[-1]
				children = sorted(edges.get(v, ()))
				if idx < len(children):
					work[-1] = (v, idx + 1)
					w = children[idx]
					if w not in index_of:
						index_of[w] = next_index
						lowlink[w] = next_index
						next_index += 1
						scc_stack.append(w)
						on_stack.add(w)
						work.append((w, 0))
					elif w in on_stack:
						if index_of[w] < lowlink[v]:
							lowlink[v] = index_of[w]
					continue
				# All children of v processed. If v is an SCC root, pop the SCC.
				if lowlink[v] == index_of[v]:
					component: list[int] = []
					while True:
						w = scc_stack.pop()
						on_stack.discard(w)
						component.append(w)
						if w == v:
							break
					sccs.append(component)
				work.pop()
				if work:
					parent = work[-1][0]
					if lowlink[v] < lowlink[parent]:
						lowlink[parent] = lowlink[v]

		for n in sorted(nodes):
			if n not in index_of:
				_strongconnect(n)

		# A cycle is any SCC with size > 1, OR a single-node SCC with a self-loop.
		cycles: list[list[int]] = []
		for scc in sccs:
			if len(scc) > 1:
				cycles.append(sorted(scc))
				continue
			only = scc[0]
			if only in edges.get(only, set()):
				cycles.append([only])

		if not cycles:
			return

		# Build user-facing names and emit diagnostics.
		def _type_name(tid: int) -> str:
			td = table.get(tid)
			mod = getattr(td, "module_id", None)
			name = getattr(td, "name", None) or "<anonymous>"
			if mod:
				return f"{mod}::{name}"
			return name

		def _suggest_indirection(field_decl_type_expr: object | None, self_type_name: str) -> str:
			"""Return the suggested replacement for an offending field type.

			- If the field type is `Optional<...Self...>`, suggest
			  `Optional<Arc<Self>>` (preserving the user's wrapper).
			- Otherwise the primary suggestion is `Arc<Self>`.
			"""
			if field_decl_type_expr is not None:
				expr_name = getattr(field_decl_type_expr, "name", None)
				if expr_name == "Optional":
					return f"Optional<Arc<{self_type_name}>>"
			return f"Arc<{self_type_name}>"

		def _offending_field_for(tid: int, cycle_set: set[int]) -> tuple[str | None, object | None, object | None]:
			"""Return (field_name, type_expr, decl_loc) for the field on `tid`
			that creates a by-value edge into `cycle_set`.

			- field_name and type_expr are used for the diagnostic message and
			  suggestion shape (e.g. preserving an Optional<...> wrapper).
			- decl_loc is the source loc of the *containing* struct/variant
			  declaration; the schema layer does not retain field-local locs,
			  so the diagnostic anchors at the declaration that holds the
			  offending field.
			"""
			td = table.get(tid)
			if td.kind is TypeKind.STRUCT:
				schema = (getattr(table, "struct_bases", {}) or {}).get(tid)
				inst = (getattr(table, "struct_instances", {}) or {}).get(tid)
				if schema is None or inst is None:
					return (None, None, None)
				field_names = list(getattr(inst, "field_names", []) or [])
				field_types = list(getattr(inst, "field_types", []) or [])
				for fname, fty in zip(field_names, field_types):
					resolved = _resolve_forward(fty)
					if int(resolved) in cycle_set:
						type_expr: object | None = None
						for fs in getattr(schema, "fields", []) or []:
							if fs.name == fname:
								type_expr = fs.type_expr
								break
						return (fname, type_expr, getattr(schema, "decl_loc", None))
				return (None, None, getattr(schema, "decl_loc", None))
			if td.kind is TypeKind.VARIANT:
				schema = (getattr(table, "variant_schemas", {}) or {}).get(tid)
				inst = (getattr(table, "variant_instances", {}) or {}).get(tid)
				if schema is None or inst is None:
					return (None, None, None)
				for arm_inst in getattr(inst, "arms", []) or []:
					for fname, fty in zip(getattr(arm_inst, "field_names", []) or [], getattr(arm_inst, "field_types", []) or []):
						resolved = _resolve_forward(fty)
						if int(resolved) in cycle_set:
							qualified = f"{arm_inst.name}.{fname}"
							type_expr2: object | None = None
							for arm_s in getattr(schema, "arms", []) or []:
								if arm_s.name == arm_inst.name:
									for fs2 in arm_s.fields:
										if fs2.name == fname:
											type_expr2 = fs2.type_expr
											break
									break
							return (qualified, type_expr2, getattr(schema, "decl_loc", None))
				return (None, None, getattr(schema, "decl_loc", None))
			return (None, None, None)

		def _is_toolchain_type(tid: int) -> bool:
			"""User diagnostics should anchor at user-defined types, not at
			toolchain-provided types like `lang.core::Optional` that the
			user merely *uses*. This predicate identifies cycle members
			that should be deprioritized for anchor selection.
			"""
			td = table.get(tid)
			mod = getattr(td, "module_id", None)
			if mod is None:
				return False
			return mod.startswith("lang.")

		# Emit one diagnostic per cycle. Anchor selection rules:
		#   1. Prefer user-defined types (module_id not under `lang.*`).
		#   2. Within that preference class, pick the lex-smallest type
		#      name for determinism.
		# This keeps diagnostics pointing at the user's declaration even
		# when the cycle physically includes a toolchain type like
		# `lang.core::Optional` that the user only used as a wrapper.
		for cycle in cycles:
			cycle_set = set(cycle)
			user_members = [c for c in cycle if not _is_toolchain_type(c)]
			candidates = user_members if user_members else list(cycle)
			anchor_tid = min(candidates, key=_type_name)
			anchor_name = _type_name(anchor_tid)
			offending_field_name, offending_type_expr, anchor_decl_loc = _offending_field_for(anchor_tid, cycle_set)
			suggestion = _suggest_indirection(offending_type_expr, anchor_name)
			field_phrase = (
				f" through field '{offending_field_name}'"
				if offending_field_name is not None
				else ""
			)
			suggestion_phrase = (
				f"; suggestion: wrap the offending field in `{suggestion}`"
			)
			# Build the cycle path starting at the anchor for stable display.
			anchor_idx = cycle.index(anchor_tid) if anchor_tid in cycle else 0
			rotated = cycle[anchor_idx:] + cycle[:anchor_idx]
			cycle_names = " → ".join(_type_name(c) for c in rotated)
			if len(cycle) > 1:
				message = (
					f"recursive value type: '{anchor_name}' participates in a "
					f"by-value cycle ({cycle_names} → {anchor_name})"
					f"{field_phrase}; every cycle must contain at least one "
					f"indirection (Arc, &, Array, RawPtr)"
					f"{suggestion_phrase}"
				)
			else:
				message = (
					f"recursive value type: '{anchor_name}' is infinitely recursive"
					f"{field_phrase}; the field must contain at least one "
					f"indirection (Arc, &, Array, RawPtr)"
					f"{suggestion_phrase}"
				)
			# Anchor the diagnostic at the containing struct/variant
			# declaration loc. The schema layer does not retain field-local
			# locs, so the message names the field but the span points at
			# the declaration line.
			diag_span = Span.from_loc(anchor_decl_loc) if anchor_decl_loc is not None else Span()
			diagnostics.append(
				_tc_diag(
					message=message,
					code="E_RECURSIVE_VALUE_TYPE",
					severity="error",
					span=diag_span,
				)
			)

	def validate_interface_schemas(self, *, diagnostics: list[Diagnostic]) -> None:
		def _fixed_width_allowed(module_name: str | None) -> bool:
			if module_name is None:
				return False
			return module_name.startswith("lang.abi.") or module_name.startswith("std.")

		def _scan_generic(expr: object, *, module_name: str | None, span: Span | None) -> None:
			if not isinstance(expr, GenericTypeExpr):
				return
			if expr.name == "Self":
				return
			if expr.param_index is not None:
				return
			if expr.name in FIXED_WIDTH_TYPE_NAMES and not _fixed_width_allowed(module_name):
				diagnostics.append(
					_tc_diag(
						message=f"fixed-width type '{expr.name}' is reserved in v1; use Int/Uint/Float or Byte",
						code="E_FIXED_WIDTH_RESERVED",
						severity="error",
						span=span or Span(),
					)
				)
			for arg in expr.args:
				_scan_generic(arg, module_name=module_name, span=span)

		for _base_id, schema in (getattr(self.type_table, "interface_bases", {}) or {}).items():
			module_id = schema.module_id
			methods = list(getattr(schema, "methods", []) or [])
			for method in methods:
				seen_params: set[str] = set()
				for param in method.params:
					if param.name in seen_params:
						diagnostics.append(
							_tc_diag(
								message=f"duplicate parameter '{param.name}' in interface method '{method.name}'",
								code="E_DUPLICATE_PARAM",
								severity="error",
								span=None,
							)
						)
						continue
					seen_params.add(param.name)
					if param.name == "self":
						if not isinstance(param.type_expr, GenericTypeExpr) or param.type_expr.name not in {"&", "&mut"}:
							diagnostics.append(
								_tc_diag(
									message=f"interface method '{method.name}' must take self by reference",
									severity="error",
									span=None,
								)
							)
						elif not param.type_expr.args or param.type_expr.args[0].name != "Self":
							diagnostics.append(
								_tc_diag(
									message=f"interface method '{method.name}' must use '&Self' or '&mut Self' for self",
									severity="error",
									span=None,
								)
							)
					else:
						if isinstance(param.type_expr, GenericTypeExpr) and param.type_expr.name == "Self":
							diagnostics.append(
								_tc_diag(
									message=f"interface method '{method.name}' parameter '{param.name}' cannot use Self",
									severity="error",
									span=None,
								)
							)
					_scan_generic(param.type_expr, module_name=module_id, span=None)
				_scan_generic(method.return_type, module_name=module_id, span=None)
				if isinstance(method.return_type, GenericTypeExpr) and method.return_type.name == "Self":
					diagnostics.append(
						_tc_diag(
							message=f"interface method '{method.name}' return type cannot be Self in v1",
							severity="error",
							span=None,
						)
					)

				total_params = len(schema.type_params) + len(method.type_params)
				if total_params:
					owner = FunctionId(
						module="lang.__internal",
						name=f"__interface_{schema.module_id}::{schema.name}::{method.name}",
						ordinal=0,
					)
					type_args = [
						self.type_table.ensure_typevar(TypeParamId(owner=owner, index=i))
						for i in range(total_params)
					]
				else:
					type_args = []

				for param in method.params:
					if param.name == "self":
						continue
					ty = self.type_table._eval_generic_type_expr(param.type_expr, type_args, module_id=schema.module_id)
					if self.type_table.get(ty).kind is TypeKind.UNKNOWN:
						diagnostics.append(
							_tc_diag(
								message=f"unknown type in interface method '{method.name}' parameter '{param.name}'",
								code="E_TYPE_UNKNOWN",
								severity="error",
								span=None,
							)
						)
				# Phase 1 v3 of terminal-`throws`: bare-terminal interface
				# methods (`fn f() throws`) carry `return_type=None` on the
				# schema. They have no return type to validate; skip the
				# return-type kind check. Phase 2 will introduce a separate
				# terminal-form validation.
				if not getattr(method, "declared_terminal_throws", False) and method.return_type is not None:
					ret_ty = self.type_table._eval_generic_type_expr(method.return_type, type_args, module_id=schema.module_id)
					if self.type_table.get(ret_ty).kind is TypeKind.UNKNOWN:
						diagnostics.append(
							_tc_diag(
								message=f"unknown return type in interface method '{method.name}'",
								code="E_TYPE_UNKNOWN",
								severity="error",
								span=None,
							)
						)
			parent_ids = list(getattr(schema, "parent_base_ids", []) or [])
			if len(parent_ids) != len(set(parent_ids)):
				diagnostics.append(
					_tc_diag(
						message=f"interface '{schema.name}' has duplicate parent entries",
						severity="error",
						span=None,
					)
				)
			for pid in parent_ids:
				pdef = self.type_table.get(pid)
				if pdef.kind is not TypeKind.INTERFACE:
					diagnostics.append(
						_tc_diag(
							message=f"interface '{schema.name}' parent must be an interface type",
							severity="error",
							span=None,
						)
					)
			try:
				linear = self.type_table.interface_linearization(_base_id)
			except ValueError as err:
				diagnostics.append(
					_tc_diag(
						message=str(err),
						severity="error",
						span=None,
					)
				)
				continue
			seen_methods: dict[str, TypeId] = {}
			for owner_id in linear:
				owner_schema = self.type_table.interface_bases.get(owner_id)
				for m in list(getattr(owner_schema, "methods", []) or []):
					prev = seen_methods.get(m.name)
					if prev is None:
						seen_methods[m.name] = owner_id
						continue
					if prev != owner_id:
						diagnostics.append(
							_tc_diag(
								message=f"interface '{schema.name}' has duplicate method '{m.name}' across parents",
								severity="error",
								span=None,
							)
						)

	def validate_interface_impls(
		self,
		impls: Sequence[ImplMeta],
		*,
		signatures_by_id: Mapping[FunctionId, FnSignature],
		diagnostics: list[Diagnostic],
	) -> None:
		for impl in impls:
			if impl.trait_expr is None:
				continue
			impl_type_param_ids: dict[str, TypeParamId] = {}
			if impl.methods:
				first_sig = signatures_by_id.get(impl.methods[0].fn_id)
				if first_sig is not None:
					for tp in getattr(first_sig, "impl_type_params", []) or []:
						impl_type_param_ids[tp.name] = tp.id
			trait_type_id = resolve_opaque_type(
				impl.trait_expr,
				self.type_table,
				module_id=impl.def_module,
				type_params=impl_type_param_ids or None,
			)
			interface_inst = self.type_table.get_interface_instance(trait_type_id)
			trait_td = self.type_table.get(trait_type_id)
			if trait_td.kind is not TypeKind.INTERFACE:
				if interface_inst is None:
					continue
				trait_type_id = interface_inst.base_id
				trait_td = self.type_table.get(trait_type_id)
				if trait_td.kind is not TypeKind.INTERFACE:
					continue
			schema = self.type_table.interface_bases.get(trait_type_id)
			if schema is None:
				continue
			interface_type_args = list(getattr(interface_inst, "type_args", []) or [])
			try:
				linear = self.type_table.interface_linearization(trait_type_id)
			except Exception:
				linear = [trait_type_id]
			inst_map: dict[TypeId, TypeId] = {}
			try:
				inst_map = self.type_table.interface_instance_view_map(trait_type_id)
			except Exception:
				inst_map = {}
			method_schemas: dict[str, tuple[TypeId, InterfaceMethodSchema]] = {}
			for owner_id in linear:
				owner_schema = self.type_table.interface_bases.get(owner_id)
				for m in list(getattr(owner_schema, "methods", []) or []):
					method_schemas.setdefault(m.name, (owner_id, m))
			impl_method_names = {m.name for m in impl.methods}
			for method_name in method_schemas:
				if method_name not in impl_method_names:
					diagnostics.append(
						_tc_diag(
							message=f"interface impl for '{schema.name}' missing method '{method_name}'",
							code="E_INTERFACE_METHOD_MISSING",
							severity="error",
							span=impl.loc or Span(),
						)
					)
			if schema.type_params and not interface_type_args:
				diagnostics.append(
					_tc_diag(
						message=f"interface impl for '{schema.name}' must specify type arguments",
						code="E_INTERFACE_IMPL_MISSING_TYPE_ARGS",
						severity="error",
						span=impl.loc or Span(),
					)
				)
				continue
			for method in impl.methods:
				method_owner = method_schemas.get(method.name)
				if method_owner is None:
					diagnostics.append(
						_tc_diag(
							message=f"method '{method.name}' not declared in interface '{schema.name}'",
							code="E_INTERFACE_METHOD_UNKNOWN",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				owner_id, method_schema = method_owner
				if method_schema.type_params:
					diagnostics.append(
						_tc_diag(
							message=f"interface method '{method.name}' type parameters are not supported in impls yet",
							code="E_INTERFACE_METHOD_TYPE_PARAMS_UNSUPPORTED",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				sig = signatures_by_id.get(method.fn_id)
				if sig is None or sig.param_type_ids is None:
					continue
				# Terminal-throws impls have return_type_id=None; allow that.
				if sig.return_type_id is None and not bool(getattr(sig, "declared_terminal_throws", False)):
					continue
				# Terminal-throws compatibility check (before return-type comparison).
				iface_terminal = bool(getattr(method_schema, "declared_terminal_throws", False))
				impl_terminal = bool(getattr(sig, "declared_terminal_throws", False))
				if iface_terminal != impl_terminal:
					if iface_terminal:
						_msg = (
							f"interface impl method '{method.name}' must use bare terminal "
							f"`throws` to match interface declaration (no return type allowed)"
						)
					else:
						_msg = (
							f"interface impl method '{method.name}' uses bare terminal "
							f"`throws` but interface declaration expects a return type"
						)
					diagnostics.append(
						_tc_diag(
							message=_msg,
							code="E_INTERFACE_METHOD_TERMINAL_THROWS_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				if len(sig.param_type_ids) != len(method_schema.params):
					diagnostics.append(
						_tc_diag(
							message=f"interface method '{method.name}' parameter count does not match interface '{schema.name}'",
							code="E_INTERFACE_METHOD_PARAM_COUNT",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				type_args = list(interface_type_args)
				owner_inst_id = inst_map.get(owner_id)
				if owner_inst_id is not None:
					owner_inst = self.type_table.get_interface_instance(owner_inst_id)
					if owner_inst is not None:
						type_args = list(owner_inst.type_args)
				for idx, param in enumerate(method_schema.params):
					if param.name == "self":
						if sig.param_names and idx < len(sig.param_names) and sig.param_names[idx] != "self":
							diagnostics.append(
								_tc_diag(
									message=f"interface method '{method.name}' must use 'self' as the receiver parameter",
									code="E_INTERFACE_METHOD_SELF_NAME",
									severity="error",
									span=method.loc or impl.loc or Span(),
								)
							)
						continue
					expected = self.type_table._eval_generic_type_expr(
						param.type_expr,
						type_args,
						module_id=self.type_table.interface_bases.get(owner_id).module_id if self.type_table.interface_bases.get(owner_id) is not None else schema.module_id,
					)
					if expected != sig.param_type_ids[idx]:
						diagnostics.append(
							_tc_diag(
								message=(
									f"interface method '{method.name}' parameter '{param.name}' "
									f"expects {self._pretty_type_name(expected, current_module=schema.module_id)} "
									f"but got {self._pretty_type_name(sig.param_type_ids[idx], current_module=schema.module_id)}"
								),
								code="E_INTERFACE_METHOD_PARAM_MISMATCH",
								severity="error",
								span=method.loc or impl.loc or Span(),
							)
						)
				owner_schema = self.type_table.interface_bases.get(owner_id)
				owner_module = owner_schema.module_id if owner_schema is not None else schema.module_id
				# Terminal-throws compatibility: interface and impl must match
				# exactly, same as trait methods (Phase 3.5).
				iface_terminal = bool(getattr(method_schema, "declared_terminal_throws", False))
				impl_terminal = bool(getattr(sig, "declared_terminal_throws", False))
				if iface_terminal != impl_terminal:
					if iface_terminal:
						_msg = (
							f"interface impl method '{method.name}' must use bare terminal "
							f"`throws` to match interface declaration (no return type allowed)"
						)
					else:
						_msg = (
							f"interface impl method '{method.name}' uses bare terminal "
							f"`throws` but interface declaration expects a return type"
						)
					diagnostics.append(
						_tc_diag(
							message=_msg,
							code="E_INTERFACE_METHOD_TERMINAL_THROWS_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				# Terminal-throws methods have no return type to compare.
				if iface_terminal:
					continue
				expected_ret = self.type_table._eval_generic_type_expr(
					method_schema.return_type,
					type_args,
					module_id=owner_module,
				)
				if expected_ret != sig.return_type_id:
					diagnostics.append(
						_tc_diag(
							message=(
								f"interface method '{method.name}' return type expects "
								f"{self._pretty_type_name(expected_ret, current_module=schema.module_id)} "
								f"but got {self._pretty_type_name(sig.return_type_id, current_module=schema.module_id)}"
							),
							code="E_INTERFACE_METHOD_RETURN_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)

	def validate_trait_impls(
		self,
		impls: Sequence[ImplMeta],
		*,
		signatures_by_id: Mapping[FunctionId, FnSignature],
		trait_index: GlobalTraitIndex | None,
		diagnostics: list[Diagnostic],
	) -> None:
		def _dealias_zero_param_type(ty: TypeId, *, _seen: set[tuple[str | None, str]] | None = None) -> TypeId:
			seen = _seen if _seen is not None else set()
			td = self.type_table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner = _dealias_zero_param_type(td.param_types[0], _seen=seen)
				return self.type_table.ensure_ref_mut(inner) if td.ref_mut else self.type_table.ensure_ref(inner)
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem = _dealias_zero_param_type(td.param_types[0], _seen=seen)
				return self.type_table.new_array(elem)
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None and inst.type_args:
				new_args = [_dealias_zero_param_type(arg, _seen=seen) for arg in inst.type_args]
				if any(self.type_table.has_typevar(arg) for arg in new_args):
					return self.type_table.ensure_struct_template(inst.base_id, new_args)
				return self.type_table.ensure_struct_instantiated(inst.base_id, new_args)
			vinst = self.type_table.get_variant_instance(ty)
			if vinst is not None and vinst.type_args:
				new_args = [_dealias_zero_param_type(arg, _seen=seen) for arg in vinst.type_args]
				if any(self.type_table.has_typevar(arg) for arg in new_args):
					return self.type_table.ensure_variant_template(vinst.base_id, new_args)
				return self.type_table.ensure_variant_instantiated(vinst.base_id, new_args)
			mod = td.module_id
			name = td.name
			alias_def = self.type_table.lookup_type_alias(module_id=mod, name=name)
			if alias_def is None:
				return ty
			alias_params, alias_target, _loc = alias_def
			if alias_params:
				return ty
			alias_key = (mod, name)
			if alias_key in seen:
				return ty
			resolved = resolve_opaque_type(alias_target, self.type_table, module_id=mod, type_params=None, allow_generic_base=True)
			return _dealias_zero_param_type(resolved, _seen=seen | {alias_key})

		# Collect the set of (module_id, name) pairs for types that have
		# an explicit `implement Copy for T` in the current compilation.
		# Struct fields whose type is a struct NOT in this set are
		# non-Copy — even if their internal layout is raw-pointer-only
		# (e.g. Arc wraps a RawBuffer of raw ptrs, but Arc itself is
		# Destructible and must not be bit-copied).
		_copy_declared: set[tuple[str | None, str]] = set()
		for _impl in impls:
			_tk = getattr(_impl, "trait_key", None)
			if _tk is None:
				continue
			if _is_core_copy_trait_key(_tk):
				_ttd = self.type_table.get(_impl.target_type_id)
				_copy_declared.add((_ttd.module_id, _ttd.name))

		# Compiler-known Copy types whose Copy semantics are not
		# expressible by the structural prover (refcount-backed values
		# behaving as O(1) bit-copyable at the source level).  These
		# are the only types allowed to declare `implement Copy` without
		# passing the field-level structural check.
		_COMPILER_KNOWN_COPY_KINDS = {TypeKind.DIAGNOSTICVALUE}
		_COMPILER_KNOWN_COPY_SCALARS = {"String"}

		def _is_compiler_known_copy(tid: TypeId) -> bool:
			td = self.type_table.get(tid)
			if td.kind in _COMPILER_KNOWN_COPY_KINDS:
				return True
			if td.kind is TypeKind.SCALAR and td.name in _COMPILER_KNOWN_COPY_SCALARS:
				return True
			return False

		def _is_structurally_copy(tid: TypeId, *, seen: set[TypeId], covered_tparams: frozenset[TypeParamId] = frozenset()) -> bool:
			td = self.type_table.get(tid)
			if _is_compiler_known_copy(tid):
				return True
			if td.kind is TypeKind.SCALAR:
				return td.name != "String"
			if td.kind in (TypeKind.REF, TypeKind.RAW_PTR, TypeKind.FUNCTION, TypeKind.VOID):
				return True
			if td.kind is TypeKind.TYPEVAR:
				# A TYPEVAR T is treated as Copy only when the enclosing
				# generic impl's `require T is Copy` clause covers it.
				# This is the single gate that lets generic Copy impls
				# pass the structural prover: no require clause → any T
				# in a stored field flips this to non-Copy and rejects
				# the impl, matching how concrete instantiations behave.
				return td.type_param_id is not None and td.type_param_id in covered_tparams
			if td.kind in (TypeKind.ARRAY, TypeKind.FNRESULT, TypeKind.ERROR, TypeKind.DIAGNOSTICVALUE, TypeKind.UNKNOWN, TypeKind.FORWARD_NOMINAL):
				return False
			if td.kind is TypeKind.INTERFACE:
				return False
			if tid in seen:
				return False
			seen.add(tid)
			try:
				if td.kind is TypeKind.STRUCT:
					# If this struct doesn't have its own Copy impl, it's
					# non-Copy — don't look through its field layout.
					if (td.module_id, td.name) not in _copy_declared:
						return False
					inst = self.type_table.get_struct_instance(tid)
					if inst is None:
						return False
					return all(_is_structurally_copy(fty, seen=seen, covered_tparams=covered_tparams) for fty in inst.field_types)
				if td.kind is TypeKind.VARIANT:
					inst = self.type_table.get_variant_instance(tid)
					if inst is None:
						return False
					for arm in inst.arms:
						for fty in arm.field_types:
							if not _is_structurally_copy(fty, seen=seen, covered_tparams=covered_tparams):
								return False
					return True
				return False
			finally:
				seen.discard(tid)

		_copy_validate_module_packages = getattr(self.type_table, "module_packages", None)
		_copy_validate_default_package = getattr(self.type_table, "package_id", None)

		def _collect_copy_covered_tparams(expr: object, *, default_module: str | None) -> frozenset[TypeParamId]:
			# Walk conjunctive `require` facts to find `T is Copy` clauses
			# whose trait resolves to std.core.Copy specifically.
			# Name-only matching would let a shadowing local `trait Copy`
			# in the impl's module satisfy a `require T is Copy` clause
			# on a `core.Copy` impl — unsound.  Resolving via
			# `trait_key_from_expr` anchors to the canonical trait key.
			# Disjunctions and negations don't give us an unconditional
			# Copy guarantee for any single subject, so ignore them.
			covered: set[TypeParamId] = set()
			def _walk(e: object) -> None:
				if isinstance(e, parser_ast.TraitIs):
					subj = e.subject
					if not isinstance(subj, TypeParamId):
						return
					key = trait_key_from_expr(
						e.trait,
						default_module=default_module,
						default_package=_copy_validate_default_package,
						module_packages=_copy_validate_module_packages,
					)
					if key is None:
						return
					if _is_core_copy_trait_key(key):
						covered.add(subj)
					return
				if isinstance(e, parser_ast.TraitAnd):
					_walk(e.left)
					_walk(e.right)
			if expr is not None:
				_walk(expr)
			return frozenset(covered)

		if trait_index is None:
			return
		for impl in impls:
			trait_key = getattr(impl, "trait_key", None)
			if trait_key is None:
				continue
			if _is_core_copy_trait_key(trait_key):
				# Generic Copy impls (e.g. `implement<T> Copy for
				# Optional<T> require T is Copy`) still go through the
				# structural prover — the prover treats TYPEVARs
				# covered by a `require T is Copy` clause as Copy, and
				# rejects any stored-field TYPEVAR not so covered.
				# Phantom generic structs (no stored T) pass with no
				# require clause.  Structs with wrappers that don't
				# propagate Copy-dep (RawPtr<T>, &T, fn ptrs) also
				# pass without require clauses.
				covered = _collect_copy_covered_tparams(
					getattr(impl, "require_expr", None),
					default_module=getattr(impl, "def_module", None),
				)
				if not _is_structurally_copy(impl.target_type_id, seen=set(), covered_tparams=covered):
					diagnostics.append(
						_tc_diag(
							message="core.Copy impl target must be structurally Copy in v1",
							code="E_COPY_IMPL_NONCOPY_TARGET",
							severity="error",
							span=impl.loc or Span(),
						)
					)
			trait_def = trait_index.traits_by_id.get(trait_key)
			if trait_def is None:
				continue
			trait_module_for_types = getattr(trait_key, "module", None) or getattr(trait_def, "module_id", None) or impl.def_module
			method_sigs = {m.name: m for m in list(getattr(trait_def, "methods", []) or [])}
			for method in impl.methods:
				trait_method = method_sigs.get(method.name)
				if trait_method is None:
					continue
				sig = signatures_by_id.get(method.fn_id)
				if sig is None or sig.param_type_ids is None:
					continue
				# Terminal-throws impls have return_type_id=None; allow that.
				if sig.return_type_id is None and not bool(getattr(sig, "declared_terminal_throws", False)):
					continue
				type_params: dict[str, TypeId] = {"Self": impl.target_type_id}
				trait_param_names = list(getattr(trait_def, "type_params", []) or [])
				trait_args = list(getattr(impl, "trait_args", []) or [])
				for idx, tp_name in enumerate(trait_param_names):
					if idx < len(trait_args):
						type_params[tp_name] = trait_args[idx]
				def _resolve_trait_method_type(raw_expr: object) -> TypeId:
					expr_mod = getattr(raw_expr, "module_id", None)
					expr_alias = getattr(raw_expr, "module_alias", None)
					resolve_mod = expr_mod
					if resolve_mod is None and isinstance(expr_alias, str) and expr_alias:
						resolve_mod = expr_alias
					if resolve_mod is None:
						resolve_mod = trait_module_for_types
					return resolve_opaque_type(
						raw_expr,
						self.type_table,
						module_id=resolve_mod,
						type_params=type_params,
					)
				expected_params: list[TypeId] = []
				for p in list(getattr(trait_method, "params", []) or []):
					expected_params.append(_resolve_trait_method_type(p.type_expr))
				expected_ret = _resolve_trait_method_type(getattr(trait_method, "return_type", None))
				if len(expected_params) != len(sig.param_type_ids):
					diagnostics.append(
						_tc_diag(
							message=(
								f"trait impl method '{method.name}' parameter count does not match trait "
								f"'{getattr(trait_key, 'module', None)}.{getattr(trait_key, 'name', None)}'"
							),
							code="E_TRAIT_METHOD_PARAM_COUNT",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				param_mismatch = False
				for idx, (want, have) in enumerate(zip(expected_params, sig.param_type_ids)):
					want_cmp = _dealias_zero_param_type(want)
					have_cmp = _dealias_zero_param_type(have)
					if want_cmp != have_cmp:
						param_mismatch = True
						diagnostics.append(
							_tc_diag(
								message=(
									f"trait impl method '{method.name}' parameter {idx + 1} expects "
									f"{self._pretty_type_name(want, current_module=impl.def_module)} but got "
									f"{self._pretty_type_name(have, current_module=impl.def_module)}"
								),
								code="E_TRAIT_METHOD_PARAM_MISMATCH",
								severity="error",
								span=method.loc or impl.loc or Span(),
							)
						)
						break
				if param_mismatch:
					continue
				# Phase 3.5: terminal-throws compatibility check runs before
				# the return type comparison. A terminal-throws mismatch (one
				# side bare `throws`, the other `-> T`) produces a more specific
				# diagnostic than the generic return-type mismatch, and the
				# return-type difference is a consequence of the terminal shape
				# difference. Check first, continue on mismatch.
				trait_terminal = bool(getattr(trait_method, "declared_terminal_throws", False))
				impl_terminal = bool(getattr(sig, "declared_terminal_throws", False))
				if trait_terminal != impl_terminal:
					if trait_terminal:
						_msg = (
							f"trait impl method '{method.name}' must use bare terminal "
							f"`throws` to match trait declaration (no return type allowed)"
						)
					else:
						_msg = (
							f"trait impl method '{method.name}' uses bare terminal "
							f"`throws` but trait declaration expects a return type"
						)
					diagnostics.append(
						_tc_diag(
							message=_msg,
							code="E_TRAIT_METHOD_TERMINAL_THROWS_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				# Both sides agree on terminal shape — if both are terminal
				# throws, there is no return type to compare; skip straight
				# to nothrow/throws checking.
				if trait_terminal:
					continue
				expected_ret_cmp = _dealias_zero_param_type(expected_ret)
				actual_ret_cmp = _dealias_zero_param_type(sig.return_type_id)
				if expected_ret_cmp != actual_ret_cmp:
					diagnostics.append(
						_tc_diag(
							message=(
								f"trait impl method '{method.name}' return type expects "
								f"{self._pretty_type_name(expected_ret, current_module=impl.def_module)} but got "
								f"{self._pretty_type_name(sig.return_type_id, current_module=impl.def_module)}"
							),
							code="E_TRAIT_METHOD_RETURN_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)
					continue
				declared_nothrow = bool(getattr(trait_method, "declared_nothrow", False))
				declared_throws = bool(getattr(trait_method, "declared_throws", False))
				actual_can_throw = bool(getattr(sig, "declared_can_throw", False))
				# Trait impl methods inherit trait nothrow when omitted in source:
				# omitted marker => declared_can_throw defaults True, declared_throws=False.
				# Preserve explicit `throws` mismatch reporting by only inheriting on omitted throws.
				if declared_nothrow and not declared_throws and actual_can_throw and not bool(getattr(sig, "declared_throws", False)):
					actual_can_throw = False
				# Compatibility rule:
				# - Trait `nothrow` methods require non-throwing impl methods.
				# - Throw-capable trait methods may be implemented by either
				#   throw-capable or nothrow impl methods.
				if declared_nothrow and not declared_throws and actual_can_throw:
					diagnostics.append(
						_tc_diag(
							message=(
								f"trait impl method '{method.name}' throw behavior does not match trait declaration"
							),
							code="E_TRAIT_METHOD_THROW_MISMATCH",
							severity="error",
							span=method.loc or impl.loc or Span(),
						)
					)

	def check_function(
		self,
		fn_id: FunctionId,
		body: H.HBlock,
		param_types: Mapping[str, TypeId] | None = None,
		param_mutable: Mapping[str, bool] | None = None,
		return_type: TypeId | None = None,
		preseed_type_params: Mapping[str, TypeId] | None = None,
		preseed_binding_types: Mapping[int, TypeId] | None = None,
		preseed_binding_names: Mapping[int, str] | None = None,
		preseed_binding_mutable: Mapping[int, bool] | None = None,
		preseed_binding_place_kind: Mapping[int, PlaceKind] | None = None,
		preseed_scope_env: Mapping[str, TypeId] | None = None,
		preseed_scope_bindings: Mapping[str, int] | None = None,
		signatures_by_id: Mapping[FunctionId, FnSignature] | None = None,
		function_keys_by_fn_id: Mapping[FunctionId, FunctionKey] | None = None,
		callable_registry: CallableRegistry | None = None,
		impl_index: GlobalImplIndex | None = None,
		trait_index: GlobalTraitIndex | None = None,
		trait_impl_index: GlobalTraitImplIndex | None = None,
		trait_scope_by_module: Mapping[str, list[TraitKey]] | None = None,
		trait_key_for_id: Callable[[int], TraitKey | None] | None = None,
		linked_world: LinkedWorld | None = None,
		require_env: RequireEnv | None = None,
		visible_modules: Optional[Tuple[ModuleId, ...]] = None,
		current_module: ModuleId = 0,
		visibility_provenance: Mapping[ModuleId, tuple[str, ...]] | None = None,
		visibility_imports: set[str] | None = None,
	) -> TypeCheckResult:
		# Best-effort current module id in canonical string form.
		#
		# This is required for correct module-scoped nominal type resolution
		# (e.g., `Point(...)` inside module `a.geom` must refer to `a.geom:Point`
		# even if another module also defines `Point`).
		current_module_name: str | None = None
		current_module_name = fn_id.module or "main"
		sig = signatures_by_id.get(fn_id) if signatures_by_id is not None else None
		if isinstance(signatures_by_id, ChainMap):
			derived_sig = signatures_by_id.maps[0].get(fn_id)
			base_sig = signatures_by_id.maps[1].get(fn_id)
			if derived_sig is not None:
				sig = derived_sig
			elif base_sig is not None:
				sig = base_sig
		fn_sig = sig

		def _fixed_width_allowed(module_name: str | None) -> bool:
			if module_name is None:
				return False
			return module_name.startswith("lang.abi.") or module_name.startswith("std.")

		def _reject_fixed_width_type_expr(raw: object, module_name: str | None, span: Span | None) -> bool:
			# Return True if a fixed-width type was rejected.
			if raw is None:
				return False
			name = None
			args = None
			if hasattr(raw, "name") and hasattr(raw, "args"):
				name = getattr(raw, "name", None)
				args = getattr(raw, "args", None)
			elif isinstance(raw, str):
				name = raw
				args = None
			if name in FIXED_WIDTH_TYPE_NAMES and not _fixed_width_allowed(module_name):
				diagnostics.append(
					_tc_diag(
						message=f"fixed-width type '{name}' is reserved in v1; use Int/Uint/Float or Byte",
						code="E_FIXED_WIDTH_RESERVED",
						severity="error",
						span=span or Span(),
					)
				)
				return True
			if args:
				for arg in list(args):
					if _reject_fixed_width_type_expr(arg, module_name, span):
						return True
			return False
		visibility_provenance = visibility_provenance or {}
		visibility_imports = visibility_imports if visibility_imports is not None else None
		self._seed_binding_id_counter(body)
		if preseed_binding_types:
			max_preseed = max(preseed_binding_types)
			if self._next_binding_id <= max_preseed:
				self._next_binding_id = max_preseed + 1
		if callable_registry is not None:
			has_call = False

			def _scan_expr(expr: H.HExpr) -> None:
				nonlocal has_call
				if isinstance(expr, (H.HCall, H.HMethodCall, H.HInvoke)):
					has_call = True
				if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HLambda):
					lam = expr.fn
					if getattr(lam, "body_expr", None) is not None:
						_scan_expr(lam.body_expr)
					if getattr(lam, "body_block", None) is not None:
						_scan_block(lam.body_block)
				for child in getattr(expr, "__dict__", {}).values():
					if isinstance(child, H.HExpr):
						_scan_expr(child)
					elif isinstance(child, H.HBlock):
						_scan_block(child)
					elif isinstance(child, list):
						for it in child:
							if isinstance(it, H.HExpr):
								_scan_expr(it)
							elif isinstance(it, H.HBlock):
								_scan_block(it)
							elif isinstance(it, H.HNode):
								_scan_expr(it)

			def _scan_block(block: H.HBlock) -> None:
				for st in block.statements:
					if isinstance(st, H.HExprStmt):
						_scan_expr(st.expr)
					elif isinstance(st, H.HReturn) and st.value is not None:
						_scan_expr(st.value)
					else:
						for child in getattr(st, "__dict__", {}).values():
							if isinstance(child, H.HExpr):
								_scan_expr(child)
							elif isinstance(child, H.HBlock):
								_scan_block(child)
							elif isinstance(child, list):
								for it in child:
									if isinstance(it, H.HExpr):
										_scan_expr(it)
									elif isinstance(it, H.HBlock):
										_scan_block(it)

			_scan_block(body)
			if has_call:
				H.assign_callsite_ids(body)

		next_callsite_id: int | None = None

		def _max_callsite_id(block: H.HBlock) -> int:
			highest = -1

			def _walk_expr(expr: H.HExpr) -> None:
				nonlocal highest
				csid = getattr(expr, "callsite_id", None)
				if isinstance(csid, int):
					highest = max(highest, csid)
				for child in getattr(expr, "__dict__", {}).values():
					if isinstance(child, H.HExpr):
						_walk_expr(child)
					elif isinstance(child, H.HBlock):
						_walk_block(child)
					elif isinstance(child, list):
						for it in child:
							if isinstance(it, H.HExpr):
								_walk_expr(it)
							elif isinstance(it, H.HBlock):
								_walk_block(it)
							elif isinstance(it, H.HNode):
								_walk_expr(it)

			def _walk_block(b: H.HBlock) -> None:
				for st in b.statements:
					if isinstance(st, H.HExprStmt):
						_walk_expr(st.expr)
					elif isinstance(st, H.HReturn) and st.value is not None:
						_walk_expr(st.value)
					else:
						for child in getattr(st, "__dict__", {}).values():
							if isinstance(child, H.HExpr):
								_walk_expr(child)
							elif isinstance(child, H.HBlock):
								_walk_block(child)
							elif isinstance(child, list):
								for it in child:
									if isinstance(it, H.HExpr):
										_walk_expr(it)
									elif isinstance(it, H.HBlock):
										_walk_block(it)

			_walk_block(block)
			return highest

		def _alloc_callsite_id() -> int:
			nonlocal next_callsite_id
			if next_callsite_id is None:
				next_callsite_id = _max_callsite_id(body) + 1
			csid = next_callsite_id
			next_callsite_id += 1
			return csid

		def _format_visibility_chain(chain: tuple[str, ...], max_hops: int = 4) -> str:
			if not chain:
				return "<unknown>"
			if len(chain) == 1:
				return f"{chain[0]} (self)"
			nodes = list(chain)
			if len(nodes) - 1 > max_hops:
				nodes = list(chain[: max_hops + 1])
				nodes.append("...")
			parts = [nodes[0]]
			for idx in range(1, len(nodes)):
				if idx == 1:
					if visibility_imports is None:
						label = "visible->"
					else:
						label = "import->" if nodes[idx] in visibility_imports else "reexport->"
				else:
					label = "reexport->"
				parts.append(f"{label} {nodes[idx]}")
			return " ".join(parts)

		def _visibility_note(module_id: ModuleId) -> str | None:
			chain = visibility_provenance.get(module_id)
			if not chain:
				return None
			return f"visible via: {_format_visibility_chain(chain)}"

		# Arc runtime boundary — central helpers for intrinsic-aware
		# call-info writes.
		#
		# Several sites in this file (plus `driftc.py::_rewrite_call_
		# targets`) override `call_info_by_callsite_id[csid]` with a
		# `CallTarget.direct(__inst__fn_id)` after monomorphization
		# decides on a concrete template instance.  For `@intrinsic`
		# generic methods (Arc.clone / Arc.get /
		# Arc::Destructible::destroy / Arc.as_interface) the template
		# has no body; their call sites must retain the INTRINSIC
		# target set by method-resolution rewrite so hir_to_mir can
		# redirect to the matching `_arc_*_impl<T>` helper.
		#
		# `_template_is_intrinsic_generic` is the single predicate
		# every override site consults.  `_write_call_info_respecting_
		# intrinsic` is the single writer: if the existing CallInfo
		# already has an INTRINSIC target (or the template is known-
		# intrinsic) it leaves the entry alone; otherwise it performs
		# the override.
		def _template_is_intrinsic_generic(template_key: "FunctionKey | None") -> bool:
			if template_key is None or signatures_by_id is None:
				return False
			for _fid, _sig in signatures_by_id.items():
				if _fid.module != template_key.module_path:
					continue
				if _fid.name != template_key.name:
					continue
				return bool(getattr(_sig, "is_intrinsic", False))
			return False

		def _write_call_info_respecting_intrinsic(
			csid: int,
			new_info: CallInfo,
			*,
			template_key: "FunctionKey | None" = None,
		) -> None:
			# Preserve intrinsic dispatch: if the method-resolution
			# rewrite left an INTRINSIC target at this callsite, do
			# NOT overwrite with a monomorphized Direct target.
			existing = call_info_by_callsite_id.get(csid)
			if existing is not None and existing.target.kind is CallTargetKind.INTRINSIC:
				return
			if _template_is_intrinsic_generic(template_key):
				return
			call_info_by_callsite_id[csid] = new_info

		def _record_call_info(expr: H.HExpr, info: CallInfo) -> int:
			csid = getattr(expr, "callsite_id", None)
			if not isinstance(csid, int):
				csid = _alloc_callsite_id()
				expr.callsite_id = csid
			node_id = getattr(expr, "node_id", None)
			owner = callsite_owner_node_id.get(csid)
			existing = call_info_by_callsite_id.get(csid)
			if owner is None and existing is None:
				call_info_by_callsite_id[csid] = info
				callsite_owner_node_id[csid] = int(node_id) if isinstance(node_id, int) else -1
				return csid
			if owner is not None and isinstance(node_id, int) and owner == node_id:
				call_info_by_callsite_id[csid] = info
				return csid
			if existing == info:
				if owner is None:
					callsite_owner_node_id[csid] = int(node_id) if isinstance(node_id, int) else -1
				return csid
			new_csid = _alloc_callsite_id()
			expr.callsite_id = new_csid
			call_info_by_callsite_id[new_csid] = info
			callsite_owner_node_id[new_csid] = int(node_id) if isinstance(node_id, int) else -1
			return new_csid

		module_ids_by_name: dict[str, ModuleId] = {}
		for mod_id, chain in visibility_provenance.items():
			if chain:
				module_ids_by_name.setdefault(chain[-1], mod_id)
		prelude_module_id = module_ids_by_name.get("lang.core")

		def _visible_modules_for_free_call(module_name: str | None) -> tuple[ModuleId, ...]:
			if module_name is not None:
				mod_id = module_ids_by_name.get(module_name)
				if mod_id is not None:
					return (mod_id,)
				if module_packages is not None and module_name in module_packages:
					return tuple(visible_modules or (current_module,))
				# When no provenance map exists (unit-test harness), fall back to
				# the provided visible module set instead of hard-failing.
				if not visibility_provenance:
					return tuple(visible_modules or (current_module,))
				return ()
			modules = [current_module]
			if prelude_module_id is not None and prelude_module_id != current_module:
				modules.append(prelude_module_id)
			return tuple(modules)

		next_node_id = assign_node_ids(body)
		def _assign_node_id(node: H.HNode) -> None:
			nonlocal next_node_id
			if getattr(node, "node_id", 0):
				return
			if is_dataclass(node) and getattr(node, "__dataclass_params__", None) and node.__dataclass_params__.frozen:
				object.__setattr__(node, "node_id", next_node_id)
			else:
				node.node_id = next_node_id
			next_node_id += 1

		def _assign_place_expr_ids(place_expr: H.HPlaceExpr) -> None:
			_assign_node_id(place_expr)
			for proj in place_expr.projections:
				_assign_node_id(proj)
		scope_env: List[Dict[str, TypeId]] = [dict()]
		scope_bindings: List[Dict[str, int]] = [dict()]
		expr_types: Dict[int, TypeId] = {}
		iface_coercions: Dict[int, TypeId] = {}
		binding_for_var: Dict[int, int] = {}
		binding_types: Dict[int, TypeId] = {}
		binding_names: Dict[int, str] = {}
		# G3: bindings introduced by `match &Variant { Ctor(x) => ... }`
		# whose type is `Ref<Copy>` (e.g. binder for an `Int` payload
		# field).  At three syntactic positions inside the arm body,
		# a bare `HVar` referring to such a binder is rewritten to
		# `HUnary(DEREF, HVar)` so HIR→MIR's existing DEREF lowering
		# emits a `LoadRef`:
		#   - HBinary operands (`n + 1`, `n > 0`).
		#   - HTernary condition (`b ? a : c`).
		#   - Match arm result / trailing expr (`val k: Int = match
		#     &v { Active(n) => { n } }`).
		# Other contexts — function arguments, return statements,
		# nested `match` over the binder, taking its address — are
		# *not* rewritten.  Those uses see the strict `&FieldType`
		# binder type and obey the usual borrow rules.  Stdlib's
		# existing `*code` form continues to work because the
		# binder's stored type is unchanged (`Ref<Copy>`).
		copy_arm_binder_ids: Set[int] = set()
		# Binding mutability (val/var) keyed by binding id.
		#
		# MVP borrow rules depend on this:
		#   - `&mut x` requires `x` to be declared mutable (`var`).
		binding_mutable: Dict[int, bool] = {}
		# Binding identity kind (param vs local). Binding ids share a single counter,
		# but we still track the origin kind to keep place reasoning explicit.
		binding_place_kind: Dict[int, PlaceKind] = {}
		pending_lambda_by_binding: Dict[int, H.HLambda] = {}
		# Block-scope const binding ids: these re-materialize at each use site,
		# so the Copy check is skipped (non-Copy types like String are allowed).
		local_const_binding_ids: Set[int] = set()
		# Track whether a binding was declared as &mut T (param-only for now).
		binding_param_ref_mut: Dict[int, bool] = {}
		if preseed_binding_place_kind:
			for bid, kind in preseed_binding_place_kind.items():
				binding_place_kind[bid] = kind
		if preseed_binding_types:
			for bid, ty in preseed_binding_types.items():
				binding_types[bid] = ty
				binding_place_kind.setdefault(bid, PlaceKind.LOCAL)
		if preseed_binding_names:
			for bid, name in preseed_binding_names.items():
				binding_names[bid] = name
		if preseed_binding_mutable:
			for bid, is_mut in preseed_binding_mutable.items():
				binding_mutable[bid] = bool(is_mut)
		if preseed_scope_env:
			scope_env[-1].update(preseed_scope_env)
		if preseed_scope_bindings:
			scope_bindings[-1].update(preseed_scope_bindings)
		def _receiver_base_lookup(hv: object) -> Optional[PlaceBase]:
			bid = getattr(hv, "binding_id", None)
			if bid is None:
				return None
			kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
			name = hv.name if hasattr(hv, "name") else str(hv)
			return PlaceBase(kind=kind, local_id=bid, name=name)

		def _receiver_place(expr: H.HExpr) -> Optional[Place]:
			if isinstance(expr, H.HBorrow):
				return place_from_expr(expr.subject, base_lookup=_receiver_base_lookup)
			return place_from_expr(expr, base_lookup=_receiver_base_lookup)

		def _receiver_can_mut_borrow(expr: H.HExpr, place: Optional[Place], recv_ty_hint: Optional[TypeId] = None) -> bool:
			ref_ty = recv_ty_hint if recv_ty_hint is not None else type_expr(expr, used_as_value=False)
			if ref_ty is not None:
				ref_def = self.type_table.get(ref_ty)
				if ref_def.kind is TypeKind.REF and bool(ref_def.ref_mut):
					return True
			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				base_ty = type_expr(expr.base, used_as_value=False)
				if base_ty is not None:
					base_def = self.type_table.get(base_ty)
					if base_def.kind is TypeKind.REF and bool(base_def.ref_mut):
						return True
			if isinstance(expr, H.HBorrow):
				return bool(expr.is_mut)
			if place is None:
				return False
			if place.base.local_id is not None:
				base_ty = binding_types.get(place.base.local_id)
				if base_ty is not None:
					base_def = self.type_table.get(base_ty)
					if base_def.kind is TypeKind.REF and bool(base_def.ref_mut):
						return True
			has_deref = any(isinstance(p, DerefProj) for p in place.projections)
			if not has_deref:
				if place.base.local_id is None:
					return False
				return bool(binding_mutable.get(place.base.local_id, False))
			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				# Special-case Error.attrs["key"] in place-expr form.
				for idx, proj in enumerate(expr.projections):
					if not (isinstance(proj, H.HPlaceField) and proj.name == "attrs"):
						continue
					if idx + 1 >= len(expr.projections) or not isinstance(expr.projections[idx + 1], H.HPlaceIndex):
						continue
					# Only support direct Error.attrs["key"] access (no trailing projections).
					if idx + 2 != len(expr.projections):
						continue
					key_proj = expr.projections[idx + 1]
					sub_ty = type_expr(expr.base, used_as_value=False)
					sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is TypeKind.REF and sub_def.param_types:
						sub_ty = sub_def.param_types[0]
						sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is TypeKind.ERROR:
						key_ty = type_expr(key_proj.index)
						if self.type_table.get(key_ty).name != "String":
							diagnostics.append(
								_tc_diag(
									message="Error.attrs expects a String key",
									severity="error",
									span=getattr(key_proj.index, "loc", Span()),
									code="E-ERROR-ATTR-KEY-NOT-STRING",
								)
							)
						return record_expr(expr, self._dv)
					diagnostics.append(
						_tc_diag(
							message="attrs access is only supported on Error values",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)

				cur = type_expr(expr.base, used_as_value=False)
				for pr in expr.projections:
					if isinstance(pr, H.HPlaceDeref):
						if cur is None:
							return False
						ptr_def = self.type_table.get(cur)
						if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
							return False
						cur = ptr_def.param_types[0] if ptr_def.param_types else None
					elif isinstance(pr, H.HPlaceField):
						if cur is None:
							return False
						td = self.type_table.get(cur)
						if td.kind is TypeKind.STRUCT:
							info = self.type_table.struct_field(cur, pr.name)
							if info is not None:
								_, cur = info
					elif isinstance(pr, H.HPlaceIndex):
						if cur is None:
							return False
						td = self.type_table.get(cur)
						if td.kind is TypeKind.ARRAY and td.param_types:
							cur = td.param_types[0]
				return True
			if isinstance(expr, H.HUnary) and expr.op is H.UnaryOp.DEREF:
				ptr_ty = type_expr(expr.expr, used_as_value=False)
				if ptr_ty is None:
					return False
				ptr_def = self.type_table.get(ptr_ty)
				return ptr_def.kind is TypeKind.REF and ptr_def.ref_mut
			return True

		def _receiver_preference(
			self_mode: SelfMode | None,
			*,
			receiver_is_lvalue: bool,
			receiver_can_mut_borrow: bool,
			autoborrow: Optional[SelfMode],
		) -> int | None:
			if self_mode is None:
				return None
			if not receiver_is_lvalue:
				if self_mode is SelfMode.SELF_BY_VALUE:
					return 1
				if self_mode is SelfMode.SELF_BY_REF:
					# Phase 1: allow auto-borrow of rvalue receivers for &self methods.
					return 0
				return -1
			if self_mode is SelfMode.SELF_BY_REF:
				return 1
			if self_mode is SelfMode.SELF_BY_REF_MUT:
				if autoborrow is SelfMode.SELF_BY_REF_MUT and not receiver_can_mut_borrow:
					return -1
				return 2
			if self_mode is SelfMode.SELF_BY_VALUE:
				return 0
			return None

		def _infer_receiver_arg_type(
			self_mode: SelfMode | None,
			recv_ty: TypeId,
			*,
			receiver_is_lvalue: bool,
			receiver_can_mut_borrow: bool,
		) -> TypeId:
			if self_mode is None:
				return recv_ty
			td_recv = self.type_table.get(recv_ty)
			if self_mode is SelfMode.SELF_BY_REF:
				if td_recv.kind is TypeKind.REF and not td_recv.ref_mut:
					return recv_ty
				return self.type_table.ensure_ref(recv_ty)
			if self_mode is SelfMode.SELF_BY_REF_MUT:
				if td_recv.kind is TypeKind.REF and td_recv.ref_mut:
					return recv_ty
				if receiver_can_mut_borrow:
					return self.type_table.ensure_ref_mut(recv_ty)
				return recv_ty
			return recv_ty

		def _self_mode_from_sig(sig: FnSignature) -> SelfMode:
			param_type_ids = getattr(sig, "param_type_ids", None)
			if param_type_ids is None:
				param_type_ids = list(getattr(sig, "param_types", ()) or ())
			if param_type_ids:
				param0 = self.type_table.get(param_type_ids[0])
				if param0.kind is TypeKind.REF:
					return SelfMode.SELF_BY_REF_MUT if param0.ref_mut else SelfMode.SELF_BY_REF
			return SelfMode.SELF_BY_VALUE
		# Borrow exclusivity (MVP): tracked within a single statement/expression.
		#
		# Key by Place (not binding id) so this mechanism naturally extends to
		# projections once we support borrowing from `x.field`, `arr[i]`, `*p`.
		#
		# Value is "shared" or "mut". This is intentionally shallow (no lifetimes)
		# but prevents the worst footguns:
		#   - multiple `&x` in a statement is OK
		#   - `&mut x` conflicts with any other borrow of `x` in the same statement
		#   - `&x` conflicts with a prior `&mut x` in the same statement
		borrows_in_stmt: Dict[Place, str] = {}
		borrow_expr_ids_in_stmt: set[int] = set()
		def _ref_param_info(param_ty: TypeId) -> tuple[bool, TypeId] | None:
			pdef = self.type_table.get(param_ty)
			if pdef.kind is not TypeKind.REF or not pdef.param_types:
				return None
			return bool(pdef.ref_mut), pdef.param_types[0]

		def _dealias_zero_param_type(ty: TypeId, *, _seen: set[tuple[str | None, str]] | None = None) -> TypeId:
			seen = _seen if _seen is not None else set()
			td = self.type_table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner = _dealias_zero_param_type(td.param_types[0], _seen=seen)
				return self.type_table.ensure_ref_mut(inner) if td.ref_mut else self.type_table.ensure_ref(inner)
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem = _dealias_zero_param_type(td.param_types[0], _seen=seen)
				return self.type_table.new_array(elem)
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None and inst.type_args:
				new_args = [_dealias_zero_param_type(arg, _seen=seen) for arg in inst.type_args]
				return self.type_table.ensure_struct_template(inst.base_id, new_args) if any(self.type_table.has_typevar(arg) for arg in new_args) else self.type_table.ensure_struct_instantiated(inst.base_id, new_args)
			vinst = self.type_table.get_variant_instance(ty)
			if vinst is not None and vinst.type_args:
				new_args = [_dealias_zero_param_type(arg, _seen=seen) for arg in vinst.type_args]
				return self.type_table.ensure_variant_template(vinst.base_id, new_args) if any(self.type_table.has_typevar(arg) for arg in new_args) else self.type_table.ensure_variant_instantiated(vinst.base_id, new_args)
			mod = td.module_id
			name = td.name
			alias_def = self.type_table.lookup_type_alias(module_id=mod, name=name)
			if alias_def is None:
				return ty
			alias_params, alias_target, _loc = alias_def
			if alias_params:
				return ty
			alias_key = (mod, name)
			if alias_key in seen:
				return ty
			resolved = resolve_opaque_type(alias_target, self.type_table, module_id=mod, type_params=None, allow_generic_base=True)
			return _dealias_zero_param_type(resolved, _seen=seen | {alias_key})

		def _coerce_args_for_params(params: list[TypeId], args: list[TypeId]) -> list[TypeId]:
			if len(params) != len(args):
				return list(args)
			coerced = list(args)
			for idx, (param_ty, arg_ty) in enumerate(zip(params, args)):
				if arg_ty is None:
					continue
				if arg_ty != param_ty and self.type_table.get(param_ty).kind is TypeKind.INTERFACE:
					if self.type_table.get(arg_ty).kind is not TypeKind.INTERFACE:
						coerced[idx] = param_ty
					continue
				ref_info = _ref_param_info(param_ty)
				if ref_info is None:
					continue
				ref_mut, _inner = ref_info
				arg_def = self.type_table.get(arg_ty)
				if arg_def.kind is TypeKind.REF:
					continue
				coerced[idx] = self.type_table.ensure_ref_mut(arg_ty) if ref_mut else self.type_table.ensure_ref(arg_ty)
			return coerced

		def _args_match_params(params: list[TypeId], args: list[TypeId]) -> bool:
			if len(params) != len(args):
				return False
			for param_ty, arg_ty in zip(params, args):
				if arg_ty is None:
					return False
				if arg_ty == self._unknown:
					param_def = self.type_table.get(param_ty)
					if param_def.kind is TypeKind.INTERFACE and param_def.name in ("Callback0", "Callback1", "Callback2", "CallbackThrow0", "CallbackThrow1", "CallbackThrow2"):
						continue
				param_cmp = _dealias_zero_param_type(param_ty)
				arg_cmp = _dealias_zero_param_type(arg_ty)
				if param_cmp == arg_cmp:
					continue
				ref_info = _ref_param_info(param_cmp)
				if ref_info is not None and arg_cmp == ref_info[1]:
					continue
				param_def = self.type_table.get(param_cmp)
				arg_def = self.type_table.get(arg_cmp)
				if param_def.kind is TypeKind.REF and arg_def.kind is TypeKind.REF and arg_def.param_types and arg_def.param_types[0] == param_cmp:
					continue
				# Implicit reborrow: &mut T matches where &T is expected.
				if (
					arg_def.kind is TypeKind.REF and param_def.kind is TypeKind.REF
					and arg_def.ref_mut is True and param_def.ref_mut is False
					and arg_def.param_types and param_def.param_types
					and arg_def.param_types[0] == param_def.param_types[0]
				):
					continue
				return False
			return True

		def _apply_autoborrow_args(
			args: list[H.HExpr],
			arg_types: list[TypeId],
			param_types: list[TypeId],
			*,
			span: Span,
			skip_first: bool = False,
		) -> tuple[list[TypeId], bool]:
			def _can_autoborrow_mut(place_expr: H.HExpr, place: Place) -> bool:
				has_deref = any(isinstance(p, DerefProj) for p in place.projections)
				if not has_deref:
					if place.base.local_id is None:
						return False
					base_ty = binding_types.get(place.base.local_id)
					if base_ty is not None:
						base_def = self.type_table.get(base_ty)
						if base_def.kind is TypeKind.REF and bool(base_def.ref_mut):
							return True
					return bool(binding_mutable.get(place.base.local_id, False))
				if not hasattr(H, "HPlaceExpr") or not isinstance(place_expr, getattr(H, "HPlaceExpr")):
					return False
				cur = type_expr(place_expr.base, used_as_value=False)
				if cur is None:
					return False
				for pr in place_expr.projections:
					if isinstance(pr, H.HPlaceDeref):
						ptr_def = self.type_table.get(cur)
						if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
							return False
						cur = ptr_def.param_types[0] if ptr_def.param_types else None
						if cur is None:
							return False
					elif isinstance(pr, H.HPlaceField):
						td = self.type_table.get(cur)
						if td.kind is not TypeKind.STRUCT:
							return False
						info = self.type_table.struct_field(cur, pr.name)
						if info is None:
							return False
						_, cur = info
					elif isinstance(pr, H.HPlaceIndex):
						td = self.type_table.get(cur)
						if td.kind is not TypeKind.ARRAY or not td.param_types:
							return False
						cur = td.param_types[0]
				return True

			def _try_borrow_coerce(
				idx: int,
				arg_ty: TypeId,
				param_ty: TypeId,
				arg_expr: H.HExpr,
				ref_mut: bool,
			) -> bool:
				method_name = "borrow_mut" if ref_mut else "borrow"
				call_expr = H.HMethodCall(
					receiver=arg_expr,
					method_name=method_name,
					args=[],
				)
				call_expr.callsite_id = _alloc_callsite_id()
				_assign_node_id(call_expr)
				start = len(diagnostics)
				coerced_ty = type_expr(call_expr, expected_type=param_ty)
				if coerced_ty is None or coerced_ty != param_ty:
					del diagnostics[start:]
					return False
				args[idx] = call_expr
				updated_types[idx] = coerced_ty
				return True

			if len(args) != len(param_types) or len(arg_types) != len(param_types):
				return list(arg_types), False
			updated_types = list(arg_types)
			had_error = False
			for idx, (param_ty, arg_ty, arg_expr) in enumerate(zip(param_types, arg_types, args)):
				if arg_ty is None:
					if self.type_table.get(param_ty).kind is TypeKind.INTERFACE and isinstance(arg_expr, H.HLambda):
						inst = self.type_table.get_interface_instance(param_ty)
						base_id = inst.base_id if inst is not None else param_ty
						base_def = self.type_table.interface_bases.get(base_id)
						if base_def is not None and base_def.name.startswith("Callback"):
							record_iface_coercion(arg_expr, param_ty)
							updated_types[idx] = param_ty
							continue
					ref_info = _ref_param_info(param_ty)
					if ref_info is None:
						continue
					ref_mut, inner = ref_info
					place_expr = place_expr_from_lvalue_expr(arg_expr)
					if place_expr is None:
						if ref_mut:
							diagnostics.append(
								_tc_diag(
									message="borrow requires an addressable place; bind to a local first",
									severity="error",
									phase="typecheck",
									span=getattr(arg_expr, "loc", span),
								)
							)
							had_error = True
							continue
						borrow_expr = H.HBorrow(subject=arg_expr, is_mut=False, allow_rvalue=True)
						_assign_node_id(borrow_expr)
						args[idx] = borrow_expr
						updated_types[idx] = type_expr(borrow_expr)
						continue
					_assign_place_expr_ids(place_expr)
					if ref_mut:
						place = place_from_expr(place_expr, base_lookup=_receiver_base_lookup)
						if place is None or not _can_autoborrow_mut(place_expr, place):
							diagnostics.append(
								_tc_diag(
									message="cannot auto-borrow as &mut; argument is not mutable",
									severity="error",
									phase="typecheck",
									span=getattr(arg_expr, "loc", span),
								)
							)
							had_error = True
							continue
					borrow_expr = H.HBorrow(subject=place_expr, is_mut=ref_mut)
					_assign_node_id(borrow_expr)
					args[idx] = borrow_expr
					updated_types[idx] = type_expr(borrow_expr)
					continue
				if arg_ty is not None and arg_ty != param_ty and self.type_table.get(param_ty).kind is TypeKind.INTERFACE:
					inst = self.type_table.get_interface_instance(param_ty)
					base_id = inst.base_id if inst is not None else param_ty
					base_def = self.type_table.interface_bases.get(base_id)
					if base_def is not None and base_def.name.startswith("Callback") and isinstance(arg_expr, H.HLambda):
						continue
					if self.type_table.get(arg_ty).kind is TypeKind.INTERFACE:
						if iface_assignable(arg_ty, param_ty):
							record_iface_coercion(arg_expr, param_ty)
							updated_types[idx] = param_ty
							continue
					else:
						record_iface_coercion(arg_expr, param_ty)
						updated_types[idx] = param_ty
						continue
				ref_info = _ref_param_info(param_ty)
				if ref_info is None:
					continue
				ref_mut, inner = ref_info
				if arg_ty == param_ty:
					continue
				arg_def = self.type_table.get(arg_ty)
				if arg_def.kind is TypeKind.REF and arg_def.param_types:
					arg_inner = arg_def.param_types[0]
					if arg_inner == param_ty:
						# Allow implicit single deref for nested refs in call arguments,
						# e.g. passing &&T where &T is required.
						deref_expr = H.HUnary(op=H.UnaryOp.DEREF, expr=arg_expr)
						_assign_node_id(deref_expr)
						coerced_ty = type_expr(deref_expr, expected_type=param_ty)
						if coerced_ty == param_ty:
							args[idx] = deref_expr
							updated_types[idx] = coerced_ty
							continue
				if not (skip_first and idx == 0):
					if _try_borrow_coerce(idx, arg_ty, param_ty, arg_expr, ref_mut):
						continue
				if arg_ty != inner:
					continue
				place_expr = place_expr_from_lvalue_expr(arg_expr)
				if place_expr is None:
					if ref_mut:
						diagnostics.append(
							_tc_diag(
								message="borrow requires an addressable place; bind to a local first",
								severity="error",
								phase="typecheck",
								span=getattr(arg_expr, "loc", span),
							)
						)
						had_error = True
						continue
					borrow_expr = H.HBorrow(subject=arg_expr, is_mut=False, allow_rvalue=True)
					_assign_node_id(borrow_expr)
					args[idx] = borrow_expr
					updated_types[idx] = type_expr(borrow_expr)
					continue
				_assign_place_expr_ids(place_expr)
				if ref_mut:
					place = place_from_expr(place_expr, base_lookup=_receiver_base_lookup)
					if place is None or not _can_autoborrow_mut(place_expr, place):
						diagnostics.append(
							_tc_diag(
								message="cannot auto-borrow as &mut; argument is not mutable",
								severity="error",
								phase="typecheck",
								span=getattr(arg_expr, "loc", span),
							)
						)
						had_error = True
						continue
				borrow_expr = H.HBorrow(subject=place_expr, is_mut=ref_mut)
				_assign_node_id(borrow_expr)
				args[idx] = borrow_expr
				updated_types[idx] = type_expr(borrow_expr)
			return updated_types, had_error
		# Ref origin tracking (MVP escape policy):
		#
		# When a binding has a reference type, record whether it is ultimately
		# derived from a single reference *parameter* binding. This lets us enforce
		# "return refs only derived from a ref param" without a full lifetime model.
		#
		# Value is the binding_id of the originating ref param, or None when the
		# reference points at local/temporary storage.
		ref_origin_param: Dict[int, Optional[int]] = {}
		explicit_capture_stack: list[dict[int, str]] = []
		def _explicit_capture_kind(binding_id: int | None) -> str | None:
			if binding_id is None:
				return None
			for scope in reversed(explicit_capture_stack):
				kind = scope.get(binding_id)
				if kind is not None:
					return kind
			return None
		diagnostics: List[Diagnostic] = []
		deferred_guard_diags: Dict[DeferredGuardKey, List[Diagnostic]] = {}
		guard_outcomes: Dict[GuardKey, ProofStatus] = {}
		call_resolutions: Dict[int, CallableDecl | MethodResolution] = {}
		call_info_by_callsite_id: Dict[int, CallInfo] = {}
		callsite_owner_node_id: Dict[int, int] = {}
		fnptr_consts_by_node_id: Dict[int, tuple[FunctionRefId, CallSig]] = {}
		instantiations_by_callsite_id: Dict[int, CallInstantiation] = {}
		instantiations_by_node_id: Dict[int, CallInstantiation] = {}
		trait_worlds = getattr(self.type_table, "trait_worlds", {}) or {}
		def _world_has_trait_data(world: TraitWorld) -> bool:
			return bool(
				world.traits
				or world.impls
				or world.requires_by_struct
				or world.requires_by_fn
			)
		has_trait_worlds = isinstance(trait_worlds, dict) and any(
			_world_has_trait_data(world) for world in trait_worlds.values()
		)
		linked = linked_world
		if linked is None and has_trait_worlds:
			linked = link_trait_worlds(trait_worlds)
		global_trait_world: TraitWorld | None = linked.global_world if linked is not None else None
		visible_trait_world: TraitWorld | None = None
		if linked is not None and visible_modules is not None:
			module_name_by_id: dict[ModuleId, str] = {}
			if callable_registry is not None and signatures_by_id is not None:
				for fn_key, sig in signatures_by_id.items():
					decl = callable_registry.get_by_fn_id(fn_key)
					mod_name = getattr(sig, "module", None)
					if decl is None or mod_name is None:
						continue
					prev = module_name_by_id.get(decl.module_id)
					if prev is None:
						module_name_by_id[decl.module_id] = mod_name
					elif prev != mod_name:
						# Keep the first mapping; conflicting module ids are a bug upstream.
						module_name_by_id[decl.module_id] = prev
			visible_names: list[str] = []
			missing_modules: list[ModuleId] = []
			for mid in visible_modules:
				chain = visibility_provenance.get(mid)
				if chain:
					visible_names.append(chain[-1])
				elif current_module_name is not None and mid == current_module:
					visible_names.append(current_module_name)
				elif mid in module_name_by_id:
					visible_names.append(module_name_by_id[mid])
				else:
					missing_modules.append(mid)
			if missing_modules:
				if visibility_provenance:
					diagnostics.append(
						_tc_diag(
							message=(
								"internal: missing visibility provenance for module ids "
								+ ", ".join(str(mid) for mid in missing_modules)
							),
							severity="error",
							span=Span.from_loc(getattr(body, "loc", None)),
						)
					)
			if not visible_names and current_module_name is not None:
				visible_names.append(current_module_name)
			visible_trait_world = linked.visible_world(visible_names)
		default_package = getattr(self.type_table, "package_id", None)
		module_packages = getattr(self.type_table, "module_packages", None)
		require_env_local = require_env
		if require_env_local is None and linked_world is not None:
			require_env_local = build_require_env(linked_world, default_package=default_package, module_packages=module_packages or {})
		if require_env_local is None and has_trait_worlds:
			require_env_local = RequireEnv(
				requires_by_fn={},
				requires_by_struct={},
				default_package=default_package,
				module_packages=module_packages or {},
			)
		type_param_map: dict[str, TypeParamId] = {}
		type_param_names: dict[TypeParamId, str] = {}
		fn_require_assumed: set[tuple[object, TraitKey]] = set()
		if preseed_type_params:
			for _name, _tid in preseed_type_params.items():
				type_param_map[_name] = _tid

		def _require_for_fn(fid: FunctionId) -> parser_ast.TraitExpr | None:
			if require_env_local is not None:
				return require_env_local.requires_by_fn.get(fid)
			return None

		def _require_for_struct(key: TypeKey) -> parser_ast.TraitExpr | None:
			if require_env_local is not None:
				return require_env_local.requires_by_struct.get(key)
			return None

		def _collect_trait_is(expr: parser_ast.TraitExpr, out: list[parser_ast.TraitIs]) -> None:
			if isinstance(expr, parser_ast.TraitIs):
				out.append(expr)
				return
			if isinstance(expr, (parser_ast.TraitAnd, parser_ast.TraitOr)):
				_collect_trait_is(expr.left, out)
				_collect_trait_is(expr.right, out)
				return
			if isinstance(expr, parser_ast.TraitNot):
				_collect_trait_is(expr.expr, out)

		def _extract_conjunctive_facts(expr: parser_ast.TraitExpr) -> list[parser_ast.TraitIs]:
			if isinstance(expr, parser_ast.TraitIs):
				return [expr]
			if isinstance(expr, parser_ast.TraitAnd):
				return _extract_conjunctive_facts(expr.left) + _extract_conjunctive_facts(expr.right)
			if isinstance(expr, (parser_ast.TraitOr, parser_ast.TraitNot)):
				return []
			return []

		def _subject_name(subject: object) -> str | None:
			if isinstance(subject, parser_ast.SelfRef):
				return "Self"
			if isinstance(subject, parser_ast.TypeNameRef):
				return subject.name
			if isinstance(subject, str):
				return subject
			return None

		def _subject_lookup_key(subject: object) -> object:
			name = _subject_name(subject)
			return name if name is not None else subject

		def _is_self_subject(subject: object) -> bool:
			return isinstance(subject, parser_ast.SelfRef) or subject == "Self"

		def _resolve_trait_subjects_for_type_params(
			expr: parser_ast.TraitExpr,
			map_by_name: dict[str, TypeParamId],
		) -> parser_ast.TraitExpr:
			if isinstance(expr, parser_ast.TraitIs):
				subj_name = _subject_name(expr.subject)
				if subj_name is not None and subj_name in map_by_name:
					return parser_ast.TraitIs(
						loc=expr.loc,
						subject=map_by_name[subj_name],
						trait=expr.trait,
					)
				return expr
			if isinstance(expr, parser_ast.TraitAnd):
				return parser_ast.TraitAnd(
					loc=expr.loc,
					left=_resolve_trait_subjects_for_type_params(expr.left, map_by_name),
					right=_resolve_trait_subjects_for_type_params(expr.right, map_by_name),
				)
			if isinstance(expr, parser_ast.TraitOr):
				return parser_ast.TraitOr(
					loc=expr.loc,
					left=_resolve_trait_subjects_for_type_params(expr.left, map_by_name),
					right=_resolve_trait_subjects_for_type_params(expr.right, map_by_name),
				)
			if isinstance(expr, parser_ast.TraitNot):
				return parser_ast.TraitNot(
					loc=expr.loc,
					expr=_resolve_trait_subjects_for_type_params(expr.expr, map_by_name),
				)
			return expr

		def _normalize_type_key(key: object) -> object:
			if isinstance(key, TypeKey):
				return normalize_type_key(
					key,
					module_name=current_module_name,
					default_package=getattr(self.type_table, "package_id", None),
					module_packages=getattr(self.type_table, "module_packages", None),
				)
			return key

		def _type_key_label(key: object) -> str:
			pkg = getattr(key, "package_id", None)
			module = getattr(key, "module", None)
			name = getattr(key, "name", "")
			base = f"{module}.{name}" if module else name
			if pkg:
				base = f"{pkg}::{base}"
			args = getattr(key, "args", None) or ()
			if not args:
				return base
			inner = ", ".join(_type_key_label(a) for a in args)
			return f"{base}<{inner}>"

		def _trait_label(trait_key: TraitKey) -> str:
			base = f"{trait_key.module}.{trait_key.name}" if trait_key.module else trait_key.name
			if trait_key.package_id and not base.startswith(f"{trait_key.package_id}."):
				return f"{trait_key.package_id}::{base}"
			return base

		def _trait_expr_label(expr: parser_ast.TraitExpr) -> str:
			if isinstance(expr, parser_ast.TraitIs):
				subj = expr.subject
				subj_name = _subject_name(subj)
				if subj_name is None:
					if isinstance(subj, TypeParamId):
						subj_name = type_param_names.get(subj, "T")
					elif isinstance(subj, TypeKey):
						subj_name = _type_key_label(subj)
					else:
						subj_name = str(subj)
				trait_key = trait_key_from_expr(
					expr.trait,
					default_module=current_module_name,
					default_package=default_package,
					module_packages=module_packages,
				)
				return f"{subj_name} is {_trait_label(trait_key)}"
			if isinstance(expr, parser_ast.TraitAnd):
				return f"({_trait_expr_label(expr.left)} and {_trait_expr_label(expr.right)})"
			if isinstance(expr, parser_ast.TraitOr):
				return f"({_trait_expr_label(expr.left)} or {_trait_expr_label(expr.right)})"
			if isinstance(expr, parser_ast.TraitNot):
				return f"not ({_trait_expr_label(expr.expr)})"
			return "<trait expr>"

		def _type_has_typevar(ty_id: TypeId) -> bool:
			seen: set[TypeId] = set()
			stack = [ty_id]
			while stack:
				cur = stack.pop()
				if cur in seen:
					continue
				seen.add(cur)
				td = self.type_table.get(cur)
				if td.kind is TypeKind.TYPEVAR:
					return True
				inst = None
				if td.kind is TypeKind.STRUCT:
					inst = self.type_table.get_struct_instance(cur)
				elif td.kind is TypeKind.VARIANT:
					inst = self.type_table.get_variant_instance(cur)
				elif td.kind is TypeKind.INTERFACE:
					inst = self.type_table.get_interface_instance(cur)
				if inst is not None:
					stack.extend(list(inst.type_args))
				for child in getattr(td, "param_types", []) or []:
					stack.append(child)
			return False

		def _is_zero_sized_type(ty_id: TypeId) -> bool:
			seen: set[TypeId] = set()
			stack = [ty_id]
			while stack:
				cur = stack.pop()
				if cur in seen:
					continue
				seen.add(cur)
				td = self.type_table.get(cur)
				if td.kind in (TypeKind.TYPEVAR, TypeKind.SCALAR, TypeKind.REF, TypeKind.RAW_PTR, TypeKind.ARRAY, TypeKind.ERROR, TypeKind.VARIANT, TypeKind.FNRESULT):
					return False
				if td.kind is TypeKind.STRUCT:
					if not td.param_types:
						continue
					stack.extend(list(td.param_types))
					continue
				return False
			return True

		def _reject_zst_array(elem: TypeId, *, span: Span) -> bool:
			if _is_zero_sized_type(elem):
				diagnostics.append(
					_tc_diag(
						message="arrays of zero-sized element types are not supported in v1",
						code="E_ARRAY_ZST_UNSUPPORTED",
						severity="error",
						span=span,
					)
				)
				return True
			return False

		def _is_map_like_target_type(ty_id: TypeId | None) -> bool:
			if ty_id is None:
				return False
			td = self.type_table.get(ty_id)
			# v1 map literal typing bridge:
			# accept explicit targets named like map containers until a dedicated
			# map-literal desugaring/typeclass path lands.
			if td.name in {"Map", "HashMap", "HashMapCore", "TreeMap"}:
				return True
			return False

		# TypeIds temporarily exempt from Copy enforcement.  Used when an array
		# element is being accessed for field projection (entries[i].name) — the
		# element itself doesn't need to be Copy, only the projected field does.
		_suppress_copy_type_ids: set = set()

		def _require_copy_value(
			ty_id: TypeId | None,
			*,
			span: Span,
			name: str | None = None,
			used_as_value: bool = True,
		) -> None:
			if not used_as_value:
				return
			if ty_id is not None and ty_id in _suppress_copy_type_ids:
				return
			if ty_id is None:
				return
			td = self.type_table.get(ty_id)
			if td.kind is TypeKind.TYPEVAR:
				# Defer Copy requirements for unresolved type parameters to instantiation.
				return
			copy_status = self.type_table.copy_status(ty_id)
			if copy_status is True:
				return
			# G4: route binder name through user_facing_binding_name so
			# the internal `__match_binder_<n>_<src>` form never leaks
			# into copy diagnostics for match-arm binders.
			user_name = user_facing_binding_name(name) if name else name
			if copy_status is None:
				pretty = self._pretty_type_name(ty_id, current_module=current_module_name)
				reason = self.type_table.copy_unknown_reason(ty_id)
				if user_name:
					msg = f"cannot copy '{user_name}': type '{pretty}' Copy is unknown ({reason})"
				else:
					msg = f"cannot copy value of type '{pretty}': Copy is unknown ({reason})"
				diagnostics.append(
					_tc_diag(
						message=msg,
						code="E-COPY-UNKNOWN",
						severity="error",
						span=span,
					)
				)
				return
			pretty = self._pretty_type_name(ty_id, current_module=current_module_name)
			if user_name:
				msg = f"cannot copy '{user_name}': type '{pretty}' is not Copy (use move {user_name})"
			else:
				msg = f"cannot copy value of type '{pretty}' (use move <expr>)"
			diagnostics.append(
				_tc_diag(
					message=msg,
					severity="error",
					span=span,
				)
			)

		def _require_int_index_type(idx_ty: TypeId | None, *, span: Span) -> bool:
			if idx_ty is None:
				return True
			td_idx = self.type_table.get(idx_ty)
			if td_idx.kind is not TypeKind.TYPEVAR and idx_ty != self._int:
				diagnostics.append(
					_tc_diag(
						message="array index must be an Int",
						severity="error",
						span=span,
					)
				)
				return False
			return True

		def _array_element_type(container_ty: TypeId | None, *, span: Span) -> TypeId | None:
			if container_ty is None:
				return None
			td = self.type_table.get(container_ty)
			if td.kind is TypeKind.REF and td.param_types:
				td = self.type_table.get(td.param_types[0])
			if td.kind is TypeKind.ARRAY and td.param_types:
				return td.param_types[0]
			diagnostics.append(
				_tc_diag(
					message="indexing requires an Array value",
					severity="error",
					span=span,
				)
			)
			return None

		def _deref_inner_type(ptr_ty: TypeId | None, *, span: Span) -> TypeId | None:
			if ptr_ty is None:
				return None
			td = self.type_table.get(ptr_ty)
			if td.kind is not TypeKind.REF or not td.param_types:
				diagnostics.append(
					_tc_diag(
						message="deref requires a reference value",
						severity="error",
						span=span,
					)
				)
				return None
			return td.param_types[0]

		self_type_id: TypeId | None = None

		def _guard_assumptions(
			expr: parser_ast.TraitExpr,
			*,
			subst: dict[object, object],
		) -> set[tuple[object, TraitKey]]:
			out: set[tuple[object, TraitKey]] = set()
			for atom in _extract_conjunctive_facts(expr):
				subj = atom.subject
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=current_module_name,
					default_package=default_package,
					module_packages=module_packages,
				)
				if _is_self_subject(subj):
					if self_type_id is None:
						continue
					subj_type_id = self_type_id
					subj_def = self.type_table.get(subj_type_id)
					if subj_def.kind is TypeKind.REF and subj_def.param_types:
						subj_type_id = subj_def.param_types[0]
					self_def = self.type_table.get(subj_type_id)
					if self_def.kind is TypeKind.TYPEVAR and self_def.type_param_id is not None:
						out.add((self_def.type_param_id, trait_key))
						tp_name = type_param_names.get(self_def.type_param_id)
						ty_id = self.type_table.ensure_typevar(self_def.type_param_id, name=tp_name)
						key = _normalize_type_key(type_key_from_typeid(self.type_table, ty_id))
						out.add((key, trait_key))
					else:
						key = _normalize_type_key(type_key_from_typeid(self.type_table, subj_type_id))
						out.add((key, trait_key))
					continue
				if isinstance(subj, TypeParamId):
					out.add((subj, trait_key))
					tp_name = type_param_names.get(subj)
					ty_id = self.type_table.ensure_typevar(subj, name=tp_name)
					key = _normalize_type_key(type_key_from_typeid(self.type_table, ty_id))
					out.add((key, trait_key))
					continue
				lookup_key = _subject_lookup_key(subj)
				key = subst.get(lookup_key)
				if key is not None:
					out.add((key, trait_key))
				elif isinstance(subj, TypeKey):
					out.add((_normalize_type_key(subj), trait_key))
			return out

		guard_trait_scopes: list[list[TraitKey]] = []

		def _with_guard_assumptions(assumed: set[tuple[object, TraitKey]], block: H.HBlock) -> None:
			if not assumed:
				type_block(block)
				return
			added = {a for a in assumed if a not in fn_require_assumed}
			if added:
				fn_require_assumed.update(added)
			guard_traits = sorted({trait for _subj, trait in assumed}, key=_trait_label)
			if guard_traits:
				guard_trait_scopes.append(guard_traits)
			try:
				type_block(block)
			finally:
				for item in added:
					fn_require_assumed.discard(item)
				if guard_traits:
					guard_trait_scopes.pop()

		def _guard_key(expr: H.HTraitExpr) -> GuardKey:
			return int(getattr(expr, "node_id", 0) or 0)

		def _type_block_defer_diags(
			block: H.HBlock,
			*,
			guard_key: GuardKey,
			branch: str,
			assumed: set[tuple[object, TraitKey]] | None = None,
		) -> None:
			start = len(diagnostics)
			if assumed:
				_with_guard_assumptions(assumed, block)
			else:
				type_block(block)
			if len(diagnostics) > start:
				key = (guard_key, branch)
				deferred_guard_diags.setdefault(key, []).extend(diagnostics[start:])
				del diagnostics[start:]

		sig: FnSignature | None = None
		if signatures_by_id is not None:
			sig = signatures_by_id.get(fn_id)
			if sig is not None:
				for p in (list(getattr(sig, "impl_type_params", []) or []) + list(getattr(sig, "type_params", []) or [])):
					if p.name not in type_param_map:
						type_param_map[p.name] = p.id
				type_param_names = {p.id: p.name for p in (list(getattr(sig, "impl_type_params", []) or []) + list(getattr(sig, "type_params", []) or []))}
		unsafe_allowed_module = self._allow_unsafe or self._is_toolchain_trusted_module(current_module_name) or self._is_pkg_unsafe_allowed(current_module_name)
		unsafe_context = bool(getattr(sig, "is_unsafe", False)) if sig is not None else False
		allow_unsafe_without_block_local = self._allow_unsafe_without_block or self._is_toolchain_trusted_module(current_module_name)
		if unsafe_context and not unsafe_allowed_module:
			diagnostics.append(_tc_diag(message="unsafe fn requires --allow-unsafe", severity="error", span=Span.from_loc(getattr(sig, "loc", None))))

		def _traits_in_scope() -> list[TraitKey]:
			extra: list[TraitKey] = []
			for scope in guard_trait_scopes:
				extra.extend(scope)
			core_borrow = trait_key_from_expr(
				parser_ast.TypeExpr(name="Borrow", module_id="std.core"),
				default_module=current_module_name,
				default_package=default_package,
				module_packages=module_packages,
			)
			core_borrow_mut = trait_key_from_expr(
				parser_ast.TypeExpr(name="BorrowMut", module_id="std.core"),
				default_module=current_module_name,
				default_package=default_package,
				module_packages=module_packages,
			)
			if trait_impl_index is not None and fn_id is not None and hasattr(trait_impl_index, "trait_key_for_fn_id"):
				impl_trait = trait_impl_index.trait_key_for_fn_id(fn_id)
				if impl_trait is not None and impl_trait not in extra:
					extra.append(impl_trait)
			if trait_scope_by_module:
				traits = list(trait_scope_by_module.get(current_module_name, []))
				for trait in extra:
					if trait not in traits:
						traits.append(trait)
				if core_borrow not in traits:
					traits.append(core_borrow)
				if core_borrow_mut not in traits:
					traits.append(core_borrow_mut)
				return traits
			traits = list(extra)
			if core_borrow not in traits:
				traits.append(core_borrow)
			if core_borrow_mut not in traits:
				traits.append(core_borrow_mut)
			return traits

		def _resolve_self_type_id() -> TypeId | None:
			if param_types and "self" in param_types:
				return param_types.get("self")
			if sig is not None and sig.param_names and sig.param_type_ids:
				for name, ty_id in zip(sig.param_names, sig.param_type_ids):
					if name == "self":
						return ty_id
			if sig is not None and sig.is_method and sig.param_type_ids:
				return sig.param_type_ids[0]
			return None

		self_type_id = _resolve_self_type_id()
		req = _require_for_fn(fn_id)
		if req is not None:
			for atom in _extract_conjunctive_facts(req):
				subj = atom.subject
				subj_name = _subject_name(subj)
				if subj_name is not None and subj_name in type_param_map:
					subj = type_param_map[subj_name]
				if isinstance(subj, TypeParamId):
					trait_key = trait_key_from_expr(
						atom.trait,
						default_module=current_module_name,
						default_package=default_package,
						module_packages=module_packages,
					)
					fn_require_assumed.add((subj, trait_key))
					tp_name = type_param_names.get(subj)
					ty_id = self.type_table.ensure_typevar(subj, name=tp_name)
					key = _normalize_type_key(type_key_from_typeid(self.type_table, ty_id))
					fn_require_assumed.add((key, trait_key))
		if trait_impl_index is not None and fn_id is not None and hasattr(trait_impl_index, "require_for_fn_id"):
			impl_req = trait_impl_index.require_for_fn_id(fn_id)
			if impl_req is not None:
				for atom in _extract_conjunctive_facts(impl_req):
					subj = atom.subject
					subj_name = _subject_name(subj)
					if subj_name is not None and subj_name in type_param_map:
						subj = type_param_map[subj_name]
					if isinstance(subj, TypeParamId):
						trait_key = trait_key_from_expr(
							atom.trait,
							default_module=current_module_name,
							default_package=default_package,
							module_packages=module_packages,
						)
						fn_require_assumed.add((subj, trait_key))
						tp_name = type_param_names.get(subj)
						ty_id = self.type_table.ensure_typevar(subj, name=tp_name)
						key = _normalize_type_key(type_key_from_typeid(self.type_table, ty_id))
						fn_require_assumed.add((key, trait_key))

		def _function_ref_candidates(
			name: str,
			module_name: str | None,
		) -> list[tuple[FunctionId, FnSignature]]:
			if callable_registry is not None:
				candidates = callable_registry.get_free_candidates(
					name=name,
					visible_modules=_visible_modules_for_free_call(module_name),
					include_private_in=current_module if module_name is None else None,
				)
				sigs: list[tuple[FunctionId, FnSignature]] = []
				for cand in candidates:
					if cand.fn_id is None or signatures_by_id is None:
						continue
					sig = signatures_by_id.get(cand.fn_id)
					if sig is not None and not getattr(sig, "is_method", False):
						sigs.append((cand.fn_id, sig))
				return sigs
			return []

		def _expected_function_shape(expected_type: TypeId | None) -> tuple[list[TypeId], TypeId, bool] | None:
			if expected_type is None:
				return None
			td = self.type_table.get(expected_type)
			if td.kind is TypeKind.INTERFACE:
				inst = self.type_table.get_interface_instance(expected_type)
				base_id = inst.base_id if inst is not None else expected_type
				base_def = self.type_table.interface_bases.get(base_id)
				if base_def is None:
					return None
				args = list(inst.type_args) if inst is not None else []
				# Table-driven over `Callback{N}` / `CallbackThrow{N}` —
				# the iface's type-args layout is `<P1, ..., PN, R>`,
				# generic over arity.  Adding a new arity is a one-line
				# change in `call_resolver._CALLBACK_ROWS`.
				from lang.driftc.checker.call_resolver import _CALLBACK_KIND_BY_IFACE
				kind = _CALLBACK_KIND_BY_IFACE.get(base_def.name)
				if kind is not None:
					arity, is_throw = kind
					if len(args) >= arity + 1:
						return list(args[:arity]), args[arity], is_throw
			if td.kind is not TypeKind.FUNCTION or not td.param_types:
				return None
			params = list(td.param_types[:-1])
			ret = td.param_types[-1]
			can_throw = td.can_throw()
			return params, ret, can_throw

		def _fn_trait_expected(trait_name: str) -> tuple[int, bool] | None:
			# Returns `(arity_plus_one, is_throw)` — the trait `Fn{N}` /
			# `FnThrow{N}` corresponds to a function with N params + 1
			# return type (so `arity_plus_one == N + 1`).  Table-driven
			# from the central `_CALLBACK_ROWS` enumeration in
			# `call_resolver.py` so a new arity adds a row there and
			# nothing else.
			from lang.driftc.checker.call_resolver import _CALLBACK_ROWS
			for r in _CALLBACK_ROWS:
				if trait_name == r["fn_trait"]:
					return (r["arity"] + 1, False)
				if trait_name == r["fn_throw_trait"]:
					return (r["arity"] + 1, True)
			return None

		# Option B: boundary ABI functions collapsed. No cross-package
		# wrapper upgrade or forced can_throw.
		def _force_boundary_can_throw(sig, fn_id):
			return False

		def _method_boundary_visible(sig, fn_id):
			return False

		def _apply_method_boundary(
			expr: H.HMethodCall,
			*,
			target_fn_id: FunctionId,
			sig_for_throw: FnSignature | None,
			call_can_throw: bool,
		) -> tuple[FunctionId, bool] | None:
			# Option B: no boundary wrapper upgrade. Return target as-is.
			return target_fn_id, call_can_throw

		def _call_sig_for_fn_ref(sig: FnSignature) -> tuple[list[TypeId], TypeId, bool] | None:
			if getattr(sig, "type_params", None):
				return None
			if sig.param_type_ids is None or sig.return_type_id is None:
				return None
			if sig.declared_can_throw is None:
				diagnostics.append(
					_tc_diag(
						message="internal: signature missing declared_can_throw (checker bug)",
						severity="error",
						span=Span.from_loc(getattr(sig, "loc", None)),
					)
				)
				can_throw = True
			else:
				can_throw = bool(sig.declared_can_throw)
			if getattr(sig, "is_exported_entrypoint", False) or getattr(sig, "is_extern", False):
				can_throw = True
			return list(sig.param_type_ids), sig.return_type_id, can_throw

		def _ensure_ok_wrap_thunk(
			target_fn_id: FunctionId,
			params: list[TypeId],
			ret: TypeId,
		) -> FunctionRefId:
			key = (ThunkKind.OK_WRAP, target_fn_id, tuple(params), ret)
			spec = self._thunk_specs.get(key)
			if spec is not None:
				return FunctionRefId(fn_id=spec.thunk_fn_id, kind=FunctionRefKind.THUNK_OK_WRAP)
			thunk_fn_id = FunctionId(
				module="lang.__internal",
				name=f"__thunk_ok_wrap::{function_symbol(target_fn_id)}",
				ordinal=0,
			)
			spec = ThunkSpec(
				thunk_fn_id=thunk_fn_id,
				target_fn_id=target_fn_id,
				param_types=tuple(params),
				return_type=ret,
				kind=ThunkKind.OK_WRAP,
			)
			self._thunk_specs[key] = spec
			return FunctionRefId(fn_id=thunk_fn_id, kind=FunctionRefKind.THUNK_OK_WRAP)

		def _ensure_boundary_thunk(
			target_fn_id: FunctionId,
			params: list[TypeId],
			ret: TypeId,
		) -> FunctionRefId:
			key = (ThunkKind.BOUNDARY, target_fn_id, tuple(params), ret)
			spec = self._thunk_specs.get(key)
			if spec is not None:
				return FunctionRefId(fn_id=spec.thunk_fn_id, kind=FunctionRefKind.THUNK_BOUNDARY)
			thunk_fn_id = FunctionId(
				module="lang.__internal",
				name=f"__thunk_boundary::{function_symbol(target_fn_id)}",
				ordinal=0,
			)
			spec = ThunkSpec(
				thunk_fn_id=thunk_fn_id,
				target_fn_id=target_fn_id,
				param_types=tuple(params),
				return_type=ret,
				kind=ThunkKind.BOUNDARY,
			)
			self._thunk_specs[key] = spec
			return FunctionRefId(fn_id=thunk_fn_id, kind=FunctionRefKind.THUNK_BOUNDARY)

		@dataclass
		class _FnRefResolution:
			fn_ref: FunctionRefId | None
			call_sig: CallSig | None
			fn_type: TypeId | None

		def _resolve_function_reference_value(
			*,
			name: str,
			module_name: str | None,
			expected_type: TypeId | None,
			span: Span,
			diag_mode: str,
			allow_thunk: bool,
		) -> _FnRefResolution | None:
			fn_candidates = _function_ref_candidates(name, module_name)
			if not fn_candidates:
				return None
			expected_fn = _expected_function_shape(expected_type)
			candidate_labels: list[str] = []
			matches: list[tuple[FunctionId, FnSignature, tuple[list[TypeId], TypeId, bool]]] = []
			thunk_candidates: list[tuple[FunctionId, FnSignature, tuple[list[TypeId], TypeId, bool]]] = []
			throw_mismatch_only = False

			def _build_resolution(
				fn_id: FunctionId,
				sig: FnSignature,
				call_sig_tuple: tuple[list[TypeId], TypeId, bool],
			) -> _FnRefResolution:
				params, ret, can_throw = call_sig_tuple
				call_sig = CallSig(param_types=tuple(params), user_ret_type=ret, can_throw=bool(can_throw))
				is_exported = bool(getattr(sig, "is_exported_entrypoint", False))
				is_extern = bool(getattr(sig, "is_extern", False))
				if is_exported or is_extern:
					# Nothrow exported targets use OK_WRAP: call __impl directly,
					# wrap bare return into FnResult.  Throwing targets use BOUNDARY:
					# passthrough of the FnResult the target already returns.
					if bool(getattr(sig, "declared_can_throw", False)):
						fn_ref = _ensure_boundary_thunk(fn_id, params, ret)
					else:
						fn_ref = _ensure_ok_wrap_thunk(fn_id, params, ret)
				else:
					fn_ref = FunctionRefId(fn_id=fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
				fn_ty = self.type_table.ensure_function(params, ret, can_throw=bool(can_throw))
				return _FnRefResolution(fn_ref=fn_ref, call_sig=call_sig, fn_type=fn_ty)

			for fn_id, sig in fn_candidates:
				cs = _call_sig_for_fn_ref(sig)
				if cs is None:
					continue
				params, ret, can_throw = cs
				cand_ty = self.type_table.ensure_function(params, ret, can_throw=bool(can_throw))
				candidate_labels.append(self._pretty_type_name(cand_ty, current_module=current_module_name))
				if expected_fn is None:
					continue
				exp_params, exp_ret, exp_throw = expected_fn
				if params == exp_params and ret == exp_ret:
					if can_throw != exp_throw:
						throw_mismatch_only = True
						if exp_throw and not can_throw:
							thunk_candidates.append((fn_id, sig, cs))
					else:
						matches.append((fn_id, sig, cs))

			if expected_fn is None:
				if len(fn_candidates) > 1:
					diagnostics.append(
						_tc_diag(
							message=f"ambiguous function reference '{name}'; add a type annotation",
							severity="error",
							span=span,
						)
					)
					return _FnRefResolution(fn_ref=None, call_sig=None, fn_type=None)
				chosen_fn_id, chosen_sig = fn_candidates[0]
				call_sig_tuple = _call_sig_for_fn_ref(chosen_sig)
				if call_sig_tuple is None:
					diag = (
						f"function reference '{name}' requires explicit type arguments"
						if getattr(chosen_sig, "type_params", None)
						else "function reference lacks resolved parameter types (compiler bug)"
					)
					diagnostics.append(
						_tc_diag(
							message=diag,
							severity="error",
							span=span,
						)
					)
					return _FnRefResolution(fn_ref=None, call_sig=None, fn_type=None)
				return _build_resolution(chosen_fn_id, chosen_sig, call_sig_tuple)

			if not matches:
				if allow_thunk and expected_fn is not None and len(thunk_candidates) == 1:
					chosen_fn_id, chosen_sig, cs = thunk_candidates[0]
					params, ret, _can_throw = cs
					thunk_ref = _ensure_ok_wrap_thunk(chosen_fn_id, params, ret)
					call_sig = CallSig(param_types=tuple(params), user_ret_type=ret, can_throw=True)
					fn_ty = self.type_table.ensure_function(params, ret, can_throw=True)
					return _FnRefResolution(fn_ref=thunk_ref, call_sig=call_sig, fn_type=fn_ty)
				if diag_mode == "cast":
					pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
					exp_params, exp_ret, exp_throw = expected_fn
					params_s = ", ".join(self._pretty_type_name(p, current_module=current_module_name) for p in exp_params)
					if not params_s:
						params_s = "()"
					ret_s = self._pretty_type_name(exp_ret, current_module=current_module_name)
					throw_label = "nothrow" if not exp_throw else "can-throw"
					notes: list[str] = []
					if candidate_labels:
						notes.append(f"candidates: {'; '.join(candidate_labels)}")
					if throw_mismatch_only:
						notes.append("note: throw-mode differs; thunking (nothrow -> can-throw) is not supported yet")
					diagnostics.append(
						_tc_diag(
							message=(
								f"cannot cast function '{name}' to {pretty}: no overload matches "
								f"(expected params: {params_s}, returns: {ret_s}, {throw_label})"
							),
							severity="error",
							span=span,
							notes=notes,
						)
					)
				else:
					pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=f"no overload of '{name}' matches function type {pretty}",
							severity="error",
							span=span,
						)
					)
				return _FnRefResolution(fn_ref=None, call_sig=None, fn_type=None)

			if len(matches) > 1:
				if diag_mode == "cast":
					pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
					notes = [f"candidates: {'; '.join(candidate_labels)}"] if candidate_labels else []
					diagnostics.append(
						_tc_diag(
							message=f"cannot cast function '{name}' to {pretty}: ambiguous overload resolution",
							severity="error",
							span=span,
							notes=notes,
						)
					)
				else:
					diagnostics.append(
						_tc_diag(
							message=f"ambiguous function reference '{name}'; add a type annotation to disambiguate",
							severity="error",
							span=span,
						)
					)
				return _FnRefResolution(fn_ref=None, call_sig=None, fn_type=None)

			chosen_fn_id, chosen_sig, call_sig_tuple = matches[0]
			return _build_resolution(chosen_fn_id, chosen_sig, call_sig_tuple)

		def _lambda_can_throw(lam: H.HLambda, call_info: Mapping[int, CallInfo] | None) -> bool:
			if call_info is None:
				call_info = {}
			def _treat_can_throw(info: CallInfo) -> bool:
				if not info.sig.can_throw:
					return False
				if info.target.kind is CallTargetKind.DIRECT and signatures_by_id is not None and info.target.symbol is not None:
					sig = signatures_by_id.get(info.target.symbol)
					if sig is not None:
						if not bool(getattr(sig, "declared_can_throw", False)):
							return False
						wrapped = getattr(sig, "wraps_target_fn_id", None)
						if wrapped is not None:
							inner = signatures_by_id.get(wrapped)
							if inner is not None and not bool(getattr(inner, "declared_can_throw", False)):
								return False
				return True
			def expr_can_throw(expr: H.HExpr) -> bool:
				if isinstance(expr, H.HCall):
					info = call_info.get(getattr(expr, "callsite_id", None))
					if info is None:
						return True
					if _treat_can_throw(info):
						return True
					if isinstance(expr.fn, H.HLambda):
						return _lambda_can_throw(expr.fn, call_info)
					return any(expr_can_throw(a) for a in expr.args)
				if isinstance(expr, H.HMethodCall):
					info = call_info.get(getattr(expr, "callsite_id", None))
					if info is None:
						return True
					if _treat_can_throw(info):
						return True
					if expr_can_throw(expr.receiver):
						return True
					return any(expr_can_throw(a) for a in expr.args)
				if isinstance(expr, H.HInvoke):
					info = call_info.get(getattr(expr, "callsite_id", None))
					if info is None:
						return True
					if _treat_can_throw(info):
						return True
					if isinstance(expr.callee, H.HLambda):
						return _lambda_can_throw(expr.callee, call_info)
					if expr_can_throw(expr.callee):
						return True
					return any(expr_can_throw(a) for a in expr.args)
				if isinstance(expr, H.HTryExpr):
					catch_all = any(arm.event_fqn is None for arm in expr.arms)
					if not catch_all and expr_can_throw(expr.attempt):
						return True
					for arm in expr.arms:
						if block_can_throw(arm.block):
							return True
						if arm.result is not None and expr_can_throw(arm.result):
							return True
					return False
				if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
					return block_can_throw(expr.body) or expr_can_throw(expr.result)
				if isinstance(expr, H.HLambda):
					return _lambda_can_throw(expr, call_info)
				if isinstance(expr, H.HResultOk):
					return expr_can_throw(expr.value)
				if isinstance(expr, H.HTernary):
					return (
						expr_can_throw(expr.cond)
						or expr_can_throw(expr.then_expr)
						or expr_can_throw(expr.else_expr)
					)
				if isinstance(expr, H.HUnary):
					return expr_can_throw(expr.expr)
				if isinstance(expr, H.HBinary):
					return expr_can_throw(expr.left) or expr_can_throw(expr.right)
				if isinstance(expr, H.HField):
					return expr_can_throw(expr.subject)
				if isinstance(expr, H.HIndex):
					return expr_can_throw(expr.subject) or expr_can_throw(expr.index)
				if isinstance(expr, H.HPlaceExpr):
					for proj in expr.projections:
						if isinstance(proj, H.HPlaceIndex) and expr_can_throw(proj.index):
							return True
					return False
				if isinstance(expr, H.HArrayLiteral):
					return any(expr_can_throw(el) for el in expr.elements)
				if isinstance(expr, H.HDVInit):
					return any(expr_can_throw(a) for a in expr.args)
				return False

			def stmt_can_throw(stmt: H.HStmt) -> bool:
				if isinstance(stmt, (H.HThrow, H.HRethrow)):
					return True
				if isinstance(stmt, H.HLocalConst):
					return False  # literal initializer
				if isinstance(stmt, H.HExprStmt):
					return expr_can_throw(stmt.expr)
				if isinstance(stmt, H.HLet):
					return expr_can_throw(stmt.value)
				if isinstance(stmt, H.HAssign):
					return expr_can_throw(stmt.value)
				if isinstance(stmt, H.HAugAssign):
					return expr_can_throw(stmt.value) or expr_can_throw(stmt.target)
				if isinstance(stmt, H.HReturn):
					return expr_can_throw(stmt.value) if stmt.value is not None else False
				if isinstance(stmt, H.HIf):
					if expr_can_throw(stmt.cond):
						return True
					if block_can_throw(stmt.then_block):
						return True
					return block_can_throw(stmt.else_block) if stmt.else_block is not None else False
				if isinstance(stmt, H.HLoop):
					return block_can_throw(stmt.body)
				if isinstance(stmt, H.HTry):
					if block_can_throw(stmt.body):
						return True
					return any(block_can_throw(arm.block) for arm in stmt.catches)
				return False

			def block_can_throw(block: H.HBlock | None) -> bool:
				if block is None:
					return False
				return any(stmt_can_throw(stmt) for stmt in block.statements)

			if lam.body_expr is not None:
				return expr_can_throw(lam.body_expr)
			if lam.body_block is not None:
				return block_can_throw(lam.body_block)
			return False

		def _loc_from_span(span: Span) -> parser_ast.Located:
			return parser_ast.Located(line=span.line or 0, column=span.column or 0)

		def _trait_subject_to_parser(subject: object) -> object:
			if isinstance(subject, H.HSelfRef):
				return parser_ast.SelfRef(loc=_loc_from_span(subject.loc))
			if isinstance(subject, H.HTypeNameRef):
				# Forward `module_id` so qualified subjects round-
				# trip H → parser_ast without losing the qualifier.
				# Same drop-shape as the `_to_jsonable` TypeNameRef
				# collision closed at 0.31.28 and the AST→HIR drop
				# closed at 0.31.29; this site is the matching
				# back-conversion leg.
				return parser_ast.TypeNameRef(
					name=subject.name,
					loc=_loc_from_span(subject.loc),
					module_id=getattr(subject, "module_id", None),
				)
			return subject

		def _trait_expr_to_parser(expr: H.HTraitExpr) -> parser_ast.TraitExpr:
			if isinstance(expr, H.HTraitIs):
				loc = _loc_from_span(expr.loc)
				return parser_ast.TraitIs(
					loc=loc,
					subject=_trait_subject_to_parser(expr.subject),
					trait=expr.trait,
				)
			if isinstance(expr, H.HTraitAnd):
				loc = _loc_from_span(expr.loc)
				return parser_ast.TraitAnd(loc=loc, left=_trait_expr_to_parser(expr.left), right=_trait_expr_to_parser(expr.right))
			if isinstance(expr, H.HTraitOr):
				loc = _loc_from_span(expr.loc)
				return parser_ast.TraitOr(loc=loc, left=_trait_expr_to_parser(expr.left), right=_trait_expr_to_parser(expr.right))
			if isinstance(expr, H.HTraitNot):
				loc = _loc_from_span(expr.loc)
				return parser_ast.TraitNot(loc=loc, expr=_trait_expr_to_parser(expr.expr))
			raise TypeError(f"unsupported trait expr node: {type(expr).__name__}")

		def _collect_trait_subjects(expr: parser_ast.TraitExpr, out: set[object]) -> None:
			if isinstance(expr, parser_ast.TraitIs):
				subj = expr.subject
				if isinstance(subj, TypeParamId):
					out.add(subj)
				subj_name = _subject_name(subj)
				if subj_name is not None:
					out.add(subj_name)
				def _collect_trait_args(arg: parser_ast.TypeExpr) -> None:
					if not getattr(arg, "args", None):
						out.add(arg.name)
						return
					for child in (getattr(arg, "args", []) or []):
						_collect_trait_args(child)
				for arg in (getattr(expr.trait, "args", []) or []):
					_collect_trait_args(arg)
				return
				out.add(subj)
			elif isinstance(expr, (parser_ast.TraitAnd, parser_ast.TraitOr)):
				_collect_trait_subjects(expr.left, out)
				_collect_trait_subjects(expr.right, out)
			elif isinstance(expr, parser_ast.TraitNot):
				_collect_trait_subjects(expr.expr, out)

		def _first_obligation_failure(
			*,
			req_expr: parser_ast.TraitExpr,
			subst: dict[object, object],
			origin: ObligationOrigin,
			span: Span,
			env: TraitEnv,
			world: TraitWorld | None,
		) -> ProofFailure | None:
			if world is None:
				return None
			atoms: list[parser_ast.TraitIs] = []
			_collect_trait_is(req_expr, atoms)
			def _resolve_trait_arg(arg: parser_ast.TypeExpr) -> TypeKey:
				if not getattr(arg, "args", None):
					subj = subst.get(arg.name)
					if isinstance(subj, TypeKey):
						return subj
					if arg.name == "Self":
						subj = subst.get("Self")
						if isinstance(subj, TypeKey):
							return subj
				key = type_key_from_expr(
					arg,
					default_module=env.default_module,
					default_package=env.default_package,
					module_packages=env.module_packages,
				)
				if not getattr(arg, "args", None):
					return key
				args = tuple(_resolve_trait_arg(a) for a in (getattr(arg, "args", []) or []))
				if args == key.args:
					return key
				return TypeKey(package_id=key.package_id, module=key.module, name=key.name, args=args)
			for atom in atoms:
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=env.default_module,
					default_package=env.default_package,
					module_packages=env.module_packages,
					type_param_subst=subst,
				)
				trait_args = tuple(
					_resolve_trait_arg(a) for a in (getattr(atom.trait, "args", []) or [])
				)
				lookup_key = _subject_lookup_key(atom.subject)
				subject_key = subst.get(lookup_key)
				if subject_key is None and isinstance(lookup_key, TypeParamId):
					subject_key = subst.get(lookup_key)
				if subject_key is None:
					continue
				obl = Obligation(
					subject=subject_key,
					trait=trait_key,
					trait_args=trait_args,
					origin=origin,
					span=span,
				)
				failure = prove_obligation(world, env, obl)
				if failure is not None:
					return failure
			return None

		def _failure_reason_for_status(status: ProofStatus) -> ProofFailureReason:
			if status is ProofStatus.AMBIGUOUS:
				return ProofFailureReason.AMBIGUOUS_IMPL
			if status is ProofStatus.UNKNOWN:
				return ProofFailureReason.UNKNOWN
			return ProofFailureReason.NO_IMPL

		def _require_failure(
			*,
			req_expr: parser_ast.TraitExpr,
			subst: dict[object, object],
			origin: ObligationOrigin,
			span: Span,
			env: TraitEnv,
			world: TraitWorld | None,
			result: ProofResult | None = None,
		) -> ProofFailure | None:
			if world is None:
				return None
			res = result or prove_expr(world, env, subst, req_expr)
			if res.status is ProofStatus.PROVED:
				return None
			reason = _failure_reason_for_status(res.status)
			if isinstance(req_expr, parser_ast.TraitOr):
				left_res = prove_expr(world, env, subst, req_expr.left)
				right_res = prove_expr(world, env, subst, req_expr.right)
				notes: list[str] = []
				left_failure = _require_failure(
					req_expr=req_expr.left,
					subst=subst,
					origin=origin,
					span=span,
					env=env,
					world=world,
					result=left_res,
				)
				if left_failure is not None:
					notes.append(_format_failure_message(left_failure))
				right_failure = _require_failure(
					req_expr=req_expr.right,
					subst=subst,
					origin=origin,
					span=span,
					env=env,
					world=world,
					result=right_res,
				)
				if right_failure is not None:
					notes.append(_format_failure_message(right_failure))
				base_failure = _first_obligation_failure(
					req_expr=req_expr,
					subst=subst,
					origin=origin,
					span=span,
					env=env,
					world=world,
				)
				message = f"requirement not satisfied: expected {_trait_expr_label(req_expr)}"
				if base_failure is not None:
					obl = Obligation(
						subject=base_failure.obligation.subject,
						trait=base_failure.obligation.trait,
						trait_args=base_failure.obligation.trait_args,
						origin=origin,
						span=span,
						notes=notes,
					)
					return ProofFailure(
						obligation=obl,
						reason=reason,
						impl_ids=base_failure.impl_ids,
						details=tuple(res.reasons),
						message_override=message,
					)
				placeholder = Obligation(
					subject=TypeKey(package_id=None, module=None, name="<unknown>", args=()),
					trait=TraitKey(package_id=None, module=None, name="<unknown>"),
					origin=origin,
					span=span,
					notes=notes,
				)
				return ProofFailure(
					obligation=placeholder,
					reason=reason,
					details=tuple(res.reasons),
					message_override=message,
				)
			if isinstance(req_expr, parser_ast.TraitNot):
				message = f"requirement not satisfied: expected {_trait_expr_label(req_expr)}"
				notes = list(res.reasons)
				base_failure = _first_obligation_failure(
					req_expr=req_expr,
					subst=subst,
					origin=origin,
					span=span,
					env=env,
					world=world,
				)
				if base_failure is not None:
					obl = Obligation(
						subject=base_failure.obligation.subject,
						trait=base_failure.obligation.trait,
						trait_args=base_failure.obligation.trait_args,
						origin=origin,
						span=span,
						notes=notes,
					)
					return ProofFailure(
						obligation=obl,
						reason=reason,
						impl_ids=base_failure.impl_ids,
						details=tuple(res.reasons),
						message_override=message,
					)
				placeholder = Obligation(
					subject=TypeKey(package_id=None, module=None, name="<unknown>", args=()),
					trait=TraitKey(package_id=None, module=None, name="<unknown>"),
					origin=origin,
					span=span,
					notes=notes,
				)
				return ProofFailure(
					obligation=placeholder,
					reason=reason,
					details=tuple(res.reasons),
					message_override=message,
				)
			failure = _first_obligation_failure(
				req_expr=req_expr,
				subst=subst,
				origin=origin,
				span=span,
				env=env,
				world=world,
			)
			if failure is not None:
				return ProofFailure(
					obligation=failure.obligation,
					reason=reason,
					impl_ids=failure.impl_ids,
					details=failure.details,
				)
			atoms: list[parser_ast.TraitIs] = []
			_collect_trait_is(req_expr, atoms)
			for atom in atoms:
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=env.default_module,
					default_package=env.default_package,
					module_packages=env.module_packages,
					type_param_subst=subst,
				)
				def _resolve_trait_arg(arg: parser_ast.TypeExpr) -> TypeKey:
					if not getattr(arg, "args", None):
						subj = subst.get(arg.name)
						if isinstance(subj, TypeKey):
							return subj
						if arg.name == "Self":
							subj = subst.get("Self")
							if isinstance(subj, TypeKey):
								return subj
					key = type_key_from_expr(
						arg,
						default_module=env.default_module,
						default_package=env.default_package,
						module_packages=env.module_packages,
					)
					if not getattr(arg, "args", None):
						return key
					args = tuple(_resolve_trait_arg(a) for a in (getattr(arg, "args", []) or []))
					if args == key.args:
						return key
					return TypeKey(package_id=key.package_id, module=key.module, name=key.name, args=args)
				trait_args = tuple(
					_resolve_trait_arg(a) for a in (getattr(atom.trait, "args", []) or [])
				)
				lookup_key = _subject_lookup_key(atom.subject)
				subject_key = subst.get(lookup_key)
				if subject_key is None and isinstance(lookup_key, TypeParamId):
					subject_key = subst.get(lookup_key)
				if subject_key is None:
					continue
				obl = Obligation(
					subject=subject_key,
					trait=trait_key,
					trait_args=trait_args,
					origin=origin,
					span=span,
					notes=list(res.reasons),
				)
				return ProofFailure(
					obligation=obl,
					reason=reason,
					details=tuple(res.reasons),
				)
			return None

		def _format_failure_message(failure: ProofFailure) -> str:
			if failure.message_override:
				msg = failure.message_override
			else:
				subj = _type_key_label(failure.obligation.subject)
				trait = _trait_label(failure.obligation.trait)
				if failure.reason is ProofFailureReason.AMBIGUOUS_IMPL:
					msg = f"requirement is ambiguous: {subj} is {trait}"
				elif failure.reason is ProofFailureReason.UNKNOWN:
					msg = f"requirement cannot be proven: {subj} is {trait}"
				else:
					msg = f"requirement not satisfied: {subj} is {trait}"
			label = failure.obligation.origin.label
			if label:
				msg = f"{msg} (required by {label})"
			return msg

		def _failure_code(failure: ProofFailure) -> str:
			if failure.reason is ProofFailureReason.AMBIGUOUS_IMPL:
				return "E_REQUIREMENT_AMBIGUOUS"
			if failure.reason is ProofFailureReason.UNKNOWN:
				return "E_REQUIREMENT_UNKNOWN"
			return "E_REQUIREMENT_NOT_SATISFIED"

		def _requirement_notes(failure: ProofFailure) -> list[str]:
			notes = list(getattr(failure.obligation, "notes", []) or [])
			notes.append(f"requirement_trait={_trait_label(failure.obligation.trait)}")
			notes.append(f"requirement_subject={_type_key_label(failure.obligation.subject)}")
			notes.append(f"requirement_reason={failure.reason.name.lower()}")
			label = failure.obligation.origin.label
			if label:
				notes.append(f"requirement_origin={label}")
			return notes

		def _pick_best_failure(failures: list[ProofFailure]) -> ProofFailure | None:
			if not failures:
				return None
			priority = {
				ProofFailureReason.AMBIGUOUS_IMPL: 0,
				ProofFailureReason.UNKNOWN: 1,
				ProofFailureReason.NO_IMPL: 2,
			}
			def _key(f: ProofFailure) -> tuple[int, str, str, str]:
				return (
					priority.get(f.reason, 9),
					_trait_label(f.obligation.trait),
					_type_key_label(f.obligation.subject),
					f.obligation.origin.label or "",
				)
			return sorted(failures, key=_key)[0]

		def _candidate_key_for_decl(decl: CallableDecl) -> object:
			return decl.fn_id if decl.fn_id is not None else ("callable", decl.callable_id)

		def _param_scope_map(sig: FnSignature | None) -> dict[TypeParamId, tuple[str, int]]:
			scope: dict[TypeParamId, tuple[str, int]] = {}
			if sig is None:
				return scope
			for idx, tp in enumerate(getattr(sig, "impl_type_params", []) or []):
				scope[tp.id] = ("impl", idx)
			for idx, tp in enumerate(getattr(sig, "type_params", []) or []):
				scope[tp.id] = ("fn", idx)
			return scope

		def _dedupe_by_key(items: list[tuple], key_fn) -> list[tuple]:
			seen: set[object] = set()
			out: list[tuple] = []
			for item in items:
				key = key_fn(item)
				if key in seen:
					continue
				seen.add(key)
				out.append(item)
			return out

		def _pick_most_specific_items(
			items: list[tuple],
			key_fn,
			require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]],
		) -> list[tuple]:
			if len(items) <= 1:
				return items
			if require_env_local is None:
				return items
			formulas: dict[object, object] = {}
			for item in items:
				key = key_fn(item)
				info = require_info.get(key)
				if info is None:
					formula = BOOL_TRUE
				else:
					req_expr, subst, def_mod, scope_map = info
					formula = require_env_local.normalized(
						req_expr,
						subst=subst,
						default_module=def_mod,
						param_scope_map=scope_map,
					)
				formulas[key] = formula
			winners: list[tuple] = []
			for item in items:
				key = key_fn(item)
				base = formulas.get(key, BOOL_TRUE)
				is_dominated = False
				for other in items:
					other_key = key_fn(other)
					if other_key == key:
						continue
					other_formula = formulas.get(other_key, BOOL_TRUE)
					if require_env_local.implies(other_formula, base) and not require_env_local.implies(base, other_formula):
						is_dominated = True
						break
				if not is_dominated:
					winners.append(item)
			return winners

		def _combine_require(
			left: parser_ast.TraitExpr | None,
			right: parser_ast.TraitExpr | None,
		) -> parser_ast.TraitExpr | None:
			if left is None:
				return right
			if right is None:
				return left
			loc = getattr(left, "loc", None) or getattr(right, "loc", None)
			return parser_ast.TraitAnd(loc=loc, left=left, right=right)

		def _label_typeid(tid: TypeId) -> str:
			return _type_key_label(type_key_from_typeid(self.type_table, tid))

		def _format_infer_failure(ctx: InferContext, res: InferResult) -> tuple[str, list[str]]:
			return format_infer_failure(ctx, res, label_typeid=_label_typeid)

		def _infer(ctx: InferContext) -> InferResult:
			trace = InferTrace()
			type_param_ids = list(ctx.type_param_ids)
			if not type_param_ids:
				return InferResult(
					ok=True,
					subst=None,
					inst_params=list(ctx.param_types),
					inst_return=ctx.return_type,
					trace=trace,
					context=ctx,
				)
			type_param_set = set(type_param_ids)
			if len(ctx.param_types) != len(ctx.arg_types):
				return InferResult(
					ok=False,
					subst=None,
					inst_params=None,
					inst_return=None,
					trace=trace,
					error=InferError(kind=InferErrorKind.ARITY),
					context=ctx,
				)
			constraints: list[InferConstraint] = []
			for idx, (p, a) in enumerate(zip(ctx.param_types, ctx.arg_types)):
				origin = None
				if ctx.call_kind == "ctor" and ctx.param_names and idx < len(ctx.param_names):
					origin = InferConstraintOrigin(kind="ctor_field", name=ctx.param_names[idx])
				elif ctx.receiver_type is not None and idx == 0:
					origin = InferConstraintOrigin(kind="receiver")
				else:
					name = ctx.param_names[idx] if ctx.param_names and idx < len(ctx.param_names) else None
					origin = InferConstraintOrigin(kind="arg", index=idx, name=name)
				constraints.append(
					InferConstraint(lhs=p, rhs=a, origin=origin, span=ctx.span)
				)
			if ctx.expected_return is not None and ctx.return_type is not None:
				constraints.append(
					InferConstraint(
						lhs=ctx.return_type,
						rhs=ctx.expected_return,
						origin=InferConstraintOrigin(kind="expected_return"),
						span=ctx.span,
					)
				)

			bindings: dict[TypeParamId, TypeId] = {}

			def _record_binding(tp_id: TypeParamId, actual: TypeId, origin: InferConstraintOrigin, span: Span) -> None:
				trace.bindings.setdefault(tp_id, []).append(
					InferBindingEvidence(param_id=tp_id, bound_to=actual, origin=origin, span=span)
				)

			def _record_conflict(lhs: TypeId, rhs: TypeId, origin: InferConstraintOrigin, span: Span, param_id: TypeParamId | None = None) -> None:
				trace.conflicts.append(
					InferConflictEvidence(lhs=lhs, rhs=rhs, origin=origin, span=span, param_id=param_id)
				)

			def _bind_typevar(tp_id: TypeParamId, actual: TypeId, origin: InferConstraintOrigin, span: Span) -> bool:
				if actual == self._unknown:
					return True
				prev = bindings.get(tp_id)
				if prev is None or prev == self._unknown:
					bindings[tp_id] = actual
					_record_binding(tp_id, actual, origin, span)
					return True
				if prev == actual:
					return True
				_record_conflict(prev, actual, origin, span, param_id=tp_id)
				return False

			def unify(param_ty: TypeId, actual_ty: TypeId, origin: InferConstraintOrigin, span: Span) -> bool:
				if param_ty == actual_ty:
					return True
				pd = self.type_table.get(param_ty)
				ad = self.type_table.get(actual_ty)
				if pd.kind is TypeKind.TYPEVAR and pd.type_param_id in type_param_set:
					return _bind_typevar(pd.type_param_id, actual_ty, origin, span)
				if ad.kind is TypeKind.TYPEVAR and ad.type_param_id in type_param_set:
					return _bind_typevar(ad.type_param_id, param_ty, origin, span)
				if pd.kind != ad.kind:
					_record_conflict(param_ty, actual_ty, origin, span)
					return False
				if pd.kind in (TypeKind.ARRAY, TypeKind.FNRESULT):
					if len(pd.param_types) != len(ad.param_types):
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return all(unify(p, a, origin, span) for p, a in zip(pd.param_types, ad.param_types))
				if pd.kind is TypeKind.REF:
					if pd.ref_mut != ad.ref_mut or not pd.param_types or not ad.param_types:
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return unify(pd.param_types[0], ad.param_types[0], origin, span)
				if pd.kind is TypeKind.STRUCT:
					p_inst = self.type_table.get_struct_instance(param_ty)
					a_inst = self.type_table.get_struct_instance(actual_ty)
					if p_inst is not None or a_inst is not None:
						p_base = p_inst.base_id if p_inst is not None else param_ty
						a_base = a_inst.base_id if a_inst is not None else actual_ty
						if p_base != a_base:
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						p_args = list(p_inst.type_args) if p_inst is not None else []
						a_args = list(a_inst.type_args) if a_inst is not None else []
						if len(p_args) != len(a_args):
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						return all(unify(p, a, origin, span) for p, a in zip(p_args, a_args))
					if pd.name != ad.name or pd.module_id != ad.module_id:
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return True
				if pd.kind is TypeKind.INTERFACE:
					p_inst = self.type_table.get_interface_instance(param_ty)
					a_inst = self.type_table.get_interface_instance(actual_ty)
					if p_inst is not None or a_inst is not None:
						p_base = p_inst.base_id if p_inst is not None else param_ty
						a_base = a_inst.base_id if a_inst is not None else actual_ty
						if p_base != a_base:
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						p_args = list(p_inst.type_args) if p_inst is not None else []
						a_args = list(a_inst.type_args) if a_inst is not None else []
						if len(p_args) != len(a_args):
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						return all(unify(p, a, origin, span) for p, a in zip(p_args, a_args))
					if pd.name != ad.name or pd.module_id != ad.module_id:
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return True
				if pd.kind is TypeKind.VARIANT:
					p_inst = self.type_table.get_variant_instance(param_ty)
					a_inst = self.type_table.get_variant_instance(actual_ty)
					if p_inst is None and a_inst is not None and pd.param_types:
						base_id = self.type_table.get_variant_base(
							module_id=pd.module_id or "", name=pd.name
						)
						if base_id is None or base_id != a_inst.base_id:
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						p_args = list(pd.param_types)
						a_args = list(a_inst.type_args)
						if len(p_args) != len(a_args):
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						return all(unify(p, a, origin, span) for p, a in zip(p_args, a_args))
					if p_inst is not None and a_inst is None and ad.param_types:
						base_id = self.type_table.get_variant_base(
							module_id=ad.module_id or "", name=ad.name
						)
						if base_id is None or base_id != p_inst.base_id:
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						p_args = list(p_inst.type_args)
						a_args = list(ad.param_types)
						if len(p_args) != len(a_args):
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						return all(unify(p, a, origin, span) for p, a in zip(p_args, a_args))
					if p_inst is not None or a_inst is not None:
						p_base = p_inst.base_id if p_inst is not None else param_ty
						a_base = a_inst.base_id if a_inst is not None else actual_ty
						if p_base != a_base:
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						p_args = list(p_inst.type_args) if p_inst is not None else []
						a_args = list(a_inst.type_args) if a_inst is not None else []
						if len(p_args) != len(a_args):
							_record_conflict(param_ty, actual_ty, origin, span)
							return False
						return all(unify(p, a, origin, span) for p, a in zip(p_args, a_args))
					if pd.name != ad.name or pd.module_id != ad.module_id:
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return True
				if pd.kind is TypeKind.FUNCTION:
					p_throw = pd.can_throw()
					a_throw = ad.can_throw()
					if p_throw != a_throw:
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					if len(pd.param_types) != len(ad.param_types):
						_record_conflict(param_ty, actual_ty, origin, span)
						return False
					return all(unify(p, a, origin, span) for p, a in zip(pd.param_types, ad.param_types))
				_record_conflict(param_ty, actual_ty, origin, span)
				return False

			for _ in range(2):
				for constraint in constraints:
					if not unify(constraint.lhs, constraint.rhs, constraint.origin, constraint.span):
						return InferResult(
							ok=False,
							subst=None,
							inst_params=None,
							inst_return=None,
							trace=trace,
							error=InferError(kind=InferErrorKind.CONFLICT, conflicts=trace.conflicts),
							context=ctx,
						)

			args: list[TypeId] = []
			missing: list[TypeParamId] = []
			for pid in type_param_ids:
				bound = bindings.get(pid)
				if bound is None:
					missing.append(pid)
					continue
				args.append(bound)
			if missing:
				return InferResult(
					ok=False,
					subst=None,
					inst_params=None,
					inst_return=None,
					trace=trace,
					error=InferError(kind=InferErrorKind.CANNOT_INFER, missing_params=missing),
					context=ctx,
				)

			subst = Subst(owner=type_param_ids[0].owner, args=args)
			inst_params = [apply_subst(p, subst, self.type_table) for p in ctx.param_types]
			inst_return = apply_subst(ctx.return_type, subst, self.type_table) if ctx.return_type is not None else None
			return InferResult(
				ok=True,
				subst=subst,
				inst_params=inst_params,
				inst_return=inst_return,
				trace=trace,
				context=ctx,
			)

		def _instantiate_sig_with_subst(
			*,
			sig: FnSignature,
			arg_types: list[TypeId],
			expected_type: TypeId | None,
			explicit_type_args: list[TypeId] | None,
			allow_infer: bool,
			require_expr: parser_ast.TraitExpr | None = None,
			diag_span: Span | None = None,
			call_kind: str = "call",
			call_name: str = "",
			receiver_type: TypeId | None = None,
		) -> InferResult:
			if sig.param_type_ids is None or sig.return_type_id is None:
				return InferResult(
					ok=False,
					subst=None,
					inst_params=None,
					inst_return=None,
					error=InferError(kind=InferErrorKind.NO_TYPES),
				)
			if explicit_type_args:
				if not sig.type_params:
					return InferResult(
						ok=False,
						subst=None,
						inst_params=None,
						inst_return=None,
						error=InferError(kind=InferErrorKind.NO_TYPEPARAMS),
					)
				if len(explicit_type_args) != len(sig.type_params):
					return InferResult(
						ok=False,
						subst=None,
						inst_params=None,
						inst_return=None,
						error=InferError(
							kind=InferErrorKind.TYPEARG_COUNT,
							expected_count=len(sig.type_params),
						),
					)
				subst = Subst(owner=sig.type_params[0].id.owner, args=list(explicit_type_args))
				for arg in subst.args:
					_enforce_struct_requires(arg, diag_span or Span())
				inst_params = [apply_subst(p, subst, self.type_table) for p in sig.param_type_ids]
				inst_return = apply_subst(sig.return_type_id, subst, self.type_table)
				return InferResult(
					ok=True,
					subst=subst,
					inst_params=inst_params,
					inst_return=inst_return,
					context=None,
				)
			if sig.type_params:
				if not allow_infer:
					return InferResult(
						ok=False,
						subst=None,
						inst_params=None,
						inst_return=None,
						error=InferError(kind=InferErrorKind.CANNOT_INFER),
					)
				if require_expr is not None and sig.param_type_ids is not None:
					type_param_ids = [p.id for p in sig.type_params]
					bindings: dict[TypeParamId, TypeId] = {}
					for idx, p in enumerate(sig.param_type_ids):
						if idx >= len(arg_types):
							break
						pd = self.type_table.get(p)
						if pd.kind is TypeKind.TYPEVAR and pd.type_param_id in type_param_ids:
							bindings[pd.type_param_id] = arg_types[idx]
					name_to_id = {p.name: p.id for p in sig.type_params}
					for atom in _extract_conjunctive_facts(require_expr):
						if not isinstance(atom, parser_ast.TraitIs):
							continue
						trait_name = getattr(atom.trait, "name", None)
						exp = _fn_trait_expected(trait_name) if trait_name is not None else None
						if exp is None:
							continue
						need, trait_can_throw = exp
						if trait_name in ("Fn0", "FnThrow0") and len(arg_types) == 1 and len(type_param_ids) >= 2:
							ad = self.type_table.get(arg_types[0])
							if ad.kind is TypeKind.FUNCTION and ad.can_throw() == trait_can_throw:
								bindings.setdefault(type_param_ids[0], arg_types[0])
								bindings.setdefault(type_param_ids[1], ad.param_types[-1])
						subj = atom.subject
						subj_id = None
						if isinstance(subj, TypeParamId):
							subj_id = subj
						elif isinstance(subj, parser_ast.TypeNameRef):
							subj_id = name_to_id.get(subj.name)
						if subj_id is None:
							continue
						subj_ty = bindings.get(subj_id)
						if subj_ty is None:
							continue
						td = self.type_table.get(subj_ty)
						if td.kind is not TypeKind.FUNCTION:
							continue
						if td.can_throw() != trait_can_throw:
							continue
						fn_parts = list(td.param_types)
						if len(fn_parts) != need:
							continue
						trait_args = list(getattr(atom.trait, "args", []) or [])
						if len(trait_args) != need:
							continue
						for targ, farg in zip(trait_args, fn_parts):
							if isinstance(targ, parser_ast.TypeNameRef):
								tp_id = name_to_id.get(targ.name)
								if tp_id is not None:
									bindings.setdefault(tp_id, farg)
					if bindings and len(bindings) == len(type_param_ids):
						subst = Subst(owner=type_param_ids[0].owner, args=[bindings[pid] for pid in type_param_ids])
						inst_params = [apply_subst(p, subst, self.type_table) for p in sig.param_type_ids]
						inst_return = apply_subst(sig.return_type_id, subst, self.type_table) if sig.return_type_id is not None else None
						return InferResult(
							ok=True,
							subst=subst,
							inst_params=inst_params,
							inst_return=inst_return,
							context=None,
						)
				type_param_names = {p.id: p.name for p in sig.type_params}
				expected_for_infer = expected_type
				if expected_for_infer is not None and sig.return_type_id is not None:
					def _expected_matches_return(exp: TypeId, ret: TypeId) -> bool:
						if exp == self._unknown or ret == self._unknown:
							return True
						exp_def = self.type_table.get(exp)
						ret_def = self.type_table.get(ret)
						if ret_def.kind is TypeKind.TYPEVAR:
							return True
						if exp_def.kind is TypeKind.REF and ret_def.kind is TypeKind.REF:
							if exp_def.param_types and ret_def.param_types:
								return _expected_matches_return(exp_def.param_types[0], ret_def.param_types[0])
							return True
						if exp_def.kind != ret_def.kind:
							return False
						if exp_def.kind in {TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.ARRAY}:
							exp_base, _ = _struct_base_and_args(exp)
							ret_base, _ = _struct_base_and_args(ret)
							return exp_base == ret_base
						if exp_def.kind is TypeKind.INTERFACE:
							exp_base = self.type_table.interface_bases.get(exp)
							ret_base = self.type_table.interface_bases.get(ret)
							if exp_base is not None and ret_base is not None:
								return exp_base == ret_base
						return exp == ret
					if not _expected_matches_return(expected_for_infer, sig.return_type_id):
						expected_for_infer = None
				ctx = InferContext(
					call_kind="method" if call_kind == "method" else call_kind,
					call_name=call_name or sig.name,
					span=diag_span or Span(),
					type_param_ids=[p.id for p in sig.type_params],
					type_param_names=type_param_names,
					param_types=list(sig.param_type_ids),
					param_names=list(sig.param_names) if sig.param_names else None,
					return_type=sig.return_type_id,
					arg_types=list(arg_types),
					receiver_type=receiver_type,
					expected_return=expected_for_infer,
				)
				res = _infer(ctx)
				if not res.ok or res.subst is None:
					return res
				subst = res.subst
				inst_params = [apply_subst(p, subst, self.type_table) for p in sig.param_type_ids]
				inst_return = apply_subst(sig.return_type_id, subst, self.type_table)
				for arg in subst.args:
					_enforce_struct_requires(arg, diag_span or Span())
				res.inst_params = inst_params
				res.inst_return = inst_return
				return res
			return InferResult(
				ok=True,
				subst=None,
				inst_params=list(sig.param_type_ids),
				inst_return=sig.return_type_id,
			)

		def _instantiate_sig(
			*,
			sig: FnSignature,
			arg_types: list[TypeId],
			expected_type: TypeId | None,
			explicit_type_args: list[TypeId] | None,
			allow_infer: bool,
			require_expr: parser_ast.TraitExpr | None = None,
			diag_span: Span | None = None,
			call_kind: str = "call",
			call_name: str = "",
			receiver_type: TypeId | None = None,
		) -> InferResult:
			res = _instantiate_sig_with_subst(
				sig=sig,
				arg_types=arg_types,
				expected_type=expected_type,
				explicit_type_args=explicit_type_args,
				allow_infer=allow_infer,
				require_expr=require_expr,
				diag_span=diag_span,
				call_kind=call_kind,
				call_name=call_name,
				receiver_type=receiver_type,
			)
			return res

		def _receiver_compat(
			receiver_type: TypeId,
			param_self: TypeId,
			self_mode: SelfMode | None,
		) -> tuple[bool, Optional[SelfMode]]:
			def _generic_nominal_match(param_inner: TypeId, recv_inner: TypeId) -> bool:
				if not _type_has_typevar(param_inner):
					return False
				param_base, _ = _struct_base_and_args(param_inner)
				recv_base, _ = _struct_base_and_args(recv_inner)
				return param_base == recv_base

			if self_mode is None:
				return False, None
			if self_mode is SelfMode.SELF_BY_VALUE:
				if receiver_type == param_self:
					return True, None
				if _generic_nominal_match(param_self, receiver_type):
					return True, None
				return False, None
			td_param = self.type_table.get(param_self)
			td_recv = self.type_table.get(receiver_type)
			if self_mode is SelfMode.SELF_BY_REF:
				if receiver_type == param_self and td_recv.kind is TypeKind.REF and td_recv.ref_mut is False:
					return True, None
				if td_param.kind is TypeKind.REF and td_param.ref_mut is False and td_param.param_types:
					param_inner = td_param.param_types[0]
					if param_inner == receiver_type or _generic_nominal_match(param_inner, receiver_type):
						return True, SelfMode.SELF_BY_REF
					if td_recv.kind is TypeKind.REF and td_recv.ref_mut is True and td_recv.param_types and td_param.param_types[0] == td_recv.param_types[0]:
						return True, SelfMode.SELF_BY_REF
					if td_recv.kind is TypeKind.REF and td_recv.ref_mut is True and td_recv.param_types and _generic_nominal_match(param_inner, td_recv.param_types[0]):
						return True, SelfMode.SELF_BY_REF
				return False, None
			if self_mode is SelfMode.SELF_BY_REF_MUT:
				if receiver_type == param_self and td_recv.kind is TypeKind.REF and td_recv.ref_mut is True:
					return True, None
				if td_param.kind is TypeKind.REF and td_param.ref_mut is True and td_param.param_types:
					param_inner = td_param.param_types[0]
					if td_recv.kind is TypeKind.REF and td_recv.ref_mut is True and td_recv.param_types:
						recv_inner = td_recv.param_types[0]
						if param_inner == recv_inner or _generic_nominal_match(param_inner, recv_inner):
							return True, None
					if param_inner == receiver_type or _generic_nominal_match(param_inner, receiver_type):
						return True, SelfMode.SELF_BY_REF_MUT
				return False, None
			return False, None

		def _unwrap_ref_type(ty: TypeId) -> TypeId:
			td = self.type_table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				return td.param_types[0]
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None and len(inst.type_args) == 1:
				base_td = self.type_table.get(inst.base_id)
				if base_td.module_id in ("std.core", "core") and base_td.name in ("Ref", "RefMut"):
					return inst.type_args[0]
			return ty

		def _dealias_zero_param(ty: TypeId, *, _seen: set[tuple[str | None, str]] | None = None) -> TypeId:
			seen = _seen if _seen is not None else set()
			td = self.type_table.get(ty)
			if td.kind is TypeKind.REF and td.param_types:
				inner = _dealias_zero_param(td.param_types[0], _seen=seen)
				return self.type_table.ensure_ref_mut(inner) if td.ref_mut else self.type_table.ensure_ref(inner)
			if td.kind is TypeKind.ARRAY and td.param_types:
				elem = _dealias_zero_param(td.param_types[0], _seen=seen)
				return self.type_table.new_array(elem)
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None and inst.type_args:
				new_args = [_dealias_zero_param(arg, _seen=seen) for arg in inst.type_args]
				return self.type_table.ensure_struct_template(inst.base_id, new_args) if any(self.type_table.has_typevar(arg) for arg in new_args) else self.type_table.ensure_struct_instantiated(inst.base_id, new_args)
			vinst = self.type_table.get_variant_instance(ty)
			if vinst is not None and vinst.type_args:
				new_args = [_dealias_zero_param(arg, _seen=seen) for arg in vinst.type_args]
				return self.type_table.ensure_variant_template(vinst.base_id, new_args) if any(self.type_table.has_typevar(arg) for arg in new_args) else self.type_table.ensure_variant_instantiated(vinst.base_id, new_args)
			mod = td.module_id
			name = td.name
			alias_def = self.type_table.lookup_type_alias(module_id=mod, name=name)
			if alias_def is None:
				return ty
			alias_params, alias_target, _loc = alias_def
			if alias_params:
				return ty
			alias_key = (mod, name)
			if alias_key in seen:
				return ty
			resolved = resolve_opaque_type(
				alias_target,
				self.type_table,
				module_id=mod,
				type_params=None,
				allow_generic_base=True,
			)
			return _dealias_zero_param(resolved, _seen=seen | {alias_key})

		def _resolve_forward_nominal(ty: TypeId) -> TypeId:
			"""K26: resolve FORWARD_NOMINAL TypeId to its concrete counterpart."""
			td = self.type_table.get(ty)
			if td.kind is not TypeKind.FORWARD_NOMINAL:
				return ty
			# Try exact module match first, then cross-module lookup for package types.
			resolved_nom = (
				self.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=td.module_id, name=td.name)
				or self.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=td.module_id, name=td.name)
				or self.type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=td.module_id, name=td.name)
			)
			if resolved_nom is None or resolved_nom == ty:
				resolved_nom = (
					self.type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=td.name)
					or self.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=td.name)
					or self.type_table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=td.name)
				)
			if resolved_nom is None or resolved_nom == ty:
				return ty
			if td.param_types:
				canon_args = [_resolve_forward_nominal(a) for a in td.param_types]
				try:
					if resolved_nom in self.type_table.struct_bases:
						return self.type_table.ensure_struct_instantiated(resolved_nom, canon_args)
					if resolved_nom in getattr(self.type_table, "variant_schemas", {}):
						return self.type_table.ensure_variant_instantiated(resolved_nom, canon_args)
				except (ValueError, KeyError):
					return ty
			return resolved_nom

		def _struct_base_and_args(ty: TypeId) -> tuple[TypeId, list[TypeId]]:
			inst = self.type_table.get_struct_instance(ty)
			if inst is not None:
				return inst.base_id, list(inst.type_args)
			vinst = self.type_table.get_variant_instance(ty)
			if vinst is not None:
				return vinst.base_id, list(vinst.type_args)
			td = self.type_table.get(ty)
			if td.kind is TypeKind.ARRAY and td.param_types:
				return self.type_table.array_base_id(), [td.param_types[0]]
			# K26: FORWARD_NOMINAL types from package-consumer parsing may not
			# have been resolved to their concrete struct/variant counterparts.
			# Resolve them here so method lookup can find generic impl methods.
			if td.kind is TypeKind.FORWARD_NOMINAL:
				resolved = _resolve_forward_nominal(ty)
				if resolved != ty:
					return _struct_base_and_args(resolved)
			if td.kind is TypeKind.STRUCT:
				param_ids = self.type_table.get_struct_type_param_ids(ty)
				if param_ids:
					schema = self.type_table.struct_bases.get(ty)
					names = list(schema.type_params) if schema is not None else []
					typevars: list[TypeId] = []
					for idx, pid in enumerate(param_ids):
						name = names[idx] if idx < len(names) else None
						typevars.append(self.type_table.ensure_typevar(pid, name=name))
					return ty, typevars
			if td.kind in {TypeKind.STRUCT, TypeKind.VARIANT, TypeKind.SCALAR}:
				return ty, []
			return ty, []

		def _enforce_struct_requires(ty: TypeId, span: Span) -> None:
			base_id, args = _struct_base_and_args(ty)
			base_def = self.type_table.get(base_id)
			if base_def.kind is not TypeKind.STRUCT:
				return
			if any(_type_has_typevar(a) for a in args):
				return
			base_mod = getattr(base_def, "module_id", None)
			base_pkg = (
				getattr(self.type_table, "module_packages", {}).get(base_mod, getattr(self.type_table, "package_id", None))
				if base_mod is not None
				else None
			)
			struct_key = TypeKey(package_id=base_pkg, module=base_mod, name=getattr(base_def, "name", ""), args=())
			req = _require_for_struct(struct_key)
			if req is None:
				return
			param_ids = self.type_table.get_struct_type_param_ids(base_id) or []
			subst: dict[object, object] = {}
			if param_ids and len(param_ids) == len(args):
				for pid, arg in zip(param_ids, args):
					key = _normalize_type_key(type_key_from_typeid(self.type_table, arg))
					subst[pid] = key
			schema = self.type_table.get_struct_schema(base_id)
			if schema is not None and schema.type_params and args:
				for name, arg in zip(schema.type_params, args):
					subst.setdefault(name, _normalize_type_key(type_key_from_typeid(self.type_table, arg)))
			subst.setdefault("Self", _normalize_type_key(type_key_from_typeid(self.type_table, ty)))
			env = TraitEnv(
				default_module=struct_key.module or current_module_name,
				default_package=default_package,
				module_packages=module_packages or {},
				assumed_true=set(fn_require_assumed),
				type_table=self.type_table,
			)
			res = prove_expr(global_trait_world, env, subst, req) if global_trait_world is not None else None
			failure = _require_failure(
				req_expr=req,
				subst=subst,
				origin=ObligationOrigin(
					kind=ObligationOriginKind.CALLEE_REQUIRE,
					label=f"struct '{struct_key.name}'",
					span=Span.from_loc(getattr(req, "loc", None)),
				),
				span=span,
				env=env,
				world=global_trait_world,
				result=res,
			)
			if failure is not None:
				diagnostics.append(
					_tc_diag(
						message=_format_failure_message(failure),
						code=_failure_code(failure),
						severity="error",
						span=span,
						notes=_requirement_notes(failure),
					)
				)

		sig_span = Span()
		if signatures_by_id is not None:
			fn_sig = signatures_by_id.get(fn_id)
			if fn_sig is not None:
				sig_span = Span.from_loc(getattr(fn_sig, "loc", None))
		if param_types:
			for ty in param_types.values():
				_enforce_struct_requires(ty, sig_span)
		if return_type is not None:
			_enforce_struct_requires(return_type, sig_span)

		def _match_impl_type_args(
			*,
			template_args: list[TypeId],
			recv_args: list[TypeId],
			impl_type_params: list[TypeParam],
		) -> Subst | None:
			if not impl_type_params:
				return None
			if len(template_args) != len(recv_args):
				return None
			owner = impl_type_params[0].id.owner
			bindings: list[TypeId | None] = [None] * len(impl_type_params)
			def _bind_typevar(param_id: TypeParamId, recv: TypeId) -> bool:
				if param_id.owner != owner:
					return False
				idx = int(param_id.index)
				if idx < 0 or idx >= len(bindings):
					return False
				if bindings[idx] is None:
					bindings[idx] = recv
					return True
				return bindings[idx] == recv

			def _match_type(tmpl: TypeId, recv: TypeId) -> bool:
				tdef = self.type_table.get(tmpl)
				if tdef.kind is TypeKind.TYPEVAR and tdef.type_param_id is not None:
					return _bind_typevar(tdef.type_param_id, recv)
				if tmpl == recv:
					return True
				rdef = self.type_table.get(recv)
				if tdef.kind is not rdef.kind:
					return False
				if tdef.kind is TypeKind.REF:
					if tdef.ref_mut != rdef.ref_mut:
						return False
					if len(tdef.param_types) != len(rdef.param_types):
						return False
					return _match_type(tdef.param_types[0], rdef.param_types[0])
				if tdef.kind in {TypeKind.ARRAY, TypeKind.FNRESULT, TypeKind.FUNCTION}:
					if tdef.kind is TypeKind.FUNCTION:
						t_throw = tdef.can_throw()
						r_throw = rdef.can_throw()
						if t_throw != r_throw:
							return False
					if len(tdef.param_types) != len(rdef.param_types):
						return False
					for sub_t, sub_r in zip(tdef.param_types, rdef.param_types):
						if not _match_type(sub_t, sub_r):
							return False
					return True
				if tdef.kind is TypeKind.STRUCT:
					tmpl_inst = self.type_table.get_struct_instance(tmpl)
					recv_inst = self.type_table.get_struct_instance(recv)
					if tmpl_inst is None and recv_inst is None:
						return tmpl == recv
					if tmpl_inst is None or recv_inst is None:
						return False
					if tmpl_inst.base_id != recv_inst.base_id:
						return False
					if len(tmpl_inst.type_args) != len(recv_inst.type_args):
						return False
					for sub_t, sub_r in zip(tmpl_inst.type_args, recv_inst.type_args):
						if not _match_type(sub_t, sub_r):
							return False
					return True
				if tdef.kind is TypeKind.VARIANT:
					tmpl_inst = self.type_table.get_variant_instance(tmpl)
					recv_inst = self.type_table.get_variant_instance(recv)
					if tmpl_inst is None and recv_inst is None:
						return tmpl == recv
					if tmpl_inst is None or recv_inst is None:
						return False
					if tmpl_inst.base_id != recv_inst.base_id:
						return False
					if len(tmpl_inst.type_args) != len(recv_inst.type_args):
						return False
					for sub_t, sub_r in zip(tmpl_inst.type_args, recv_inst.type_args):
						if not _match_type(sub_t, sub_r):
							return False
					return True
				return False

			for tmpl, recv in zip(template_args, recv_args):
				if not _match_type(tmpl, recv):
					return None
			if any(b is None for b in bindings):
				return None
			return Subst(owner=owner, args=[b for b in bindings if b is not None])

		def _fn_id_for_decl(decl: CallableDecl) -> FunctionId | None:
			return decl.fn_id

		def _resolve_free_call_with_require(
			*,
			name: str,
			module_name: str | None,
			arg_types: List[TypeId],
			call_type_args: List[TypeId] | None = None,
			call_type_args_span: Span | None = None,
			expected_type: TypeId | None = None,
		) -> tuple[CallableDecl, CallableSignature, Subst | None]:
			if callable_registry is None:
				raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			include_private = current_module if module_name is None else None
			candidates = callable_registry.get_free_candidates(
				name=name,
				visible_modules=_visible_modules_for_free_call(module_name),
				include_private_in=include_private,
			)
			viable: List[tuple[CallableDecl, CallableSignature, Subst | None]] = []
			type_arg_counts: set[int] = set()
			saw_registry_only_with_type_args = False
			saw_typed_nongeneric_with_type_args = False
			saw_infer_incomplete = False
			saw_require_failed = False
			infer_failures: list[InferResult] = []
			for decl in candidates:
				sig = None
				if decl.fn_id is not None and signatures_by_id is not None:
					sig = signatures_by_id.get(decl.fn_id)

				if sig is None:
					if call_type_args:
						saw_registry_only_with_type_args = True
						continue
					params = list(decl.signature.param_types)
					result_type = decl.signature.result_type
					if len(params) != len(arg_types):
						continue
					if _args_match_params(list(params), arg_types):
						viable.append(
							(
								decl,
								CallableSignature(param_types=tuple(params), result_type=result_type),
								None,
							)
						)
					continue

				param_needs_resolve = sig.param_type_ids is None
				if not param_needs_resolve and sig.param_type_ids is not None:
					param_needs_resolve = any(p is None or p == self._unknown for p in sig.param_type_ids)
				if param_needs_resolve and sig.param_types is not None:
					local_type_params = {p.name: p.id for p in sig.type_params}
					param_type_ids = [
						resolve_opaque_type(p, self.type_table, module_id=sig.module, type_params=local_type_params)
						for p in sig.param_types
					]
					sig = replace(sig, param_type_ids=param_type_ids)

				return_needs_resolve = sig.return_type_id is None
				if not return_needs_resolve and sig.return_type_id is not None:
					ret_def = self.type_table.get(sig.return_type_id)
					if sig.return_type_id == self._unknown:
						return_needs_resolve = True
					elif ret_def.kind is TypeKind.STRUCT:
						base = self.type_table.struct_bases.get(sig.return_type_id)
						inst = self.type_table.get_struct_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
					elif ret_def.kind is TypeKind.INTERFACE:
						base = self.type_table.interface_bases.get(sig.return_type_id)
						inst = self.type_table.get_interface_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
					elif ret_def.kind is TypeKind.VARIANT:
						base = self.type_table.variant_schemas.get(sig.return_type_id)
						inst = self.type_table.get_variant_instance(sig.return_type_id)
						if inst is None and base is not None and base.type_params:
							return_needs_resolve = True
				if return_needs_resolve and sig.return_type is not None:
					local_type_params = {p.name: p.id for p in sig.type_params}
					ret_id = resolve_opaque_type(sig.return_type, self.type_table, module_id=sig.module, type_params=local_type_params)
					sig = replace(sig, return_type_id=ret_id)

				if sig.param_type_ids is None or sig.return_type_id is None:
					continue

				inst_arg_types = _coerce_args_for_params(list(sig.param_type_ids), arg_types)
				req_for_infer = None
				if decl.fn_id is not None:
					req_for_infer = _require_for_fn(decl.fn_id)
				inst_res = _instantiate_sig_with_subst(
					sig=sig,
					arg_types=inst_arg_types,
					expected_type=expected_type,
					explicit_type_args=call_type_args,
					allow_infer=True,
					require_expr=req_for_infer,
					diag_span=call_type_args_span,
					call_kind="free",
					call_name=name,
				)
				if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS and call_type_args:
					saw_typed_nongeneric_with_type_args = True
					continue
				if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT and call_type_args:
					if inst_res.error.expected_count is not None:
						type_arg_counts.add(inst_res.error.expected_count)
					continue
				if inst_res.error and inst_res.error.kind in {InferErrorKind.CANNOT_INFER, InferErrorKind.CONFLICT}:
					saw_infer_incomplete = True
					infer_failures.append(inst_res)
					continue
				if inst_res.error:
					continue
				params = inst_res.inst_params
				result_type = inst_res.inst_return
				inst_subst = inst_res.subst

				if len(params) != len(arg_types):
					continue
				if _args_match_params(list(params), arg_types):
					viable.append(
						(
							decl,
							CallableSignature(param_types=tuple(params), result_type=result_type),
							inst_subst,
						)
					)
			if not viable:
				if call_type_args:
					if type_arg_counts:
						exp = ", ".join(str(n) for n in sorted(type_arg_counts))
						raise ResolutionError(
							f"type argument count mismatch for '{name}': expected one of ({exp}), got {len(call_type_args)}",
							span=call_type_args_span,
						)
					if saw_typed_nongeneric_with_type_args:
						raise ResolutionError(
							f"type arguments require a generic signature for function '{name}'",
							span=call_type_args_span,
						)
					if saw_registry_only_with_type_args:
						raise ResolutionError(
							f"type arguments require a typed signature for function '{name}'",
							span=call_type_args_span,
						)
					raise ResolutionError(f"no matching overload for function '{name}' with provided type arguments")
				if saw_infer_incomplete and infer_failures:
					failure = infer_failures[0]
					ctx = failure.context or InferContext(
						call_kind="free",
						call_name=name,
						span=call_type_args_span or Span(),
						type_param_ids=[],
						type_param_names={},
						param_types=[],
						param_names=None,
						return_type=None,
						arg_types=[],
					)
					msg, notes = _format_infer_failure(ctx, failure)
					raise ResolutionError(msg, span=call_type_args_span, notes=notes)
				if saw_infer_incomplete:
					ctx = InferContext(
						call_kind="free",
						call_name=name,
						span=call_type_args_span or Span(),
						type_param_ids=[],
						type_param_names={},
						param_types=[],
						param_names=None,
						return_type=None,
						arg_types=[],
					)
					res = InferResult(
						ok=False,
						subst=None,
						inst_params=None,
						inst_return=None,
						error=InferError(kind=InferErrorKind.CANNOT_INFER),
						context=ctx,
					)
					msg, notes = _format_infer_failure(ctx, res)
					raise ResolutionError(msg, span=call_type_args_span, notes=notes)
				raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			world = None
			applicable: List[tuple[CallableDecl, CallableSignature, Subst | None]] = []
			require_info: dict[object, tuple[parser_ast.TraitExpr, dict[object, object], str, dict[TypeParamId, tuple[str, int]]]] = {}
			require_failures: list[ProofFailure] = []
			for decl, sig_inst, inst_subst in viable:
				cand_key = decl.fn_id if decl.fn_id is not None else ("callable", decl.callable_id)
				fn_id = _fn_id_for_decl(decl)
				if fn_id is None:
					applicable.append((decl, sig_inst, inst_subst))
					continue
				world = global_trait_world or visible_trait_world
				req = _require_for_fn(fn_id)
				if req is None:
					applicable.append((decl, sig_inst, inst_subst))
					continue
				if req is not None and len(arg_types) == 1:
					ad = self.type_table.get(arg_types[0])
					if ad.kind is TypeKind.FUNCTION:
						facts = _extract_conjunctive_facts(req)
						if facts:
							all_fn = True
							for atom in facts:
								if not isinstance(atom, parser_ast.TraitIs):
									all_fn = False
									break
								trait_name = getattr(atom.trait, "name", None)
								exp = _fn_trait_expected(trait_name) if trait_name is not None else None
								if exp is None:
									all_fn = False
									break
								expected, trait_can_throw = exp
								if ad.can_throw() != trait_can_throw:
									all_fn = False
									break
								if len(ad.param_types) != expected:
									all_fn = False
									break
							if all_fn:
								applicable.append((decl, sig_inst, inst_subst))
								continue
				subjects: set[object] = set()
				_collect_trait_subjects(req, subjects)
				subst: dict[object, object] = {}
				sig = None
				if decl.fn_id is not None and signatures_by_id is not None:
					sig = signatures_by_id.get(decl.fn_id)
				if sig and getattr(sig, "type_params", None):
					type_params = list(getattr(sig, "type_params", []) or [])
					if inst_subst is not None:
						for idx, tp in enumerate(type_params):
							if tp.id in subjects or tp.name in subjects:
								if idx < len(inst_subst.args):
									key = _normalize_type_key(type_key_from_typeid(self.type_table, inst_subst.args[idx]))
									subst[tp.id] = key
									subst[tp.name] = key
					if sig.param_type_ids and len(sig.param_type_ids) == len(arg_types):
						for idx, p in enumerate(sig.param_type_ids):
							pd = self.type_table.get(p)
							if pd.kind is TypeKind.TYPEVAR and pd.type_param_id is not None:
								tp_id = pd.type_param_id
								if tp_id in subjects or type_param_names.get(tp_id) in subjects:
									key = _normalize_type_key(type_key_from_typeid(self.type_table, arg_types[idx]))
									subst[tp_id] = key
									tp_name = type_param_names.get(tp_id)
									if tp_name is not None:
										subst[tp_name] = key
				if sig and sig.param_names:
					for idx, pname in enumerate(sig.param_names):
						if pname in subst:
							continue
						if pname in subjects and idx < len(arg_types):
							key = _normalize_type_key(type_key_from_typeid(self.type_table, arg_types[idx]))
							subst[pname] = key
				if sig and req is not None:
					for atom in _extract_conjunctive_facts(req):
						if not isinstance(atom, parser_ast.TraitIs):
							continue
						trait_name = getattr(atom.trait, "name", None)
						exp = _fn_trait_expected(trait_name) if trait_name is not None else None
						if exp is None:
							continue
						expected, trait_can_throw = exp
						subj = atom.subject
						subj_key = subst.get(subj)
						if subj_key is None and isinstance(subj, parser_ast.TypeNameRef):
							subj_key = subst.get(subj.name)
						if subj_key is None:
							subj_name = _subject_name(subj)
							if subj_name is not None and subj_name in type_param_map and len(arg_types) == 1:
								key = _normalize_type_key(type_key_from_typeid(self.type_table, arg_types[0]))
								tp_id = type_param_map[subj_name]
								subst[tp_id] = key
								subst[subj_name] = key
								subj_key = key
						if subj_key is None and isinstance(subj, TypeParamId) and len(arg_types) == 1:
							key = _normalize_type_key(type_key_from_typeid(self.type_table, arg_types[0]))
							subst[subj] = key
							tp_name = type_param_names.get(subj)
							if tp_name is not None:
								subst[tp_name] = key
							subj_key = key
						if subj_key is None and isinstance(subj, TypeKey):
							subj_key = subj
						if not isinstance(subj_key, TypeKey) or subj_key.name != "fn":
							continue
						fn_args = list(subj_key.args)
						if not fn_args:
							continue
						trait_args = list(getattr(atom.trait, "args", []) or [])
						if len(trait_args) != expected or len(fn_args) != expected:
							continue
						for targ, farg in zip(trait_args, fn_args):
							name = getattr(targ, "name", None)
							if name and name in type_param_map:
								tp_id = type_param_map[name]
								subst[tp_id] = farg
								subst[name] = farg
				if world is None:
					continue
				if req is not None:
					facts = _extract_conjunctive_facts(req)
					if facts:
						all_fn = True
						for atom in facts:
							if not isinstance(atom, parser_ast.TraitIs):
								all_fn = False
								break
							trait_name = getattr(atom.trait, "name", None)
							exp = _fn_trait_expected(trait_name) if trait_name is not None else None
							if exp is None:
								all_fn = False
								break
							expected, trait_can_throw = exp
							subj = atom.subject
							subj_key = subst.get(subj)
							if subj_key is None and isinstance(subj, parser_ast.TypeNameRef):
								subj_key = subst.get(subj.name)
							if subj_key is None and isinstance(subj, TypeKey):
								subj_key = subj
							if subj_key is None and len(arg_types) == 1:
								ad = self.type_table.get(arg_types[0])
								if ad.kind is TypeKind.FUNCTION:
									if ad.can_throw() != trait_can_throw:
										all_fn = False
										break
									if len(ad.param_types) != expected:
										all_fn = False
										break
									continue
							if subj_key is None and len(arg_types) == 1:
								subj_key = _normalize_type_key(type_key_from_typeid(self.type_table, arg_types[0]))
							if not isinstance(subj_key, TypeKey) or subj_key.name != "fn":
								all_fn = False
								break
							if subj_key.fn_throws is True and not trait_can_throw:
								all_fn = False
								break
							if subj_key.fn_throws is False and trait_can_throw:
								all_fn = False
								break
							if len(subj_key.args) != expected:
								all_fn = False
								break
						if all_fn:
							applicable.append((decl, sig_inst, inst_subst))
							scope_map = _param_scope_map(sig)
							require_info[cand_key] = (
								req,
								subst,
								fn_id.module or current_module_name,
								scope_map,
							)
							continue
				env = TraitEnv(
					default_module=fn_id.module or current_module_name,
					default_package=default_package,
					module_packages=module_packages or {},
					assumed_true=set(fn_require_assumed),
					type_table=self.type_table,
				)
				res = prove_expr(world, env, subst, req)
				if res.status is not ProofStatus.PROVED and len(arg_types) == 1:
					ad = self.type_table.get(arg_types[0])
					if ad.kind is TypeKind.FUNCTION:
						facts = _extract_conjunctive_facts(req)
						ok_fn = True
						for atom in facts:
							if not isinstance(atom, parser_ast.TraitIs):
								ok_fn = False
								break
							trait_name = getattr(atom.trait, "name", None)
							exp = _fn_trait_expected(trait_name) if trait_name is not None else None
							if exp is None:
								ok_fn = False
								break
							expected, trait_can_throw = exp
							if ad.can_throw() != trait_can_throw:
								ok_fn = False
								break
							if len(ad.param_types) != expected:
								ok_fn = False
								break
						if ok_fn:
							applicable.append((decl, sig_inst, inst_subst))
							scope_map = _param_scope_map(sig)
							require_info[cand_key] = (
								req,
								subst,
								fn_id.module or current_module_name,
								scope_map,
							)
							continue
				if res.status is ProofStatus.PROVED:
					applicable.append((decl, sig_inst, inst_subst))
					scope_map = _param_scope_map(sig)
					require_info[cand_key] = (
						req,
						subst,
						fn_id.module or current_module_name,
						scope_map,
					)
				else:
					saw_require_failed = True
					origin = ObligationOrigin(
						kind=ObligationOriginKind.CALLEE_REQUIRE,
						label=f"function '{name}'",
						span=Span.from_loc(getattr(req, "loc", None)),
					)
					failure = _require_failure(
						req_expr=req,
						subst=subst,
						origin=origin,
						span=call_type_args_span or Span(),
						env=env,
						world=world,
						result=res,
					)
					if failure is not None:
						require_failures.append(failure)
			if not applicable:
				if saw_require_failed:
					failure = _pick_best_failure(require_failures)
					if failure is not None:
						raise ResolutionError(
							_format_failure_message(failure),
							code=_failure_code(failure),
							span=call_type_args_span,
							notes=_requirement_notes(failure),
						)
					raise ResolutionError(f"trait requirements not met for function '{name}'")
				raise ResolutionError(f"no matching overload for function '{name}' with args {arg_types}")
			applicable = _dedupe_by_key(applicable, lambda item: _candidate_key_for_decl(item[0]))
			if len(applicable) == 1:
				return applicable[0][0], applicable[0][1], applicable[0][2]
			winners = _pick_most_specific_items(
				applicable,
				lambda item: _candidate_key_for_decl(item[0]),
				require_info,
			)
			if len(winners) != 1:
				raise ResolutionError(f"ambiguous call to function '{name}' with args {arg_types}")
			return winners[0]

		params: List[ParamId] = []
		param_bindings: List[int] = []
		locals: List[LocalId] = []
		param_binding_ids: dict[str, int] = {}
		if param_types:
			wanted = set(param_types.keys())
			if preseed_scope_bindings:
				for name in wanted:
					bid = preseed_scope_bindings.get(name)
					if bid is not None:
						param_binding_ids[name] = bid
			else:
				def _scan_param_binds(obj: object) -> None:
					if isinstance(obj, H.HVar) and obj.binding_id is not None and obj.name in wanted:
						prev = param_binding_ids.get(obj.name)
						if prev is None or obj.binding_id < prev:
							param_binding_ids[obj.name] = obj.binding_id
					if isinstance(obj, H.HNode) or (is_dataclass(obj) and obj.__class__.__module__.startswith("lang.driftc.stage1")):
						if is_dataclass(obj):
							for f in fields(obj):
								_scan_param_binds(getattr(obj, f.name))
						else:
							for v in obj.__dict__.values():
								_scan_param_binds(v)
					elif isinstance(obj, list):
						for v in obj:
							_scan_param_binds(v)
					elif isinstance(obj, dict):
						for v in obj.values():
							_scan_param_binds(v)

				_scan_param_binds(body)
		self_binding_id = param_binding_ids.get("self")
		self_param_allows_mut_borrow = False
		if self_binding_id is not None and sig is not None:
			self_param_allows_mut_borrow = _self_mode_from_sig(sig) is SelfMode.SELF_BY_REF_MUT

		# Seed parameters if provided.
		for pname, pty in (param_types or {}).items():
			pid = param_binding_ids.get(pname) or self._alloc_param_id()
			params.append(pid)
			param_bindings.append(pid)
			scope_env[-1][pname] = pty
			scope_bindings[-1][pname] = pid
			binding_types[pid] = pty
			binding_names[pid] = pname
			binding_mutable[pid] = bool(param_mutable.get(pname, False)) if param_mutable else False
			if pname == "self" and self_param_allows_mut_borrow:
				binding_mutable[pid] = True
			binding_place_kind[pid] = PlaceKind.PARAM
			if pty is not None and self.type_table.get(pty).kind is TypeKind.REF:
				ref_origin_param[pid] = pid
				binding_param_ref_mut[pid] = bool(self.type_table.get(pty).ref_mut)
			elif pname == "self" and self_param_allows_mut_borrow:
				binding_param_ref_mut[pid] = True

		def record_expr(expr: H.HExpr, ty: TypeId) -> TypeId:
			if drift_debug.enabled("local_types_trace") and isinstance(expr, H.HLiteralBool) and ty != self._bool:
				td = self.type_table.get(ty)
				fn = fn_id
				span = getattr(expr, "loc", Span())
				print(f"[drift:debug][local_types_trace] fn={fn} record_expr=HLiteralBool node_id={expr.node_id} ty={ty}:{td.kind.name}:{td.name} span={span}", file=sys.stderr)
			expr_types[expr.node_id] = ty
			if self.type_table is not None and self.type_table.type_provenance_enabled():
				span = getattr(expr, "loc", None)
				self.type_table.record_type_provenance(
					ty,
					phase="typecheck",
					kind="expr",
					span=span,
					note=type(expr).__name__,
				)
			return ty

		def record_iface_coercion(expr: H.HExpr, target_iface: TypeId) -> None:
			iface_coercions[expr.node_id] = target_iface

		# Patch B Sites 5 & 6: when an iface_coercion target is a concrete
		# `Callback*` / `CallbackThrow*`, route through the canonical
		# `_implicit_callback_wrap` helper instead of recording a raw
		# coercion (which would lower as `M.ConstructIfaceValue` over a
		# lambda value, breaking codegen since lambdas don't implement
		# the iface). Returns a `CallbackWrapResult` — see the dataclass
		# in `call_resolver.py` for the WRAPPED / REJECTED / SKIP
		# contract. Caller MUST distinguish all three; in particular,
		# REJECTED forbids the raw `record_iface_coercion` fallback.
		# Built lazily so the adapter binds `type_expr` etc. after they
		# are defined later in this function.
		def _try_callback_wrap_for_iface_slot(arg: object, have_ty: TypeId | None, want_ty: TypeId):
			class _Ctx:
				pass
			c = _Ctx()
			c.type_table = self.type_table
			c.unknown_ty = self._unknown
			c.type_expr = type_expr
			c.alloc_callsite_id = _alloc_callsite_id
			c.alloc_node_id = _assign_node_id
			# Borrowed-capture rejection at this site is owned by
			# `type_expr(HLambda, expected_type=Callback*)` — see
			# `_expected_function_shape` and the captureless-coercion
			# branch below. The helper still returns REJECTED when it
			# detects an already-poisoned arg so the caller does not
			# record a raw iface_coercion over it.
			c.diagnostics = diagnostics
			c.tc_diag = _tc_diag
			return _try_wrap_arg_for_callback_field(c, arg=arg, have_ty=have_ty, want_ty=want_ty)

		def iface_assignable(src: TypeId, dst: TypeId) -> bool:
			if src == dst:
				return True
			src_def = self.type_table.get(src)
			dst_def = self.type_table.get(dst)
			if src_def.kind is not TypeKind.INTERFACE or dst_def.kind is not TypeKind.INTERFACE:
				return False
			try:
				dst_inst = self.type_table.get_interface_instance(dst)
				dst_base = dst_inst.base_id if dst_inst is not None else dst
				view_map = self.type_table.interface_instance_view_map(src)
				return view_map.get(dst_base) == dst
			except Exception:
				return False

		def record_call_info(
			expr: H.HCall,
			*,
			param_types: List[TypeId],
			return_type: TypeId,
			can_throw: bool,
			target: CallTarget,
			declared_terminal_throws: bool = False,
		) -> None:
			if target.kind is CallTargetKind.DIRECT and target.symbol is not None and signatures_by_id is not None and getattr(expr, "loc", None) is not None:
				sig = signatures_by_id.get(target.symbol)
				if sig is not None and bool(getattr(sig, "declared_unsafe", False)):
					_is_extern_c_call = bool(getattr(sig, "is_extern_c", False))
					if not unsafe_allowed_module and not allow_unsafe_without_block_local:
						if _is_extern_c_call:
							# For extern C: if the call IS in an unsafe block,
							# the only problem is the missing flag — don't also
							# claim the call lacks an unsafe block.
							if unsafe_context:
								diagnostics.append(_tc_diag(message="unsafe block requires --allow-unsafe", severity="error", span=getattr(expr, "loc", Span())))
							else:
								diagnostics.append(_tc_diag(message="call to extern C function requires unsafe block", severity="error", span=getattr(expr, "loc", Span())))
						else:
							diagnostics.append(_tc_diag(message="unsafe call requires --allow-unsafe", severity="error", span=getattr(expr, "loc", Span())))
					elif not unsafe_context and not allow_unsafe_without_block_local:
						if _is_extern_c_call:
							diagnostics.append(_tc_diag(message="call to extern C function requires unsafe block", severity="error", span=getattr(expr, "loc", Span())))
						else:
							diagnostics.append(_tc_diag(message="unsafe call requires unsafe block", severity="error", span=getattr(expr, "loc", Span())))
			if target.kind is CallTargetKind.DIRECT and target.symbol is not None and signatures_by_id is not None:
				sig = signatures_by_id.get(target.symbol)
				if _force_boundary_can_throw(sig, target.symbol):
					can_throw = True
			info = CallInfo(
				target=target,
				sig=CallSig(param_types=tuple(param_types), user_ret_type=return_type, can_throw=bool(can_throw), declared_terminal_throws=declared_terminal_throws),
			)
			if self.type_table is not None and self.type_table.type_provenance_enabled():
				span = getattr(expr, "loc", None)
				note = f"callsite:{getattr(expr, 'callsite_id', None)}"
				for tid in param_types:
					self.type_table.record_type_provenance(
						tid,
						phase="typecheck",
						kind="call_param",
						span=span,
						note=note,
					)
				self.type_table.record_type_provenance(
					return_type,
					phase="typecheck",
					kind="call_ret",
					span=span,
					note=note,
				)
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				csid = _record_call_info(expr, info)
				if drift_debug.enabled("callsite"):
					try:
						fn = getattr(expr, "fn", None)
						fn_kind = type(fn).__name__ if fn is not None else None
						fn_name = getattr(fn, "name", None) or getattr(fn, "member", None)
						print(f"[callsite] record CallInfo fn={function_symbol(fn_id)} csid={csid} fn_kind={fn_kind} name={fn_name}", file=sys.stderr)
					except Exception:
						pass
			elif callable_registry is not None:
				diagnostics.append(
					_tc_diag(
						message="internal: missing callsite_id on call node",
						severity="error",
						span=getattr(expr, "span", Span()),
					)
				)

		def record_call_resolution(expr: H.HCall, resolution: CallableDecl | MethodResolution) -> None:
			call_resolutions[expr.node_id] = resolution

		def record_invoke_call_info(
			expr: "H.HInvoke",
			*,
			param_types: List[TypeId],
			return_type: TypeId,
			can_throw: bool,
		) -> None:
			info = CallInfo(
				target=CallTarget.indirect(expr.callee.node_id),
				sig=CallSig(
					param_types=tuple(param_types),
					user_ret_type=return_type,
					can_throw=bool(can_throw),
					includes_callee=False,
				),
			)
			if self.type_table is not None and self.type_table.type_provenance_enabled():
				span = getattr(expr, "loc", None)
				note = f"callsite:{getattr(expr, 'callsite_id', None)}"
				for tid in param_types:
					self.type_table.record_type_provenance(
						tid,
						phase="typecheck",
						kind="call_param",
						span=span,
						note=note,
					)
				self.type_table.record_type_provenance(
					return_type,
					phase="typecheck",
					kind="call_ret",
					span=span,
					note=note,
				)
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				_record_call_info(expr, info)
			elif callable_registry is not None:
				diagnostics.append(
					_tc_diag(
						message="internal: missing callsite_id on invoke node",
						severity="error",
						span=getattr(expr, "span", Span()),
					)
				)

		def record_method_call_info(
			expr: H.HMethodCall,
			*,
			param_types: List[TypeId],
			return_type: TypeId,
			can_throw: bool,
			target: FunctionId,
		) -> None:
			info = CallInfo(
				target=CallTarget.direct(target),
				sig=CallSig(param_types=tuple(param_types), user_ret_type=return_type, can_throw=bool(can_throw)),
			)
			if self.type_table is not None and self.type_table.type_provenance_enabled():
				span = getattr(expr, "loc", None)
				note = f"callsite:{getattr(expr, 'callsite_id', None)}"
				for tid in param_types:
					self.type_table.record_type_provenance(
						tid,
						phase="typecheck",
						kind="call_param",
						span=span,
						note=note,
					)
				self.type_table.record_type_provenance(
					return_type,
					phase="typecheck",
					kind="call_ret",
					span=span,
					note=note,
				)
			csid = getattr(expr, "callsite_id", None)
			if isinstance(csid, int):
				prev_csid = csid
				csid = _record_call_info(expr, info)
				if prev_csid != csid:
					inst_prev = instantiations_by_callsite_id.pop(prev_csid, None)
					if inst_prev is not None:
						instantiations_by_callsite_id[csid] = inst_prev
				inst = instantiations_by_callsite_id.get(csid)
				if inst is None and isinstance(expr, H.HCall):
					callee_expr = getattr(expr, "fn", None)
					if isinstance(callee_expr, H.HTypeApply):
						type_app_node_id = getattr(callee_expr, "node_id", None)
						if isinstance(type_app_node_id, int):
							inst_node = instantiations_by_node_id.get(type_app_node_id)
							if inst_node is not None:
								instantiations_by_callsite_id[csid] = inst_node
								inst = inst_node
				if inst is None and isinstance(expr, H.HCall):
					explicit_type_args = list(getattr(expr, "type_args", None) or [])
					if explicit_type_args:
						target_sig = signatures_by_id.get(target) if signatures_by_id is not None else None
						target_tparams = list(getattr(target_sig, "type_params", None) or [])
						if target_tparams and len(explicit_type_args) == len(target_tparams):
							type_arg_ids = [
								resolve_opaque_type(
									targ,
									self.type_table,
									module_id=current_module_name,
									type_params=type_param_map,
								)
								for targ in explicit_type_args
							]
							if not any(self.type_table.has_typevar(tid) for tid in type_arg_ids):
								record_instantiation(
									callsite_id=csid,
									target_fn_id=target,
									impl_args=tuple(),
									fn_args=tuple(type_arg_ids),
									callsite_span=getattr(expr, "loc", None),
								)
								inst = instantiations_by_callsite_id.get(csid)
				if inst is not None:
					key = getattr(inst, "target_key", None)
					type_args = tuple(getattr(inst, "type_args", ()) or ())
					if isinstance(key, FunctionKey) and type_args:
						# Arc runtime boundary: if the callee is an
						# `@intrinsic` generic, keep the CallInfo at
						# `CallTarget.intrinsic(...)` (set just above
						# by the method-resolution intrinsic rewrite)
						# — do NOT overwrite it with the inst
						# Direct-target of a bodyless template.
						# Monomorphization is skipped for intrinsic
						# templates in driftc.py; the MIR lowering
						# redirects intrinsic calls to the matching
						# `_arc_*_impl<T>` helper.
						_rec_intrinsic = False
						if signatures_by_id is not None:
							for _fid, _sig in signatures_by_id.items():
								if _fid.module != key.module_path:
									continue
								if _fid.name != key.name:
									continue
								if bool(getattr(_sig, "is_intrinsic", False)):
									_rec_intrinsic = True
								break
						if not _rec_intrinsic:
							inst_key = build_instantiation_key(
								key,
								type_args,
								type_table=self.type_table,
								can_throw=bool(info.sig.can_throw),
							)
							inst_name = f"{key.name}__inst__{instantiation_key_hash(inst_key)}"
							inst_fn_id = FunctionId(module=key.module_path, name=inst_name, ordinal=0)
							call_info_by_callsite_id[csid] = CallInfo(
								target=CallTarget.direct(inst_fn_id),
								sig=info.sig,
							)
			elif callable_registry is not None:
				diagnostics.append(
					_tc_diag(
						message="internal: missing callsite_id on method call node",
						severity="error",
						span=getattr(expr, "span", Span()),
					)
				)

		def record_instantiation(
			*,
			callsite_id: int | None,
			node_id: int | None = None,
			target_fn_id: FunctionId | None,
			impl_args: Tuple[TypeId, ...],
			fn_args: Tuple[TypeId, ...],
			callsite_span: Span | None = None,
		) -> None:
			if target_fn_id is None or function_keys_by_fn_id is None:
				return
			key = function_keys_by_fn_id.get(target_fn_id)
			if key is None:
				return
			type_args = tuple(impl_args) + tuple(fn_args)
			if not type_args:
				return
			if isinstance(callsite_id, int):
				instantiations_by_callsite_id[callsite_id] = CallInstantiation(target_key=key, type_args=type_args)
				info = call_info_by_callsite_id.get(callsite_id)
				if info is not None:
					inst_can_throw = info.sig.can_throw
					if signatures_by_id is not None:
						sig = signatures_by_id.get(target_fn_id)
						if sig is not None and sig.declared_can_throw is not None:
							inst_can_throw = bool(sig.declared_can_throw)
					inst_key = build_instantiation_key(
						key,
						type_args,
						type_table=self.type_table,
						can_throw=bool(inst_can_throw),
					)
					inst_name = f"{key.name}__inst__{instantiation_key_hash(inst_key)}"
					inst_fn_id = FunctionId(module=key.module_path, name=inst_name, ordinal=0)
					_write_call_info_respecting_intrinsic(
						callsite_id,
						CallInfo(
							target=CallTarget.direct(inst_fn_id),
							sig=CallSig(
								param_types=info.sig.param_types,
								user_ret_type=info.sig.user_ret_type,
								can_throw=bool(inst_can_throw),
								includes_callee=info.sig.includes_callee,
								declared_terminal_throws=info.sig.declared_terminal_throws,
							),
						),
						template_key=key,
					)
			elif isinstance(node_id, int):
				instantiations_by_node_id[node_id] = CallInstantiation(target_key=key, type_args=type_args)
			elif callable_registry is not None:
				diagnostics.append(
					_tc_diag(
						message="internal: missing callsite_id on instantiation call node",
						severity="error",
						span=callsite_span or Span(),
					)
				)

		_intrinsic_method_fn_ids: dict[str, FunctionId] = {}

		def _intrinsic_method_fn_id(method_name: str) -> FunctionId:
			fn_id = _intrinsic_method_fn_ids.get(method_name)
			if fn_id is None:
				fn_id = FunctionId(module="lang.__intrinsic", name=f"__method::{method_name}", ordinal=0)
				_intrinsic_method_fn_ids[method_name] = fn_id
			return fn_id

		# (method_wrapper_by_target removed — Option B has no wrappers)

		# Precompute constructor-name visibility for diagnostics.
		#
		# MVP constructor resolution rule:
		# - Constructors are unqualified identifiers.
		# - Constructor calls in expression position require an *expected variant type*.
		# - Without an expected type, the compiler diagnoses instead of guessing.
		ctor_to_variant_bases: dict[str, list[TypeId]] = {}
		visible_ctor_module_ids = set(visible_modules or ())
		visible_ctor_module_ids.add(current_module)
		if prelude_module_id is not None:
			visible_ctor_module_ids.add(prelude_module_id)

		def _ctor_module_visible(module_name: str | None) -> bool:
			if module_name is None:
				return False
			if visibility_provenance:
				mod_id = module_ids_by_name.get(module_name)
				if mod_id is None:
					return False
				return mod_id in visible_ctor_module_ids
			# Best-effort fallback when no provenance is available: current module only.
			return module_name == current_module_name

		def _ensure_field_visible(struct_id: TypeId, field_name: str, span: Span) -> bool:
			td = self.type_table.get(struct_id)
			def_mod = td.module_id
			if def_mod is None or def_mod == current_module_name:
				return True
			info = self.type_table.struct_field_info(struct_id, field_name)
			if info is None:
				return True
			_is_pub = info[2]
			if _is_pub:
				return True
			diagnostics.append(
				_tc_diag(
					message=f"field '{field_name}' is private",
					severity="error",
					span=span,
					code="E-PRIVATE-FIELD",
				)
			)
			return False

		items = list(getattr(self.type_table, "variant_schemas", {}).items())
		items.sort(key=lambda kv: (kv[1].module_id, kv[1].name))
		for base_id, schema in items:
			if not _ctor_module_visible(schema.module_id):
				continue
			for arm in schema.arms:
				ctor_to_variant_bases.setdefault(arm.name, []).append(base_id)

		def type_expr(
			expr: H.HExpr,
			*,
			allow_exception_init: bool = False,
			used_as_value: bool = True,
			expected_type: TypeId | None = None,
		) -> TypeId:
			nonlocal return_type
			nonlocal catch_depth
			nonlocal unsafe_context
			# Auto-try contract state — must save/restore across lambda
			# boundaries so a `throws` outer fn does not leak its auto-try
			# context into a nothrow lambda body (Bug A — 0.31.38).
			nonlocal fn_declared_throws
			nonlocal try_block_depth
			def _resolve_struct_field_type(struct_id: TypeId, field_name: str) -> tuple[int, TypeId] | None:
				info = self.type_table.struct_field(struct_id, field_name)
				if info is not None:
					idx, field_ty = info
					field_def = self.type_table.get(field_ty)
					if field_def.kind is not TypeKind.UNKNOWN:
						return info
				schema = self.type_table.struct_bases.get(struct_id)
				if schema is None or not schema.type_params:
					return info
				type_args: list[TypeId] = []
				for tp_name in schema.type_params:
					tp_id = type_param_map.get(tp_name)
					if tp_id is None:
						type_args.append(self._unknown)
					else:
						type_args.append(self.type_table.ensure_typevar(tp_id, name=tp_name))
				if sig is not None and sig.impl_target_type_id == struct_id and sig.impl_target_type_args and len(sig.impl_target_type_args) == len(schema.type_params):
					for idx, arg in enumerate(sig.impl_target_type_args):
						if idx < len(type_args) and type_args[idx] == self._unknown:
							type_args[idx] = arg
				inst_id = self.type_table.ensure_struct_template(struct_id, type_args) if any(self.type_table.has_typevar(t) for t in type_args) else self.type_table.ensure_struct_instantiated(struct_id, type_args)
				return self.type_table.struct_field(inst_id, field_name)

			def _expr_reads_through_ref_projection(node: H.HExpr) -> bool:
				if isinstance(node, H.HField):
					sub_ty = type_expr(node.subject, used_as_value=False)
					sub_td = self.type_table.get(sub_ty) if sub_ty is not None else None
					if sub_td is not None and sub_td.kind is TypeKind.REF:
						return True
					return _expr_reads_through_ref_projection(node.subject)
				if isinstance(node, H.HIndex):
					sub_ty = type_expr(node.subject, used_as_value=False)
					sub_td = self.type_table.get(sub_ty) if sub_ty is not None else None
					if sub_td is not None and sub_td.kind is TypeKind.REF:
						return True
					return _expr_reads_through_ref_projection(node.subject)
				if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
					base_ty = type_expr(node.base, used_as_value=False)
					base_td = self.type_table.get(base_ty) if base_ty is not None else None
					if base_td is not None and base_td.kind is TypeKind.REF and len(getattr(node, "projections", []) or []) > 0:
						return True
				return False

			def _best_effort_span_for_expr(node: H.HExpr) -> Span:
				span = Span.from_loc(getattr(node, "loc", None))
				if span.line is not None:
					return span
				if isinstance(node, H.HField):
					return _best_effort_span_for_expr(node.subject)
				if isinstance(node, H.HIndex):
					sub_span = _best_effort_span_for_expr(node.subject)
					if sub_span.line is not None:
						return sub_span
					return Span.from_loc(getattr(node.index, "loc", None))
				if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
					base_span = _best_effort_span_for_expr(node.base)
					if base_span.line is not None:
						return base_span
				return span
			# Literals.
			if isinstance(expr, H.HLiteralInt):
				if expected_type == self._uint:
					return record_expr(expr, self._uint)
				if expected_type == self._uint64:
					return record_expr(expr, self._uint64)
				if expected_type == self.type_table.ensure_byte():
					return record_expr(expr, expected_type)
				return record_expr(expr, self._int)
			if hasattr(H, "HLiteralUint") and isinstance(expr, getattr(H, "HLiteralUint")):
				_uint_max = self.type_table.uint_max
				_uint_bits = self.type_table.word_bits
				if expr.value < 0 or expr.value > _uint_max:
					diagnostics.append(_tc_diag(message=f"Uint literal {expr.value}u is out of range [0, 2^{_uint_bits}-1]", code="E-UINT-OVERFLOW", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, self._uint)
			if hasattr(H, "HLiteralUint64") and isinstance(expr, getattr(H, "HLiteralUint64")):
				if expr.value < 0 or expr.value > 0xFFFFFFFFFFFFFFFF:
					diagnostics.append(_tc_diag(message=f"Uint64 literal {expr.value}u64 is out of range [0, 2^64-1]", code="E-UINT64-OVERFLOW", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, self._uint64)
			if hasattr(H, "HLiteralFloat") and isinstance(expr, getattr(H, "HLiteralFloat")):
				return record_expr(expr, self._float)
			if isinstance(expr, H.HLiteralBool):
				return record_expr(expr, self._bool)
			if isinstance(expr, H.HTraitExpr):
				diagnostics.append(
					_tc_diag(
						message="trait propositions are only allowed in require clauses or if guards",
						code="E-TRAIT-PROP-VALUE-POS",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)
			if isinstance(expr, H.HLiteralString):
				return record_expr(expr, self._string)
			if isinstance(expr, H.HFString):
				# f-strings are sugar that ultimately produce a String.
				#
				# MVP rules (from spec-change request):
				# - Each hole expression must be one of {Bool, Int, Uint, Float, String}.
				# - `:spec` is supported syntactically, but only the empty spec is
				#   accepted for now (future work will validate a richer subset).
				for hole in expr.holes:
					hole_ty = type_expr(hole.expr)
					if hole.spec:
						diagnostics.append(
							_tc_diag(
								message="E-FSTR-BAD-SPEC: non-empty :spec is not supported yet (MVP: empty only)",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
					if hole_ty not in (self._bool, self._int, self._uint, self._float, self._string):
						pretty = self.type_table.get(hole_ty).name if hole_ty is not None else "Unknown"
						diagnostics.append(
							_tc_diag(
								message=f"E-FSTR-UNSUPPORTED-TYPE: f-string hole value is not formattable in v1 (have {pretty})",
								severity="error",
								span=getattr(hole, "loc", Span()),
							)
						)
				return record_expr(expr, self._string)

			if isinstance(expr, H.HCast):
				target_ty: TypeId | None = None
				try:
					_reject_fixed_width_type_expr(
						expr.target_type_expr,
						getattr(expr.target_type_expr, "module_id", None) or current_module_name,
						getattr(expr, "loc", Span()),
					)
					target_ty = resolve_opaque_type(expr.target_type_expr, self.type_table, module_id=current_module_name)
				except Exception:
					target_ty = None
				if target_ty is None:
					diagnostics.append(
						_tc_diag(
							message="cast<T>(...) has an invalid target type",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				target_def = self.type_table.get(target_ty)
				def _is_uint_scalar_type(tid: TypeId | None) -> bool:
					if tid is None:
						return False
					td = self.type_table.get(tid)
					return td.kind is TypeKind.SCALAR and td.name == "Uint"
				if target_def.kind is TypeKind.FUNCTION:
					expected_fn = _expected_function_shape(target_ty)
					if expected_fn is None:
						return record_expr(expr, self._unknown)
					if isinstance(expr.value, H.HVar) and expr.value.binding_id is None:
						name = expr.value.name
						is_bound = any(name in scope for scope in scope_env)
						is_const = False
						if not is_bound:
							const_mod = expr.value.module_id or current_module_name
							if const_mod is not None:
								is_const = self.type_table.lookup_const(f"{const_mod}::{name}") is not None
						if not is_bound and not is_const:
							resolution = _resolve_function_reference_value(
								name=name,
								module_name=expr.value.module_id,
								expected_type=target_ty,
								span=getattr(expr, "loc", Span()),
								diag_mode="cast",
								allow_thunk=False,
							)
							if resolution is not None:
								if resolution.fn_ref is None:
									return record_expr(expr, self._unknown)
								fnptr_consts_by_node_id[expr.node_id] = (resolution.fn_ref, resolution.call_sig)
								return record_expr(expr, target_ty)
					inner_ty = type_expr(expr.value, expected_type=None)
					if inner_ty != target_ty:
						inner_pretty = self._pretty_type_name(inner_ty, current_module=current_module_name) if inner_ty is not None else "Unknown"
						target_pretty = self._pretty_type_name(target_ty, current_module=current_module_name)
						diagnostics.append(
							_tc_diag(
								message=f"cannot cast expression of type {inner_pretty} to {target_pretty}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					return record_expr(expr, target_ty)
				if target_def.kind is TypeKind.RAW_PTR:
					inner_ty = type_expr(expr.value, expected_type=None)
					if inner_ty is None:
						return record_expr(expr, self._unknown)
					inner_def = self.type_table.get(inner_ty)
					if inner_def.kind is TypeKind.RAW_PTR or _is_uint_scalar_type(inner_ty):
						return record_expr(expr, target_ty)
					inner_pretty = self._pretty_type_name(inner_ty, current_module=current_module_name)
					target_pretty = self._pretty_type_name(target_ty, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=f"cannot cast expression of type {inner_pretty} to {target_pretty}",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if target_def.kind is TypeKind.SCALAR and target_def.name in ("Int", "Uint", "Uint64", "Int32", "Uint32", "Byte", "Bool"):
					inner_ty = type_expr(expr.value, expected_type=None)
					if inner_ty is None:
						return record_expr(expr, self._unknown)
					inner_def = self.type_table.get(inner_ty)
					if target_def.name == "Uint" and inner_def.kind is TypeKind.RAW_PTR:
						return record_expr(expr, target_ty)
					if inner_def.kind is TypeKind.SCALAR and inner_def.name in ("Int", "Uint", "Uint64", "Int32", "Uint32", "Byte", "Bool"):
						return record_expr(expr, target_ty)
					inner_pretty = self._pretty_type_name(inner_ty, current_module=current_module_name)
					target_pretty = self._pretty_type_name(target_ty, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=f"cannot cast expression of type {inner_pretty} to {target_pretty}",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				pretty = self._pretty_type_name(target_ty, current_module=current_module_name)
				diagnostics.append(
					_tc_diag(
						message=(
							"cast<T>(...) is only supported for function or numeric scalar types in this build "
							f"(requested T = {pretty})"
						),
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HFnPtrConst):
				call_sig = getattr(expr, "call_sig", None)
				if call_sig is None:
					diagnostics.append(
						_tc_diag(
							message="internal: function pointer constant missing call signature",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				fn_ty = self.type_table.ensure_function(
					list(call_sig.param_types),
					call_sig.user_ret_type,
					can_throw=bool(call_sig.can_throw),
				)
				return record_expr(expr, fn_ty)

			# Names and bindings.
			if isinstance(expr, H.HVar):
				def _scope_lookup_binding_id(var_name: str) -> Optional[int]:
					for scope in reversed(scope_bindings):
						if var_name not in scope:
							continue
						cand_id = scope[var_name]
						cand_name = binding_names.get(cand_id)
						if cand_name is not None and cand_name != var_name:
							continue
						return cand_id
					return None
				if expr.binding_id is not None:
					bound_name = binding_names.get(expr.binding_id)
					if bound_name is not None and bound_name != expr.name:
						expr.binding_id = None
				if expr.module_id is None:
					scoped_id = _scope_lookup_binding_id(expr.name)
					if scoped_id is not None and expr.binding_id != scoped_id:
						expr.binding_id = scoped_id
				if expr.binding_id is not None:
					bound = binding_types.get(expr.binding_id)
					if bound is None or self.type_table.get(bound).kind is TypeKind.UNKNOWN:
						if expr.module_id is None:
							candidate = _scope_lookup_binding_id(expr.name)
							if candidate is not None:
								cand_ty = binding_types.get(candidate)
								if cand_ty is not None and self.type_table.get(cand_ty).kind is not TypeKind.UNKNOWN:
									expr.binding_id = candidate
									bound = cand_ty
					if bound is not None:
						cap_kind = _explicit_capture_kind(expr.binding_id)
						if cap_kind in ("ref", "ref_mut"):
							if used_as_value:
								bound = bound
							else:
								bound = self.type_table.ensure_ref_mut(bound) if cap_kind == "ref_mut" else self.type_table.ensure_ref(bound)
						binding_for_var[expr.node_id] = expr.binding_id
						_require_copy_value(bound, span=getattr(expr, "loc", Span()), name=expr.name, used_as_value=used_as_value)
						return record_expr(expr, bound)
				# Module-scoped compile-time constants.
				#
				# Consts live outside local scope bindings. We resolve them here so
				# later stages can:
				# - type-check `CONST` like a literal of its declared type,
				# - lower it to an immediate MIR/LLVM constant at each use site.
				#
				# Resolution order:
				#   1) local/param bindings (lexical scopes),
				#   2) module-qualified const symbols (`mod::NAME`) present in the TypeTable,
				#   3) unqualified const names resolved within the current module id.
				if expr.binding_id is None:
					const_mod = expr.module_id if expr.module_id is not None else current_module_name
					if const_mod is not None:
						cv = self.type_table.lookup_const(f"{const_mod}::{expr.name}")
						if cv is not None:
							ty_id, _val = cv
							_require_copy_value(ty_id, span=getattr(expr, "loc", Span()), name=expr.name, used_as_value=used_as_value)
							return record_expr(expr, ty_id)
				if expr.module_id is None and expr.binding_id is None:
					scoped_id = _scope_lookup_binding_id(expr.name)
					if scoped_id is not None:
						expr.binding_id = scoped_id
				if expr.module_id is None:
					for scope in reversed(scope_env):
						if expr.name in scope:
							if expr.binding_id is not None:
								binding_for_var[expr.node_id] = expr.binding_id
							ty_id = scope[expr.name]
							# Local consts re-materialize at each use site; skip Copy check.
							if expr.binding_id is None or int(expr.binding_id) not in local_const_binding_ids:
								_require_copy_value(ty_id, span=getattr(expr, "loc", Span()), name=expr.name, used_as_value=used_as_value)
							return record_expr(expr, ty_id)
				# Function reference in value position (typed context preferred).
				resolution = _resolve_function_reference_value(
					name=expr.name,
					module_name=expr.module_id,
					expected_type=expected_type,
					span=getattr(expr, "loc", Span()),
					diag_mode="value",
					allow_thunk=True,
				)
				if resolution is not None:
					if resolution.fn_ref is None:
						return record_expr(expr, self._unknown)
					fnptr_consts_by_node_id[expr.node_id] = (resolution.fn_ref, resolution.call_sig)
					return record_expr(expr, resolution.fn_type)
				if expr.binding_id is None:
					for scope in reversed(scope_bindings):
						if expr.name in scope:
							expr.binding_id = scope[expr.name]
							binding_for_var[expr.node_id] = expr.binding_id
							bid_ty = binding_types.get(expr.binding_id, self._unknown)
							return record_expr(expr, bid_ty)
				if expr.binding_id is None and binding_names:
					for bid, name in binding_names.items():
						if name == expr.name:
							expr.binding_id = bid
							binding_for_var[expr.node_id] = expr.binding_id
							bid_ty = binding_types.get(expr.binding_id, self._unknown)
							return record_expr(expr, bid_ty)
				if expr.binding_id is not None:
					bid_ty = binding_types.get(expr.binding_id)
					if bid_ty is not None:
						binding_for_var[expr.node_id] = expr.binding_id
						return record_expr(expr, bid_ty)
				diagnostics.append(
					_tc_diag(
						message=f"unknown name '{user_facing_binding_name(expr.name)}'",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HLambda):
				if expected_type is None and hasattr(expr, "expected_type_from_require"):
					expected_type = getattr(expr, "expected_type_from_require")
				expected_fn = _expected_function_shape(expected_type) if expected_type is not None else None
				lambda_type_error = False
				allow_capture_invoke = bool(getattr(expr, "allow_capture_invoke", False))
				if expected_fn is not None and len(expr.params) != len(expected_fn[0]):
					pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=(
								f"lambda parameter count does not match expected function type {pretty} "
								f"(expected {len(expected_fn[0])}, got {len(expr.params)})"
							),
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				lambda_ret_type: TypeId | None = None
				if getattr(expr, "ret_type", None) is not None:
					try:
						lambda_ret_type = resolve_opaque_type(expr.ret_type, self.type_table, module_id=current_module_name)
					except Exception:
						lambda_ret_type = None
				if expected_fn is not None:
					exp_params, exp_ret, _exp_throw = expected_fn
					if lambda_ret_type is None and exp_ret != self._unknown:
						lambda_ret_type = exp_ret
					elif lambda_ret_type is not None and exp_ret != self._unknown and lambda_ret_type != exp_ret:
						ret_pretty = self._pretty_type_name(lambda_ret_type, current_module=current_module_name)
						exp_pretty = self._pretty_type_name(exp_ret, current_module=current_module_name)
						diagnostics.append(
							_tc_diag(
								message=f"lambda return type {ret_pretty} does not match expected {exp_pretty}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						lambda_type_error = True
				scope_env.append({})
				scope_bindings.append({})
				if expr.explicit_captures is None:
					for outer_scope in scope_env[:-1]:
						for name, ty in outer_scope.items():
							scope_env[-1].setdefault(name, ty)
					for outer_scope in scope_bindings[:-1]:
						for name, bid in outer_scope.items():
							scope_bindings[-1].setdefault(name, bid)
					res = discover_captures(expr)
					if res.captures:
						outer_by_id: dict[int, str] = {}
						for outer_scope in scope_bindings[:-1]:
							for name, bid in outer_scope.items():
								outer_by_id[int(bid)] = name
						for cap in res.captures:
							bid = int(cap.key.root_local)
							name = outer_by_id.get(bid)
							if name is None:
								continue
							scope_bindings[-1].setdefault(name, bid)
							cap_ty = binding_types.get(bid, self._unknown)
							scope_env[-1].setdefault(name, cap_ty)
				lambda_param_types: list[TypeId] = []
				for param in expr.params:
					if getattr(param, "binding_id", None) is None:
						param.binding_id = self._alloc_param_id()
					param_type: TypeId = self._unknown
					if getattr(param, "type", None) is not None:
						try:
							param_type = resolve_opaque_type(param.type, self.type_table, module_id=current_module_name)
						except Exception:
							param_type = self._unknown
					if expected_fn is not None:
						exp_params, _exp_ret, _exp_throw = expected_fn
						exp_param = exp_params[len(lambda_param_types)]
						if getattr(param, "type", None) is None:
							param_type = exp_param
						elif param_type != exp_param:
							# Dealias both sides: a cross-package type alias
							# (e.g. mylib.Request → mylib.inner.Request) must be
							# transparent in lambda parameter annotations.
							if _dealias_zero_param_type(param_type) != _dealias_zero_param_type(exp_param):
								param_pretty = self._pretty_type_name(param_type, current_module=current_module_name)
								exp_pretty = self._pretty_type_name(exp_param, current_module=current_module_name)
								diagnostics.append(
									_tc_diag(
										message=(
											f"lambda parameter '{param.name}' has type {param_pretty} "
											f"but expected {exp_pretty}"
										),
										severity="error",
										span=getattr(param, "loc", Span()),
									)
								)
								lambda_type_error = True
							else:
								# Alias resolved to the same type — accept and use
								# the expected param so downstream checks are
								# consistent with the enclosing call's type.
								param_type = exp_param
					scope_env[-1][param.name] = param_type
					scope_bindings[-1][param.name] = param.binding_id
					binding_types[param.binding_id] = param_type
					binding_names[param.binding_id] = param.name
					binding_mutable[param.binding_id] = bool(getattr(param, "is_mutable", False))
					binding_place_kind[param.binding_id] = PlaceKind.PARAM
					lambda_param_types.append(param_type)
				capture_kinds: dict[int, str] = {}
				if expr.explicit_captures is not None:
					for cap in expr.explicit_captures:
						root_id = getattr(cap, "binding_id", None)
						if root_id is None and cap.name is not None:
							for scope in reversed(scope_bindings):
								if cap.name in scope:
									root_id = scope[cap.name]
									cap.binding_id = root_id
									break
						if root_id is None:
							continue
						capture_kinds[int(root_id)] = cap.kind
						root_ty = binding_types.get(root_id, self._unknown)
						if root_ty == self._unknown and cap.name is not None:
							for scope in reversed(scope_env):
								if cap.name in scope:
									root_ty = scope[cap.name]
									break
						if root_ty != self._unknown:
							binding_types[root_id] = root_ty
						if drift_debug.enabled("typecheck"):
							import sys
							print(f"[drift:debug] explicit capture '{cap.name}' root_id={root_id} root_ty={root_ty} fn={function_symbol(fn_id)}", file=sys.stderr)
						if cap.kind == "ref_mut":
							cap_ty = self.type_table.ensure_ref_mut(root_ty)
						elif cap.kind == "ref":
							cap_ty = self.type_table.ensure_ref(root_ty)
						else:
							cap_ty = root_ty
						scope_env[-1][cap.name] = cap_ty
						scope_bindings[-1][cap.name] = root_id
						if cap.kind == "share":
							# `captures(share x)` requires `T: Share`.
							# Reject Copy types up-front (they should be
							# captured via `copy`) and types with no
							# registered Share impl.  HIR→MIR carries a
							# defensive assertion that enforces the same
							# precondition; this is the user-facing
							# diagnostic surface.
							#
							# `cap.binding_id` is the user's original local
							# (the `share x` captures the same `x` the user
							# spelled — the Share::share(&x) call evaluates
							# inline at env construction, returning a fresh
							# owner that goes into the env field).  So the
							# diagnostic reads the user-spelled local's type
							# directly off `binding_id` — no auxiliary
							# tracking required.
							# Default to False so the post-conditional
							# `if implements_share and ...` gate at the bottom
							# of this block is well-defined when `root_ty` is
							# unknown (we skip diagnostic + share_value
							# resolution entirely in that case — caller's
							# binding-type resolution failed upstream).
							implements_share = False
							if root_ty == self._unknown:
								pass
							else:
								# `is_share` runs against the trait prover via
								# the query hook installed at
								# `_build_linked_world` time — this is
								# available now (mid-typecheck), before the
								# trait-impl index used by HIR→MIR has been
								# populated.
								try:
									implements_share = bool(self.type_table.is_share(root_ty))
								except Exception:
									implements_share = False
								if not implements_share:
									ty_name = self.type_table.get(root_ty).name or f"typeid={root_ty}"
									is_copy = False
									try:
										is_copy = bool(self.type_table.copy_status(root_ty))
									except Exception:
										is_copy = False
									if is_copy:
										diagnostics.append(
											_tc_diag(
												message=(
													f"E-CAPTURE-SHARE-NOT-SHARE: type "
													f"'{ty_name}' is `Copy`, not `Share`. "
													f"For value-like capture, use "
													f"`captures(copy {cap.name})`. `share` "
													f"is for non-Copy shared-owner types."
												),
												severity="error",
												span=getattr(cap, "span", None) or getattr(cap, "loc", Span()),
											)
										)
									else:
										diagnostics.append(
											_tc_diag(
												message=(
													f"E-CAPTURE-SHARE-NOT-SHARE: type "
													f"'{ty_name}' does not implement "
													f"`std.core.shareable.Share`. To "
													f"transfer ownership, use "
													f"`captures(move {cap.name})`. To enable "
													f"share-capture for '{ty_name}', "
													f"implement `std.core.shareable.Share` "
													f"for it (an inherent `.share()` method "
													f"does NOT satisfy `captures(share x)`)."
												),
												severity="error",
												span=getattr(cap, "span", None) or getattr(cap, "loc", Span()),
											)
										)
							# Type-check `cap.share_value` so the synthesized
							# `Share::share(&x)` HCall goes through the
							# normal call_resolver → instantiation pipeline.
							# Without this, the HCall has no CallInfo and
							# HIR→MIR's `lower_expr(cap.share_value)` would
							# lower to a generic Arc<T>::share call instead
							# of the monomorphized Arc<X>::share__inst__,
							# producing a struct-field type mismatch at
							# LLVM codegen.
							#
							# When `type_expr` raises, surface a span-anchored
							# internal-compiler-error diagnostic with the
							# underlying exception type and message.  The
							# previous form silently swallowed the exception,
							# which left the synthesized HCall with a
							# `callsite_id` but no `call_info_by_callsite_id`
							# entry; the post-typecheck guard at
							# `driftc.py:5273` then surfaced the unhelpful
							# `E_INTERNAL_MISSING_CALLSITE_CALLINFO` with no
							# source span.  Surfacing the real cause here
							# replaces a cryptic downstream error with an
							# actionable one anchored at the offending
							# capture clause.
							if implements_share and getattr(cap, "share_value", None) is not None:
								try:
									type_expr(cap.share_value)
								except Exception as _share_exc:
									try:
										_share_ty_name = self.type_table.get(root_ty).name or f"typeid={root_ty}"
									except Exception:
										_share_ty_name = f"typeid={root_ty}"
									diagnostics.append(
										_tc_diag(
											message=(
												f"internal: failed to type-check "
												f"synthesized `Share::share(&{cap.name})` "
												f"for type '{_share_ty_name}': "
												f"{type(_share_exc).__name__}: {_share_exc}"
											),
											code="E_INTERNAL_SHARE_VALUE_TYPECHECK_FAILED",
											severity="error",
											span=getattr(cap, "span", None) or getattr(cap, "loc", Span()),
										)
									)
				if capture_kinds:
					explicit_capture_stack.append(capture_kinds)
				saved_return_type = return_type
				return_type = lambda_ret_type
				# Auto-try lambda-boundary save/restore (Bug A — 0.31.38).
				# `_auto_try_context()` reads `fn_declared_throws` /
				# `try_block_depth` as closure-captured locals set by the
				# enclosing function's signature.  Without save/restore, a
				# nothrow lambda body nested inside a `throws` outer fn
				# would inherit `fn_declared_throws=True`, eager-unwrap
				# `Result<T,E>` bindings via `or_throw()` synthesis, and
				# break user code that explicitly matches on the Result
				# (`val r = call(); match &r { Ok(_)=>..., Err(_)=>... }`)
				# — the unwrapped `r: T` then fails the variant-scrutinee
				# check.  See `test_auto_try_lambda_boundary.py`.
				saved_fn_declared_throws = fn_declared_throws
				saved_try_block_depth = try_block_depth
				# Lambda body has its own throwable surface, distinct
				# from the outer fn:
				#   - explicit `nothrow` declaration → not throws
				#   - expected-fn shape (Callback*/CallbackThrow*) carries
				#     the throwability the call site demands
				#   - otherwise default to NOT throws (conservative —
				#     auto-try fires only when the user has explicitly
				#     opted into a throwable surface)
				if getattr(expr, "declared_nothrow", False):
					fn_declared_throws = False
				elif expected_fn is not None:
					fn_declared_throws = bool(expected_fn[2])
				else:
					fn_declared_throws = False
				# `try {}` blocks do NOT transit lambda boundaries — if
				# the lambda body wants try-semantics it must open its
				# own.
				try_block_depth = 0
				if expr.body_expr is not None:
					type_expr(expr.body_expr, expected_type=lambda_ret_type)
				if expr.body_block is not None:
					type_block(expr.body_block)
				if lambda_ret_type is None:
					inferred_ret: TypeId | None = None
					if expr.body_expr is not None:
						inferred_ret = type_expr(expr.body_expr)
					elif expr.body_block is not None:
						def _find_return_expr(node: H.HNode) -> H.HExpr | None:
							if isinstance(node, H.HReturn) and node.value is not None:
								return node.value
							if isinstance(node, H.HBlock):
								for st in node.statements:
									found = _find_return_expr(st)
									if found is not None:
										return found
								return None
							for field in getattr(node, "__dataclass_fields__", {}) or {}:
								val = getattr(node, field, None)
								if isinstance(val, H.HNode):
									found = _find_return_expr(val)
									if found is not None:
										return found
								elif isinstance(val, list):
									for it in val:
										if isinstance(it, H.HNode):
											found = _find_return_expr(it)
											if found is not None:
												return found
							return None

						ret_expr = _find_return_expr(expr.body_block)
						if ret_expr is not None:
							inferred_ret = type_expr(ret_expr)
						if inferred_ret is None:
							inferred_ret = self._void
					if inferred_ret is not None:
						lambda_ret_type = inferred_ret
				return_type = saved_return_type
				# Restore auto-try lambda-boundary state (Bug A — 0.31.38).
				fn_declared_throws = saved_fn_declared_throws
				try_block_depth = saved_try_block_depth
				if capture_kinds:
					explicit_capture_stack.pop()
				scope_env.pop()
				scope_bindings.pop()
				actual_can_throw = _lambda_can_throw(expr, call_info_by_callsite_id)
				expr.can_throw_effective = bool(actual_can_throw)
				if getattr(expr, "declared_nothrow", False) and actual_can_throw:
					diagnostics.append(
						_tc_diag(
							message="lambda is declared nothrow but may throw",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if expected_fn is not None:
					if allow_capture_invoke:
						if lambda_type_error:
							return record_expr(expr, self._unknown)
						exp_params, exp_ret, exp_throw = expected_fn
						if not exp_throw and actual_can_throw:
							pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
							diagnostics.append(
								_tc_diag(
									message=f"lambda can throw but is expected to be nothrow for {pretty}",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						if exp_ret == self._unknown and lambda_ret_type is not None:
							fn_ty = self.type_table.ensure_function(exp_params, lambda_ret_type, can_throw=bool(exp_throw))
							return record_expr(expr, fn_ty)
						return record_expr(expr, expected_type)
					# Captureless lambda -> function pointer coercion.
					if lambda_type_error:
						return record_expr(expr, self._unknown)
					captures = list(getattr(expr, "captures", []) or [])
					if expr.explicit_captures:
						if any(getattr(c, "kind", None) in ("ref", "ref_mut") for c in expr.explicit_captures):
							diagnostics.append(
								_tc_diag(
									message="closures with borrowed captures are non-escaping in v0; only immediate invocation or proven non-retaining params are supported",
									severity="error",
									span=getattr(expr, "loc", Span()),
									notes=["wrap it like: (|...| => ...)(...)"],
								)
							)
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message="capturing lambdas cannot be coerced to function pointers",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if not captures:
						res = discover_captures(expr)
						diagnostics.extend(res.diagnostics)
						captures = res.captures
						expr.captures = res.captures
					if captures:
						if any(getattr(c, "kind", None) in ("ref", "ref_mut") for c in captures):
							diagnostics.append(
								_tc_diag(
									message="closures with borrowed captures are non-escaping in v0; only immediate invocation or proven non-retaining params are supported",
									severity="error",
									span=getattr(expr, "loc", Span()),
									notes=["wrap it like: (|...| => ...)(...)"],
								)
							)
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message="capturing lambdas cannot be coerced to function pointers",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					exp_params, exp_ret, exp_throw = expected_fn
					if getattr(expr, "expected_fn_inferred", False) and not actual_can_throw:
						exp_throw = False
					if exp_ret == self._unknown and lambda_ret_type is not None:
						exp_ret = lambda_ret_type
					if not exp_throw and actual_can_throw:
						pretty = self._pretty_type_name(expected_type, current_module=current_module_name)
						diagnostics.append(
							_tc_diag(
								message=f"lambda can throw but is expected to be nothrow for {pretty}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					enclosing = function_symbol(fn_id).replace("::", "_").replace("#", "_")
					name = f"__lambda_fn_{enclosing}_{expr.node_id}"
					lambda_fn_id = FunctionId(module=current_module_name, name=name, ordinal=0)
					if lambda_fn_id not in self._lambda_fn_specs:
						self._lambda_fn_specs[lambda_fn_id] = LambdaFnSpec(
							fn_id=lambda_fn_id,
							origin_fn_id=fn_id,
							lambda_expr=expr,
							param_types=tuple(lambda_param_types),
							return_type=exp_ret,
							can_throw=exp_throw,
							call_info_by_callsite_id=call_info_by_callsite_id,
						)
					fn_ref = FunctionRefId(fn_id=lambda_fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
					call_sig = CallSig(param_types=tuple(exp_params), user_ret_type=exp_ret, can_throw=bool(exp_throw))
					fnptr_consts_by_node_id[expr.node_id] = (fn_ref, call_sig)
					fn_ty = self.type_table.ensure_function(list(exp_params), exp_ret, can_throw=bool(exp_throw))
					return record_expr(expr, fn_ty)
				captures = list(getattr(expr, "captures", []) or [])
				if not captures and expr.explicit_captures is None:
					res = discover_captures(expr)
					diagnostics.extend(res.diagnostics)
					captures = res.captures
					expr.captures = res.captures
				if captures or expr.explicit_captures:
					return record_expr(expr, self._unknown)
				enclosing = function_symbol(fn_id).replace("::", "_").replace("#", "_")
				name = f"__lambda_fn_{enclosing}_{expr.node_id}"
				lambda_fn_id = FunctionId(module=current_module_name, name=name, ordinal=0)
				exp_params = list(lambda_param_types)
				exp_ret = lambda_ret_type if lambda_ret_type is not None else self._unknown
				exp_throw = bool(actual_can_throw)
				fn_ty = self.type_table.ensure_function(exp_params, exp_ret, can_throw=exp_throw)
				if lambda_fn_id not in self._lambda_fn_specs:
					self._lambda_fn_specs[lambda_fn_id] = LambdaFnSpec(
						fn_id=lambda_fn_id,
						origin_fn_id=fn_id,
						lambda_expr=expr,
						param_types=tuple(lambda_param_types),
						return_type=exp_ret,
						can_throw=exp_throw,
						call_info_by_callsite_id=call_info_by_callsite_id,
					)
				fn_ref = FunctionRefId(fn_id=lambda_fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
				call_sig = CallSig(param_types=tuple(exp_params), user_ret_type=exp_ret, can_throw=exp_throw)
				fnptr_consts_by_node_id[expr.node_id] = (fn_ref, call_sig)
				return record_expr(expr, fn_ty)

			if hasattr(H, "HQualifiedMember") and isinstance(expr, getattr(H, "HQualifiedMember")):
				base_te = getattr(expr, "base_type_expr", None)
				if base_te is None or not getattr(base_te, "args", None):
					diagnostics.append(
						_tc_diag(
							message=(
								"E-QMEM-NOT-CALLABLE: qualified member reference is not a first-class value in v1; "
								"call it directly (e.g. `Type::Ctor(...)`)"
							),
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)

				base_tid = resolve_opaque_type(base_te, self.type_table, module_id=current_module_name, allow_generic_base=True)
				try:
					base_def = self.type_table.get(base_tid)
				except Exception:
					base_def = None
				if base_def is None or base_def.kind is not TypeKind.VARIANT:
					name = getattr(base_te, "name", None)
					if isinstance(name, str):
						vb = self.type_table.get_variant_base(module_id=current_module_name, name=name) or self.type_table.get_variant_base(
							module_id="lang.core", name=name
						)
						if vb is not None:
							base_tid = vb
							base_def = self.type_table.get(base_tid)
				if base_def is None or base_def.kind is not TypeKind.VARIANT:
					diagnostics.append(
						_tc_diag(
							message="E-QMEM-NONVARIANT: qualified member base is not a variant type",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)

				schema = self.type_table.get_variant_schema(base_tid)
				if schema is None:
					diagnostics.append(
						_tc_diag(
							message="internal: missing variant schema for qualified member base (compiler bug)",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				arm_schema = next((a for a in schema.arms if a.name == expr.member), None)
				if arm_schema is None:
					ctors = self._format_ctor_signature_list(schema=schema, instance=None, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=(
								f"E-QMEM-NO-CTOR: constructor '{expr.member}' not found in variant "
								f"'{self._pretty_type_name(base_tid, current_module=current_module_name)}'. "
								f"Available constructors: {', '.join(ctors)}"
							),
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if expected_type is not None and not arm_schema.fields:
					try:
						exp_def = self.type_table.get(expected_type)
					except Exception:
						exp_def = None
					if exp_def is not None and exp_def.kind is TypeKind.VARIANT_INSTANCE and exp_def.base_type_id == base_tid:
						return record_expr(expr, expected_type)

				type_params: list[TypeParam] = []
				typevar_ids: list[TypeId] = []
				if schema.type_params:
					owner = FunctionId(module="lang.__internal", name=f"__variant_{schema.module_id}::{schema.name}", ordinal=0)
					for idx, tp_name in enumerate(schema.type_params):
						param_id = TypeParamId(owner=owner, index=idx)
						type_params.append(TypeParam(id=param_id, name=tp_name, span=None))
						typevar_ids.append(self.type_table.ensure_typevar(param_id, name=tp_name))

				type_cache: dict[tuple[TypeId, tuple[TypeId, ...]], TypeId] = {}

				def _lower_generic_expr(expr: GenericTypeExpr) -> TypeId:
					if expr.param_index is not None:
						idx = int(expr.param_index)
						if 0 <= idx < len(typevar_ids):
							return typevar_ids[idx]
						return self._unknown
					name = expr.name
					if name in FIXED_WIDTH_TYPE_NAMES:
						if _fixed_width_allowed(expr.module_id or schema.module_id or current_module_name):
							return self.type_table.ensure_named(name, module_id=expr.module_id or schema.module_id)
						diagnostics.append(
							_tc_diag(
								message=(
									f"fixed-width type '{name}' is reserved in v1; "
									"use Int/Uint/Float or Byte"
								),
								code="E_FIXED_WIDTH_RESERVED",
								severity="error",
								span=Span.from_loc(getattr(expr, "loc", None)),
							)
						)
						return self._unknown
					if name == "Int":
						return self._int
					if name == "Uint":
						return self._uint
					if name == "Byte":
						return self.type_table.ensure_byte()
					if name == "Int32":
						return self.type_table.ensure_int32()
					if name == "Uint32":
						return self.type_table.ensure_uint32()
					if name == "Bool":
						return self._bool
					if name == "Float":
						return self._float
					if name == "String":
						return self._string
					if name == "Void":
						return self._void
					if name == "Error":
						return self._error
					if name == "DiagnosticValue":
						return self._dv
					if name == "Unknown":
						return self._unknown
					if name in {"&", "&mut"} and expr.args:
						inner = _lower_generic_expr(expr.args[0])
						return self.type_table.ensure_ref_mut(inner) if name == "&mut" else self.type_table.ensure_ref(inner)
					if name == "Array" and expr.args:
						elem = _lower_generic_expr(expr.args[0])
						span = Span.from_loc(getattr(expr.args[0], "loc", None)) if expr.args else Span()
						if _reject_zst_array(elem, span=span):
							return self._unknown
						return self.type_table.new_array(elem)
					origin_mod = expr.module_id or schema.module_id
					alias_def = self.type_table.lookup_type_alias(module_id=origin_mod, name=name)
					if alias_def is None:
						unique_alias = self.type_table.find_unique_type_alias_by_name(name=name)
						if unique_alias is not None:
							origin_mod, alias_params_u, alias_target_u, alias_loc_u = unique_alias
							alias_def = (alias_params_u, alias_target_u, alias_loc_u)
					if alias_def is not None:
						alias_params, alias_target, _loc = alias_def
						if len(expr.args) != len(alias_params):
							return self._unknown
						type_param_bindings: dict[str, TypeId] = {}
						for idx, param_name in enumerate(alias_params):
							type_param_bindings[param_name] = _lower_generic_expr(expr.args[idx])
						return resolve_opaque_type(
							alias_target,
							self.type_table,
							module_id=origin_mod,
							type_params=type_param_bindings,
							allow_generic_base=True,
						)
					base_id = (
						self.type_table.get_nominal(kind=TypeKind.STRUCT, module_id=origin_mod, name=name)
						or self.type_table.get_nominal(kind=TypeKind.VARIANT, module_id=origin_mod, name=name)
						or self.type_table.get_nominal(kind=TypeKind.INTERFACE, module_id=origin_mod, name=name)
						or self.type_table.ensure_named(name, module_id=origin_mod)
					)
					if self.type_table.get(base_id).kind is TypeKind.FORWARD_NOMINAL:
						unique = (
							self.type_table.find_unique_nominal_by_name(kind=TypeKind.STRUCT, name=name)
							or self.type_table.find_unique_nominal_by_name(kind=TypeKind.VARIANT, name=name)
							or self.type_table.find_unique_nominal_by_name(kind=TypeKind.INTERFACE, name=name)
						)
						if unique is not None:
							base_id = unique
					if expr.args:
						if base_id in self.type_table.struct_bases:
							schema = self.type_table.struct_bases.get(base_id)
							if schema is not None and not schema.type_params:
								diagnostics.append(
									_tc_diag(
										message=f"type '{name}' is not generic",
										code="E-TYPE-NOT-GENERIC",
										severity="error",
										span=Span.from_loc(getattr(expr, "loc", None)),
									)
								)
								return self._unknown
						elif base_id in self.type_table.variant_schemas:
							schema = self.type_table.variant_schemas.get(base_id)
							if schema is not None and not schema.type_params:
								diagnostics.append(
									_tc_diag(
										message=f"type '{name}' is not generic",
										code="E-TYPE-NOT-GENERIC",
										severity="error",
										span=Span.from_loc(getattr(expr, "loc", None)),
									)
								)
								return self._unknown
						elif base_id in self.type_table.interface_bases:
							schema = self.type_table.interface_bases.get(base_id)
							if schema is not None and not schema.type_params:
								diagnostics.append(
									_tc_diag(
										message=f"type '{name}' is not generic",
										code="E-TYPE-NOT-GENERIC",
										severity="error",
										span=Span.from_loc(getattr(expr, "loc", None)),
									)
								)
								return self._unknown
						else:
							diagnostics.append(
								_tc_diag(
									message=f"unknown generic type '{name}'",
									code="E-TYPE-UNKNOWN",
									severity="error",
									span=Span.from_loc(getattr(expr, "loc", None)),
								)
							)
							return self._unknown
					if expr.args:
						arg_ids = [_lower_generic_expr(a) for a in expr.args]
						if base_id in self.type_table.variant_schemas:
							if any(self.type_table.get(a).kind is TypeKind.TYPEVAR for a in arg_ids):
								key = (base_id, tuple(arg_ids))
								if key not in type_cache:
									td = self.type_table.get(base_id)
									type_cache[key] = self.type_table._add(
										TypeKind.VARIANT,
										td.name,
										list(arg_ids),
										register_named=False,
										module_id=td.module_id,
									)
								return type_cache[key]
							return self.type_table.ensure_instantiated(base_id, arg_ids)
						if base_id in self.type_table.interface_bases:
							if any(self.type_table.get(a).kind is TypeKind.TYPEVAR for a in arg_ids):
								return self.type_table.ensure_interface_template(base_id, arg_ids)
							return self.type_table.ensure_interface_instantiated(base_id, arg_ids)
					return base_id

				param_type_ids: list[TypeId] = []
				for f in arm_schema.fields:
					param_type_ids.append(_lower_generic_expr(f.type_expr))
				ret_type_id = base_tid
				if schema.type_params:
					ret_type_id = _lower_generic_expr(
						GenericTypeExpr.named(schema.name, args=[GenericTypeExpr.param(i) for i in range(len(schema.type_params))], module_id=schema.module_id)
					)
				ctor_sig = FnSignature(
					name=expr.member,
					param_type_ids=param_type_ids,
					return_type_id=ret_type_id,
					type_params=type_params,
					module=current_module_name,
				)

				explicit_type_args = [
					resolve_opaque_type(t, self.type_table, module_id=current_module_name)
					for t in (base_te.args or [])
				]
				first_loc = getattr((base_te.args or [None])[0], "loc", None)
				call_type_args_span = Span.from_loc(first_loc) if first_loc is not None else None
				inst_res = _instantiate_sig(
					sig=ctor_sig,
					arg_types=[],
					expected_type=None,
					explicit_type_args=explicit_type_args,
					allow_infer=False,
					diag_span=call_type_args_span or getattr(expr, "loc", Span()),
					call_kind="ctor",
					call_name=expr.member,
				)
				if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT:
					diagnostics.append(
						_tc_diag(
							message=(
								f"E-QMEM-TYPEARGS-ARITY: expected {len(schema.type_params)} type arguments, got {len(explicit_type_args)}"
							),
							severity="error",
							span=call_type_args_span or getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS:
					diagnostics.append(
						_tc_diag(
							message="constructor does not accept type arguments; use the non-generic form instead",
							severity="error",
							span=call_type_args_span or getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if inst_res.error:
					return record_expr(expr, self._unknown)
				if inst_res.inst_params is None or inst_res.inst_return is None:
					return record_expr(expr, self._unknown)
				inst_return = inst_res.inst_return

				return record_expr(expr, self.type_table.new_function(list(inst_res.inst_params), inst_res.inst_return))

			if hasattr(H, "HTypeApp") and isinstance(expr, getattr(H, "HTypeApp")):
				call_type_args_span = None
				if getattr(expr, "type_args", None):
					first_loc = getattr((expr.type_args or [None])[0], "loc", None)
					call_type_args_span = Span.from_loc(first_loc)
				type_arg_ids = [
					resolve_opaque_type(t, self.type_table, module_id=current_module_name, type_params=type_param_map)
					for t in (expr.type_args or [])
				]

				if isinstance(expr.fn, H.HVar):
					if callable_registry is not None:
						include_private = current_module if expr.fn.module_id is None else None
						candidates = callable_registry.get_free_candidates(
							name=expr.fn.name,
							visible_modules=_visible_modules_for_free_call(expr.fn.module_id),
							include_private_in=include_private,
						)
						viable: list[tuple[CallableDecl, FnSignature, list[TypeId], TypeId, bool]] = []
						type_arg_counts: set[int] = set()
						saw_registry_only = False
						saw_typed_nongeneric = False
						for decl in candidates:
							sig = None
							if decl.fn_id is not None and signatures_by_id is not None:
								sig = signatures_by_id.get(decl.fn_id)
							if sig is None:
								saw_registry_only = True
								continue
							if sig.param_type_ids is None and sig.param_types is not None:
								local_type_params = {p.name: p.id for p in sig.type_params}
								param_type_ids = [
									resolve_opaque_type(p, self.type_table, module_id=sig.module, type_params=local_type_params)
									for p in sig.param_types
								]
								sig = replace(sig, param_type_ids=param_type_ids)
							if sig.return_type_id is None and sig.return_type is not None:
								local_type_params = {p.name: p.id for p in sig.type_params}
								ret_id = resolve_opaque_type(sig.return_type, self.type_table, module_id=sig.module, type_params=local_type_params)
								sig = replace(sig, return_type_id=ret_id)
							if sig.param_type_ids is None or sig.return_type_id is None:
								continue
							inst_res = _instantiate_sig(
								sig=sig,
								arg_types=[],
								expected_type=None,
								explicit_type_args=type_arg_ids,
								allow_infer=False,
								diag_span=call_type_args_span or getattr(expr, "loc", Span()),
								call_kind="free",
								call_name=decl.name,
							)
							if inst_res.error is not None and sig.type_params and len(type_arg_ids) == len(sig.type_params) and sig.param_type_ids is not None and sig.return_type_id is not None:
								subst = Subst(owner=sig.type_params[0].id.owner, args=list(type_arg_ids))
								inst_params = [apply_subst(p, subst, self.type_table) for p in sig.param_type_ids]
								inst_return = apply_subst(sig.return_type_id, subst, self.type_table)
								inst_res = InferResult(
									ok=True,
									subst=subst,
									inst_params=inst_params,
									inst_return=inst_return,
									context=None,
									error=None,
								)
							if inst_res.error and inst_res.error.kind is InferErrorKind.NO_TYPEPARAMS:
								saw_typed_nongeneric = True
								continue
							if inst_res.error and inst_res.error.kind is InferErrorKind.TYPEARG_COUNT:
								if inst_res.error.expected_count is not None:
									type_arg_counts.add(inst_res.error.expected_count)
								continue
							if inst_res.error:
								continue
							if inst_res.inst_params is None or inst_res.inst_return is None:
								continue
							can_throw = True
							if sig.declared_can_throw is not None:
								can_throw = bool(sig.declared_can_throw)
							viable.append((decl, sig, list(inst_res.inst_params), inst_res.inst_return, can_throw))

						if len(viable) == 1:
							decl, sig, params, ret, can_throw = viable[0]
							if decl.fn_id is not None:
								concrete_type_args = not type_arg_ids or not any(self.type_table.has_typevar(t) for t in type_arg_ids)
								if concrete_type_args:
									ref_fn_id = decl.fn_id
									if type_arg_ids and function_keys_by_fn_id is not None:
										key = function_keys_by_fn_id.get(decl.fn_id)
										if key is not None:
											record_instantiation(
												callsite_id=getattr(expr, "callsite_id", None),
												node_id=expr.node_id,
												target_fn_id=decl.fn_id,
												impl_args=tuple(),
												fn_args=tuple(type_arg_ids),
												callsite_span=getattr(expr, "loc", None),
											)
											inst_key = build_instantiation_key(
												key,
												tuple(type_arg_ids),
												type_table=self.type_table,
												can_throw=bool(can_throw),
											)
											inst_name = f"{key.name}__inst__{instantiation_key_hash(inst_key)}"
											ref_fn_id = FunctionId(module=key.module_path, name=inst_name, ordinal=0)
									is_exported = bool(getattr(sig, "is_exported_entrypoint", False))
									is_extern = bool(getattr(sig, "is_extern", False))
									if (is_exported or is_extern) and ref_fn_id == decl.fn_id:
										if bool(getattr(sig, "declared_can_throw", False)):
											fn_ref = _ensure_boundary_thunk(ref_fn_id, params, ret)
										else:
											fn_ref = _ensure_ok_wrap_thunk(ref_fn_id, params, ret)
									else:
										fn_ref = FunctionRefId(fn_id=ref_fn_id, kind=FunctionRefKind.IMPL, has_wrapper=False)
									call_sig = CallSig(param_types=tuple(params), user_ret_type=ret, can_throw=bool(can_throw))
									fnptr_consts_by_node_id[expr.node_id] = (fn_ref, call_sig)
							return record_expr(expr, self.type_table.ensure_function(params, ret, can_throw=can_throw))
						if saw_registry_only:
							diagnostics.append(
								_tc_diag(
									message=f"type arguments require a typed signature for '{expr.fn.name}'",
									severity="error",
									span=call_type_args_span or getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						if saw_typed_nongeneric:
							diagnostics.append(
								_tc_diag(
									message=f"type arguments require a generic signature for '{expr.fn.name}'",
									severity="error",
									span=call_type_args_span or getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						if type_arg_counts:
							diagnostics.append(
								_tc_diag(
									message=(
										f"type argument count mismatch for '{expr.fn.name}': expected {sorted(type_arg_counts)}, "
										f"got {len(type_arg_ids)}"
									),
									severity="error",
									span=call_type_args_span or getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						if len(viable) > 1:
							diagnostics.append(
								_tc_diag(
									message=f"ambiguous callable reference to '{expr.fn.name}'",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
					diagnostics.append(
						_tc_diag(
							message=f"unknown function '{expr.fn.name}'",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if hasattr(H, "HQualifiedMember") and isinstance(expr.fn, getattr(H, "HQualifiedMember")):
					preseed = preseed_type_params or {}
					call_ctx = make_call_ctx(type_table=self.type_table, diagnostics=diagnostics, current_module_name=current_module_name, current_module=current_module, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, preseed_type_params=preseed, type_param_names=type_param_names, current_fn_id=fn_id, int_ty=self._int, uint_ty=self._uint, uint64_ty=self._uint64, byte_ty=self.type_table.ensure_byte(), bool_ty=self._bool, float_ty=self._float, string_ty=self._string, void_ty=self._void, error_ty=self._error, dv_ty=self._dv, unknown_ty=self._unknown, signatures_by_id=signatures_by_id, callable_registry=callable_registry, trait_index=trait_index, trait_impl_index=trait_impl_index, impl_index=impl_index, visible_modules=visible_modules, visible_trait_world=visible_trait_world, global_trait_world=global_trait_world, trait_scope_by_module=trait_scope_by_module, require_env_local=require_env_local, fn_require_assumed=fn_require_assumed, binding_mutable=binding_mutable, binding_id_by_name={name: bid for bid, name in binding_names.items()}, traits_in_scope=_traits_in_scope, trait_key_for_id=trait_key_for_id, tc_diag=_tc_diag, type_expr=type_expr, optional_variant_type=self._optional_variant_type, unwrap_ref_type=_unwrap_ref_type, struct_base_and_args=_struct_base_and_args, receiver_place=_receiver_place, receiver_can_mut_borrow=_receiver_can_mut_borrow, receiver_compat=_receiver_compat, receiver_preference=_receiver_preference, args_match_params=_args_match_params, coerce_args_for_params=_coerce_args_for_params, infer_receiver_arg_type=_infer_receiver_arg_type, instantiate_sig_with_subst=_instantiate_sig_with_subst, apply_autoborrow_args=_apply_autoborrow_args, label_typeid=_label_typeid, trait_label=_trait_label, require_for_fn=_require_for_fn, extract_conjunctive_facts=_extract_conjunctive_facts, subject_name=_subject_name, normalize_type_key=_normalize_type_key, collect_trait_subjects=_collect_trait_subjects, require_failure=_require_failure, format_failure_message=_format_failure_message, failure_code=_failure_code, requirement_notes=_requirement_notes, pick_best_failure=_pick_best_failure, param_scope_map=_param_scope_map, candidate_key_for_decl=_candidate_key_for_decl, visibility_note=_visibility_note, intrinsic_method_fn_id=_intrinsic_method_fn_id, instantiate_sig=_instantiate_sig, self_mode_from_sig=_self_mode_from_sig, match_impl_type_args=_match_impl_type_args, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=self._pretty_type_name, format_ctor_signature_list=self._format_ctor_signature_list, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw, record_call_resolution=record_call_resolution, record_iface_coercion=record_iface_coercion, iface_assignable=iface_assignable, record_instantiation=record_instantiation, alloc_callsite_id=_alloc_callsite_id, alloc_node_id=_assign_node_id, allow_unsafe=unsafe_allowed_module, unsafe_context=unsafe_context, allow_unsafe_without_block=allow_unsafe_without_block_local, allow_rawbuffer=self._is_toolchain_trusted_module(current_module_name))
					ctor_res = resolve_qualified_member_call(
						make_resolver_ctx(call_ctx),
						expr.fn,
						arg_exprs=[],
						arg_types=[],
						kw_pairs=[],
						expected_type=None,
						type_arg_ids=type_arg_ids,
						allow_infer=False,
						call_type_args_span=call_type_args_span,
					)
					if ctor_res is not None and ctor_res.inst_params is not None and ctor_res.inst_return is not None:
						return record_expr(expr, self.type_table.new_function(list(ctor_res.inst_params), ctor_res.inst_return))
					diagnostics.append(
						_tc_diag(
							message="E-TYPEAPP-TARGET: type application requires a named callable target",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)


			# `match` expression (statement-form match is parsed separately; this
			# branch only handles expression-form matches).
			if hasattr(H, "HMatchExpr") and isinstance(expr, getattr(H, "HMatchExpr")):
				scrut_ty = type_expr(expr.scrutinee, used_as_value=False)
				inst = None
				scrut_ref_mut: bool | None = None
				if scrut_ty is not None:
					try:
						td_scrut = self.type_table.get(scrut_ty)
					except Exception:
						td_scrut = None
					if td_scrut is not None and td_scrut.kind is TypeKind.REF and td_scrut.param_types:
						inner = td_scrut.param_types[0]
						try:
							td_inner = self.type_table.get(inner)
						except Exception:
							td_inner = None
						if td_inner is not None and td_inner.kind is TypeKind.VARIANT:
							inst = self.type_table.get_variant_instance(inner)
							scrut_ref_mut = bool(td_scrut.ref_mut)
						else:
							diagnostics.append(
								_tc_diag(
									message="match scrutinee must be a variant type",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
					elif td_scrut is not None and td_scrut.kind is not TypeKind.VARIANT:
						diagnostics.append(
							_tc_diag(
								message="match scrutinee must be a variant type",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					elif td_scrut is not None and td_scrut.kind is TypeKind.VARIANT:
						inst = self.type_table.get_variant_instance(scrut_ty)

				seen_default = False
				seen_default_span: Span | None = None
				seen_ctors: set[str] = set()
				result_ty: TypeId | None = None

				for idx, arm in enumerate(expr.arms):
					if arm.ctor is None:
						# default arm
						if seen_default:
							diagnostics.append(
								_tc_diag(
									message="match default arm may appear at most once",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						seen_default = True
						seen_default_span = getattr(arm, "loc", Span())
					else:
						if seen_default:
							diagnostics.append(
								_tc_diag(
									message="match arms after default are unreachable",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						if arm.ctor in seen_ctors:
							diagnostics.append(
								_tc_diag(
									message=f"duplicate match arm for constructor '{arm.ctor}'",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						seen_ctors.add(arm.ctor)

					# If the pattern uses a qualified constructor base, validate it.
					arm_ctor_base = getattr(arm, "ctor_base", None)
					if arm.ctor is not None and arm_ctor_base is not None:
						base_tid = resolve_opaque_type(
							arm_ctor_base,
							self.type_table,
							module_id=current_module_name,
							allow_generic_base=True,
						)
						base_td = None
						try:
							base_td = self.type_table.get(base_tid)
						except Exception:
							base_td = None
						if base_td is None or base_td.kind is not TypeKind.VARIANT:
							diagnostics.append(
								_tc_diag(
									message=f"qualified constructor base '{arm_ctor_base}' is not a variant type",
									severity="error",
									span=getattr(arm, "loc", Span()),
								)
							)
						else:
							base_inst = self.type_table.get_variant_instance(base_tid)
							if base_inst is None:
								if inst is not None:
									if inst.base_id != base_tid:
										diagnostics.append(
											_tc_diag(
												message=(
													f"constructor '{arm.ctor}' is qualified by '{arm_ctor_base}', "
													"which does not match the match scrutinee type"
												),
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
								# If the base is a generic variant with no concrete instance yet,
								# defer resolution to the scrutinee instance (if any).
							else:
								if inst is None:
									inst = base_inst
								elif inst.base_id != base_inst.base_id:
									diagnostics.append(
										_tc_diag(
											message=(
												f"constructor '{arm.ctor}' is qualified by '{arm_ctor_base}', "
												"which does not match the match scrutinee type"
											),
											severity="error",
											span=getattr(arm, "loc", Span()),
										)
									)

					# Type-check arm body under a scope that includes constructor binders.
					scope_env.append(dict())
					scope_bindings.append(dict())
					try:
						if arm.ctor is not None and inst is not None:
							arm_def = inst.arms_by_name.get(arm.ctor)
							schema = self.type_table.get_variant_schema(inst.base_id)
							if schema is not None and schema.tombstone_ctor == arm.ctor:
								diagnostics.append(
									_tc_diag(
										message=f"E-MATCH-TOMBSTONE: tombstone constructor '{arm.ctor}' is internal and cannot be matched",
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
								continue
							if arm_def is None:
								diagnostics.append(
									_tc_diag(
										message=f"unknown constructor '{arm.ctor}' for this variant",
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
							else:
								form = getattr(arm, "pattern_arg_form", "positional")
								field_names = list(getattr(arm_def, "field_names", []) or [])
								field_types = list(arm_def.field_types)
								field_indices: list[int] = []

								if form == "bare":
									# Bare ctor patterns (`Ctor`) are allowed only for zero-field ctors.
									if field_types:
										diagnostics.append(
											_tc_diag(
												message=(
													f"E-MATCH-PAT-BARE: constructor pattern '{arm.ctor}' requires parentheses; "
													"use `Ctor()` to ignore payload fields"
												),
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
									if arm.binders:
										diagnostics.append(
											_tc_diag(
												message=f"E-MATCH-PAT-BARE: bare constructor pattern '{arm.ctor}' cannot bind fields",
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
								elif form == "paren":
									# `Ctor()` matches the tag only and ignores payload; it binds nothing.
									if arm.binders:
										diagnostics.append(
											_tc_diag(
												message=f"E-MATCH-PAT-PAREN: '{arm.ctor}()' pattern must not bind fields",
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
								elif form == "named":
									binder_fields = getattr(arm, "binder_fields", None)
									if binder_fields is None or len(binder_fields) != len(arm.binders):
										diagnostics.append(
											_tc_diag(
												message=f"internal: named constructor pattern missing binder field list (compiler bug)",
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
									else:
										seen_fields: set[str] = set()
										for fname, bname in zip(binder_fields, arm.binders):
											if fname in seen_fields:
												diagnostics.append(
													_tc_diag(
														message=f"duplicate field '{fname}' in constructor pattern '{arm.ctor}'",
														severity="error",
														span=getattr(arm, "loc", Span()),
													)
												)
												continue
											seen_fields.add(fname)
											if fname not in field_names:
												diagnostics.append(
													_tc_diag(
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
								else:
									# Positional binders (exact arity in v1).
									if len(arm.binders) != len(field_types):
										diagnostics.append(
											_tc_diag(
												message=(
													f"constructor pattern '{arm.ctor}' expects {len(field_types)} binders, got {len(arm.binders)}"
												),
												severity="error",
												span=getattr(arm, "loc", Span()),
											)
										)
									field_indices = list(range(min(len(arm.binders), len(field_types))))

								# Store normalized binder→field-index mapping for stage2 lowering.
								if hasattr(arm, "binder_field_indices"):
									arm.binder_field_indices = list(field_indices)

								# Bind only the fields requested by the pattern form.
								binder_muts = list(getattr(arm, "binder_is_mutable", []) or [])
								if len(binder_muts) != len(arm.binders):
									binder_muts = [False for _ in arm.binders]
								for (bname, fidx, is_mut) in zip(arm.binders, field_indices, binder_muts):
									if fidx < 0 or fidx >= len(field_types):
										continue
									bty = field_types[fidx]
									# Track whether this binder's underlying
									# field is `Copy` and the scrutinee is a
									# *shared* by-ref match (G3 scope).  Used
									# below to autodref via HIR rewrite at
									# value-context use sites.
									_is_copy_arm_binder = (
										scrut_ref_mut is False
										and self.type_table.copy_status(bty) is True
									)
									if scrut_ref_mut is not None:
										bty = self.type_table.ensure_ref_mut(bty) if scrut_ref_mut else self.type_table.ensure_ref(bty)
									if drift_debug.enabled("match"):
										try:
											import sys
											print(f"[debug] match binder {bname} type={self._pretty_type_name(bty, current_module=current_module_name)} in {function_symbol(fn_id)} ctor={arm.ctor}", file=sys.stderr)
										except Exception:
											pass
									bid = self._alloc_local_id()
									locals.append(bid)
									scope_env[-1][bname] = bty
									scope_bindings[-1][bname] = bid
									binding_types[bid] = bty
									binding_names[bid] = bname
									binding_mutable[bid] = is_mut
									binding_place_kind[bid] = PlaceKind.LOCAL
									if _is_copy_arm_binder:
										copy_arm_binder_ids.add(bid)

						type_block_in_scope(arm.block)

						# G3: rewrite a bare `HVar` arm result that refers
						# to a Copy match-arm binder as `HUnary(DEREF,
						# HVar)` so the arm value is the loaded Copy
						# value, not the borrow.  Closes the let-init
						# coercion gap (`val k: Int = match &v { Active(n)
						# => { n } }`) end-to-end at HIR→MIR via the
						# DEREF lowering.
						def _maybe_rewrite_arm_value_to_deref(slot_get, slot_set) -> None:
							e = slot_get()
							if not isinstance(e, H.HVar):
								return
							bid = getattr(e, "binding_id", None)
							if bid is None or bid not in copy_arm_binder_ids:
								return
							new_e = H.HUnary(op=H.UnaryOp.DEREF, expr=e)
							_assign_node_id(new_e)
							slot_set(new_e)
						arm_value_ty: TypeId | None = None
						if arm.result is not None:
							arm_value_ty = type_expr(arm.result, expected_type=expected_type)
							_maybe_rewrite_arm_value_to_deref(
								lambda: arm.result,
								lambda new: setattr(arm, "result", new),
							)
							if arm.result is not None and isinstance(arm.result, H.HUnary):
								arm_value_ty = type_expr(arm.result, expected_type=expected_type)
						elif used_as_value:
							# For value-block arms, allow a trailing expression statement to
							# supply the arm's result type.
							last = arm.block.statements[-1] if arm.block.statements else None
							if isinstance(last, H.HExprStmt):
								arm_value_ty = type_expr(last.expr, expected_type=expected_type)
								def _set_last_expr(new):
									last.expr = new
								_maybe_rewrite_arm_value_to_deref(
									lambda: last.expr,
									_set_last_expr,
								)
								if isinstance(last.expr, H.HUnary):
									arm_value_ty = type_expr(last.expr, expected_type=expected_type)
							else:
								# Allow diverging arms to omit a value in v1. We treat a block as
								# diverging when it ends with a terminator statement.
								diverges = isinstance(last, (H.HReturn, H.HBreak, H.HContinue, H.HThrow, H.HRethrow))
								if not diverges:
									diagnostics.append(
										_tc_diag(
											message="E-MATCH-ARM-NO-VALUE: match arm must end with an expression when match result is used",
											severity="error",
											span=getattr(arm, "loc", Span()),
										)
									)
						if used_as_value and arm_value_ty is not None:
							if result_ty is None:
								result_ty = arm_value_ty
							else:
								arm_cmp_ty = _dealias_zero_param(arm_value_ty)
								result_cmp_ty = _dealias_zero_param(result_ty)
								if arm_cmp_ty == result_cmp_ty:
									continue
								arm_key = _normalize_type_key(type_key_from_typeid(self.type_table, arm_cmp_ty))
								result_key = _normalize_type_key(type_key_from_typeid(self.type_table, result_cmp_ty))
								if arm_key != result_key:
									diagnostics.append(
										_tc_diag(
											message=(
											"E-MATCH-ARM-TYPE: match arms must produce the same type when match result is used "
											f"(have {self.type_table.get(arm_value_ty).name}, expected {self.type_table.get(result_ty).name})"
										),
										severity="error",
										span=getattr(arm, "loc", Span()),
									)
								)
					finally:
						scope_env.pop()
						scope_bindings.pop()

				# Non-exhaustive matches require a default arm (MVP rule).
				if inst is not None and not seen_default:
					all_ctors = set(inst.arms_by_name.keys())
					schema = self.type_table.get_variant_schema(inst.base_id) if inst is not None else None
					if schema is not None and schema.tombstone_ctor:
						all_ctors.discard(schema.tombstone_ctor)
					if drift_debug.enabled("match"):
						try:
							import sys
							print(f"[debug] match in {function_symbol(fn_id)} inst={inst.base_id} arms={list(inst.arms_by_name.keys())} seen={sorted(seen_ctors)} all={sorted(all_ctors)}", file=sys.stderr)
						except Exception:
							pass
					missing = all_ctors - seen_ctors
					if missing:
						diagnostics.append(
							_tc_diag(
								message=f"E-MATCH-NONEXHAUSTIVE: non-exhaustive match must include default arm (missing: {', '.join(sorted(missing))})",
								severity="error",
								span=getattr(expr, "loc", Span()) if seen_default_span is None else seen_default_span,
							)
						)

				if not used_as_value:
					return record_expr(expr, self._void)
				if result_ty is None:
					diagnostics.append(
						_tc_diag(
							message="E-MATCH-NO-VALUE: match result is used but no arm produces a value",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				return record_expr(expr, result_ty)

			# Borrow.
			if isinstance(expr, H.HBorrow):
				borrow_span = _best_effort_span_for_expr(expr.subject)
				if borrow_span.line is None:
					borrow_span = _best_effort_span_for_expr(expr)
				expr_id = getattr(expr, "node_id", None)
				if expr_id is None:
					expr_id = id(expr)
				if expr_id in borrow_expr_ids_in_stmt:
					inner_ty = type_expr(expr.subject, used_as_value=False)
					ref_ty = self.type_table.ensure_ref_mut(inner_ty) if expr.is_mut else self.type_table.ensure_ref(inner_ty)
					return record_expr(expr, ref_ty)
				# Guardrail: do not materialize `&mut (move x)` into a temp. This would
				# turn an explicit ownership transfer into an implicit "store then
				# borrow" pattern, which is a semantic expansion we want to avoid.
				#
				# Instead, reject at type-check time with a targeted diagnostic.
				def _contains_move(node: H.HExpr) -> bool:
					if hasattr(H, "HMove") and isinstance(node, getattr(H, "HMove")):
						return True
					if isinstance(node, H.HUnary):
						return _contains_move(node.expr)
					if isinstance(node, H.HBinary):
						return _contains_move(node.left) or _contains_move(node.right)
					if isinstance(node, H.HTernary):
						return _contains_move(node.cond) or _contains_move(node.then_expr) or _contains_move(node.else_expr)
					if isinstance(node, H.HCall):
						return (
							_contains_move(node.fn)
							or any(_contains_move(a) for a in node.args)
							or any(_contains_move(k.value) for k in getattr(node, "kwargs", []) or [])
						)
					if isinstance(node, H.HMethodCall):
						return (
							_contains_move(node.receiver)
							or any(_contains_move(a) for a in node.args)
							or any(_contains_move(k.value) for k in getattr(node, "kwargs", []) or [])
						)
					if isinstance(node, H.HField):
						return _contains_move(node.subject)
					if isinstance(node, H.HIndex):
						return _contains_move(node.subject) or _contains_move(node.index)
					if isinstance(node, getattr(H, "HPlaceExpr", ())):
						# Canonical places cannot contain moves in their base/projections.
						return False
					if isinstance(node, H.HArrayLiteral):
						return any(_contains_move(e) for e in node.elements)
					if isinstance(node, H.HDVInit):
						return any(_contains_move(a) for a in node.args)
					if isinstance(node, H.HExceptionInit):
						return any(_contains_move(a) for a in node.pos_args) or any(_contains_move(k.value) for k in node.kw_args)
					# NOTE: body scan only checks HExprStmt, so moves inside HLet/HAssign
					# within block-expression bodies are not detected. Shared limitation
					# across HTryExpr, HUnsafeExpr, and any future block-expression form.
					if isinstance(node, getattr(H, "HTryExpr", ())):
						if _contains_move(node.attempt):
							return True
						for arm in node.arms:
							if any(_contains_move(s.expr) for s in arm.block.statements if isinstance(s, H.HExprStmt)):
								return True
							if arm.result is not None and _contains_move(arm.result):
								return True
						return False
					if isinstance(node, getattr(H, "HUnsafeExpr", ())):
						return any(_contains_move(s.expr) for s in node.body.statements if isinstance(s, H.HExprStmt)) or _contains_move(node.result)
					return False

				if expr.is_mut and _contains_move(expr.subject):
					diagnostics.append(
						_tc_diag(
							message="cannot take &mut of an expression containing move; assign to a var first",
							severity="error",
							span=borrow_span,
						)
					)
					return record_expr(expr, self._unknown)

				inner_ty = type_expr(expr.subject, used_as_value=False)
				# MVP: borrowing is only supported from addressable places, except
				# for shared borrows which may be materialized from rvalues.
				#
				# Current support:
				# - locals/params: `&x`, `&mut x`
				# - projections: `&x.field`, `&arr[i]`
				# - reborrow through a reference: `&*p`, `&mut *p`
				# - shared borrows of rvalues via temporary materialization
				#   (`&(make())` becomes `val tmp = make(); &tmp`).
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				place = place_from_expr(expr.subject, base_lookup=_base_lookup)
				if place is None:
					if expr.is_mut:
						diagnostics.append(
							_tc_diag(
								message="borrow operand must be an addressable place in v1 (local/param or deref place)",
								severity="error",
								span=borrow_span,
							)
						)
						return record_expr(expr, self._unknown)
					inner_ty = type_expr(expr.subject)
					ref_ty = self.type_table.ensure_ref(inner_ty) if inner_ty is not None else self._unknown
					return record_expr(expr, ref_ty)

				# MVP: we accept borrowing from nested projections (`x.field`, `arr[i]`,
				# `(*p).field`, etc.) as long as the operand is a real place.
				#
				# Note: rvalues are rejected above by `place_from_expr` returning None.
				# Auto-borrow is applied only at call sites; explicit `&x` is still
				# required when writing a borrow expression directly.

				if expr.is_mut:
					# `&mut x` requires `x` to be `var`.
					#
					# We enforce two invariants:
					#  - If the borrow is from owned storage (no deref projections), the base
					#    binding must be `var`. (Example: `&mut p.x` where `p` is a local.)
					#  - If the borrow goes through a deref projection (reborrow), mutability
					#    comes from the reference being dereferenced (Example: `&mut (*p).x`
					#    where `p: &mut Point`). In that case, the base binding does not need
					#    to be `var` (params are effectively `val`), but the dereferenced
					#    reference must be `&mut`.
					#  - If the place includes a deref projection, the reference being dereferenced
					#    must itself be mutable (`&mut`), i.e. a mutable reborrow.
					has_deref = any(isinstance(p, DerefProj) for p in place.projections)
					base_is_ref_mut = False
					base_ty = None
					if place.base.local_id is not None:
						base_ty = binding_types.get(place.base.local_id)
						base_def = self.type_table.get(base_ty) if base_ty is not None else None
						base_is_ref_mut = bool(base_def is not None and base_def.kind is TypeKind.REF and base_def.ref_mut)
					if (not has_deref) and place.base.local_id is not None and not binding_mutable.get(
						place.base.local_id, False
					):
						if self_binding_id is not None and place.base.local_id == self_binding_id and self_param_allows_mut_borrow:
							pass
						elif binding_param_ref_mut.get(place.base.local_id, False):
							pass
						elif base_ty is None:
							pass
						elif not base_is_ref_mut:
							diag = _tc_diag(
								message="cannot take &mut of an immutable binding; declare it with `var`",
								severity="error",
								span=borrow_span,
							)
							span = getattr(diag, "span", None)
							if span is not None and span.file is None and span.line is None and span.column is None:
								diag.notes.append(f"in '{function_symbol(fn_id)}'")
								diag.notes.append(f"borrow base '{place.base.name}' (binding_id={place.base.local_id})")
							diagnostics.append(diag)
					# Detect a deref projection anywhere in the place and validate the corresponding
					# reference expression is `&mut`.
					#
					# We do a conservative check:
					#  - For canonical `HPlaceExpr` operands, walk projections and ensure each deref
					#    happens through `&mut`.
					#  - For legacy tree-shaped operands (`HUnary(DEREF, ...)`), walk the tree.
					def _validate_mutable_derefs(node: H.HExpr) -> None:
						if hasattr(H, "HPlaceExpr") and isinstance(node, getattr(H, "HPlaceExpr")):
							cur = type_expr(node.base, used_as_value=False)
							for pr in node.projections:
								if isinstance(pr, H.HPlaceDeref):
									ptr_def = self.type_table.get(cur)
									if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
										diagnostics.append(
											_tc_diag(
												message="cannot take &mut through *p unless p is a mutable reference (&mut T)",
												severity="error",
												span=borrow_span,
											)
										)
										return
									if ptr_def.param_types:
										cur = ptr_def.param_types[0]
								elif isinstance(pr, H.HPlaceField):
									td = self.type_table.get(cur)
									if td.kind is TypeKind.STRUCT:
										info = self.type_table.struct_field(cur, pr.name)
										if info is not None:
											_, cur = info
								elif isinstance(pr, H.HPlaceIndex):
									td = self.type_table.get(cur)
									if td.kind is TypeKind.ARRAY and td.param_types:
										cur = td.param_types[0]
							return
						if isinstance(node, H.HUnary) and node.op is H.UnaryOp.DEREF:
							ptr_ty = type_expr(node.expr, used_as_value=False)
							ptr_def = self.type_table.get(ptr_ty)
							if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
								diagnostics.append(
										_tc_diag(
											message="cannot take &mut through *p unless p is a mutable reference (&mut T)",
											severity="error",
											span=borrow_span,
										)
									)
							_validate_mutable_derefs(node.expr)
						elif isinstance(node, H.HField):
							_validate_mutable_derefs(node.subject)
						elif isinstance(node, H.HIndex):
							_validate_mutable_derefs(node.subject)
							_validate_mutable_derefs(node.index)

					_validate_mutable_derefs(expr.subject)
					conflict = False
					for existing, kind in borrows_in_stmt.items():
						if not places_overlap(place, existing):
							continue
						conflict = True
						diagnostics.append(
								_tc_diag(
									message="conflicting borrows in the same statement: cannot take &mut while borrowed",
									severity="error",
									span=borrow_span,
								)
							)
						break
					borrows_in_stmt[place] = "mut"
					borrow_expr_ids_in_stmt.add(expr_id)
				else:
					conflict = False
					for existing, kind in borrows_in_stmt.items():
						if not places_overlap(place, existing):
							continue
						if kind == "mut":
							conflict = True
							diagnostics.append(
									_tc_diag(
										message="conflicting borrows in the same statement: cannot take & while mutably borrowed",
										severity="error",
										span=borrow_span,
									)
								)
							break
					borrows_in_stmt.setdefault(place, "shared")
					borrow_expr_ids_in_stmt.add(expr_id)

				ref_ty = self.type_table.ensure_ref_mut(inner_ty) if expr.is_mut else self.type_table.ensure_ref(inner_ty)
				return record_expr(expr, ref_ty)

			# Explicit move.
			#
			# `move <place>` is a surface marker for ownership transfer. For MVP we
			# keep it deliberately strict:
			# - the operand must be an addressable place (same as borrow),
			# - the operand must be a *plain* binding (no projections) to avoid
			#   partial-move semantics before we have a real lifetime/ownership model.
			#
			# The borrow checker enforces:
			# - no moving while borrowed, and
			# - use-after-move until reinitialization.
			if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
				move_span = _best_effort_span_for_expr(expr.subject)
				if move_span.line is None:
					move_span = _best_effort_span_for_expr(expr)
				if isinstance(expr.subject, H.HVar) and expr.subject.binding_id is None:
					for scope in reversed(scope_bindings):
						if expr.subject.name in scope:
							expr.subject.binding_id = scope[expr.subject.name]
							break
				if hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
					base = expr.subject.base
					if isinstance(base, H.HVar) and base.binding_id is None:
						for scope in reversed(scope_bindings):
							if base.name in scope:
								base.binding_id = scope[base.name]
								break

				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				place = place_from_expr(expr.subject, base_lookup=_base_lookup)
				if place is None:
					diagnostics.append(
						_tc_diag(
							message="move operand must be an addressable place in v1 (local/param)",
							severity="error",
							span=move_span,
						)
					)
					return record_expr(expr, self._unknown)
				if place.projections:
					diagnostics.append(
						_tc_diag(
							message="move of a projected place is not supported in v1; move a local/param or use swap/replace",
							severity="error",
							span=move_span,
						)
					)
					return record_expr(expr, self._unknown)
				subject_name = getattr(expr.subject, "name", None)
				if subject_name is None and hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
					subject_name = getattr(expr.subject.base, "name", None)
				if place.base.local_id is not None:
					is_implicit_move = bool(getattr(expr, "is_implicit", False))
					if is_implicit_move:
						# Implicit moves keep existing mutability rules.
						if not binding_mutable.get(place.base.local_id, False) and subject_name != "self":
							diagnostics.append(
								_tc_diag(
									message="move requires an owned mutable binding declared with var",
									severity="error",
									span=move_span,
								)
							)
							return record_expr(expr, self._unknown)
				inner_ty = type_expr(expr.subject, used_as_value=False)
				if inner_ty is not None:
					td = self.type_table.get(inner_ty)
					if td.kind is TypeKind.REF:
						diagnostics.append(
							_tc_diag(
								message="cannot move from a reference type; move requires owned storage",
								severity="error",
								span=move_span,
							)
						)
						return record_expr(expr, self._unknown)
				return record_expr(expr, inner_ty)

			# Explicit copy.
			if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
				copy_span = _best_effort_span_for_expr(expr.subject)
				if copy_span.line is None:
					copy_span = _best_effort_span_for_expr(expr)
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						# Match binders may lack binding_id after normalization.
						# Accept them ONLY if the name is actually in scope as
						# a known binding — do not accept arbitrary names.
						name = getattr(hv, "name", None)
						if name is not None:
							for _scope in reversed(scope_env):
								if name in _scope:
									return PlaceBase(kind=PlaceKind.LOCAL, local_id=0, name=name)
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				place = place_from_expr(expr.subject, base_lookup=_base_lookup)
				if place is None:
					diagnostics.append(
						_tc_diag(
							message="copy operand must be an addressable place in v1 (local/param/field/index)",
							severity="error",
							span=copy_span,
						)
					)
					return record_expr(expr, self._unknown)
				inner_ty = type_expr(expr.subject, used_as_value=False, expected_type=expected_type)
				if inner_ty is not None:
					copy_status = self.type_table.copy_status(inner_ty)
					if copy_status is None:
						pretty = self._pretty_type_name(inner_ty, current_module=current_module_name)
						reason = self.type_table.copy_unknown_reason(inner_ty)
						diagnostics.append(
							_tc_diag(
								message=f"cannot copy value of type '{pretty}': Copy is unknown ({reason})",
								code="E-COPY-UNKNOWN",
								severity="error",
								span=copy_span,
							)
						)
						return record_expr(expr, self._unknown)
					if not copy_status:
						pretty = self._pretty_type_name(inner_ty, current_module=current_module_name)
						diagnostics.append(
							_tc_diag(
								message=f"cannot copy value of type '{pretty}': type is not Copy",
								severity="error",
								span=copy_span,
							)
						)
						return record_expr(expr, self._unknown)
				return record_expr(expr, inner_ty)

			# Calls.
			if isinstance(expr, H.HCall):
				# `share x` expression form (0.31.20): the AST→HIR
				# desugaring at `stage1/ast_to_hir.py::_visit_expr_Share`
				# synthesizes
				# `HCall(HQualifiedMember(Share-trait, "share"), [HBorrow(<local>)])`
				# with `origin="share_expr"` (NAME subject) or
				# `origin="share_expr_non_local"` (non-NAME subject).
				# Origin is used as the dispatch key because dynamic
				# attributes would be dropped by `normalize.py`'s
				# HCall rebuild — the `origin` dataclass field
				# survives normalization.
				#
				# This block emits the source-form-keyed diagnostics
				# (`E-SHARE-EXPR-SUBJECT-NOT-LOCAL`,
				# `E-SHARE-EXPR-NOT-SHARE`) BEFORE the trait-
				# resolution pipeline runs, so the user gets a clean
				# message instead of a raw "no matching method" or
				# "cannot copy value of type" complaint.
				#
				# Mirrors `E-CAPTURE-SHARE-NOT-SHARE` at lines
				# 6126-6164 above (lambda capture-share form), with
				# the call-site ergonomics:
				#   - non-NAME subject  → SUBJECT-NOT-LOCAL
				#   - Copy subject      → NOT-SHARE (copy hint)
				#   - non-Share subject → NOT-SHARE (move hint)
				_share_origin = getattr(expr, "origin", None)
				if _share_origin in ("share_expr", "share_expr_non_local"):
					# Dedup: `resolve_call_expr`'s fallback / retry paths
					# can re-enter type_expr on the SAME HCall object
					# (verified: same id() across visits within one
					# normalize-pass).  Without this guard, the
					# diagnostic fires twice per source `share x`.
					# We use a dynamic attribute because dedup is
					# per-HCall-instance — `normalize.py`'s rebuild
					# creates a new HCall, which gets its own dedup
					# slot by virtue of being a new object; the old
					# instance's flag is no longer reachable.
					if getattr(expr, "_share_expr_diagnosed", False):
						return record_expr(expr, self._unknown)
					_share_span = getattr(expr, "loc", None) or Span()
					if _share_origin == "share_expr_non_local":
						diagnostics.append(
							_tc_diag(
								message=(
									"E-SHARE-EXPR-SUBJECT-NOT-LOCAL: "
									"`share <expr>` requires a local "
									"binding subject (e.g. `share app`); "
									"computed expressions, calls, and "
									"projections are not supported in v1. "
									"Bind the value first: "
									"`val a = <expr>; share a;`"
								),
								severity="error",
								span=_share_span,
							)
						)
						expr._share_expr_diagnosed = True
						return record_expr(expr, self._unknown)
					# Subject is a NAME — extract the binding via the
					# HBorrow → HPlaceExpr → HVar chain that
					# `_visit_expr_Share` builds.
					_share_root_ty = self._unknown
					_share_root_name: str | None = None
					try:
						_borrow_arg = expr.args[0] if expr.args else None
						_place = getattr(_borrow_arg, "subject", None) if _borrow_arg is not None else None
						_base = getattr(_place, "base", None) if _place is not None else None
						_share_bid = getattr(_base, "binding_id", None) if _base is not None else None
						_share_root_name = getattr(_base, "name", None) if _base is not None else None
						if _share_bid is not None:
							_share_root_ty = binding_types.get(_share_bid, self._unknown)
					except Exception:
						pass
					if _share_root_ty != self._unknown:
						_implements_share = False
						try:
							_implements_share = bool(self.type_table.is_share(_share_root_ty))
						except Exception:
							_implements_share = False
						if not _implements_share:
							_ty_name = self.type_table.get(_share_root_ty).name or f"typeid={_share_root_ty}"
							_disp_name = _share_root_name or "x"
							_is_copy = False
							try:
								_is_copy = bool(self.type_table.copy_status(_share_root_ty))
							except Exception:
								_is_copy = False
							if _is_copy:
								diagnostics.append(
									_tc_diag(
										message=(
											f"E-SHARE-EXPR-NOT-SHARE: type "
											f"'{_ty_name}' is `Copy`, not "
											f"`Share`. For value-like "
											f"duplication, use "
											f"`copy {_disp_name}`. "
											f"`share` is for non-Copy "
											f"shared-owner types (e.g. "
											f"`Arc<T>`)."
										),
										severity="error",
										span=_share_span,
									)
								)
							else:
								diagnostics.append(
									_tc_diag(
										message=(
											f"E-SHARE-EXPR-NOT-SHARE: type "
											f"'{_ty_name}' does not "
											f"implement "
											f"`std.core.shareable.Share`. "
											f"To transfer ownership, use "
											f"`move {_disp_name}`. To "
											f"enable share-expr for "
											f"'{_ty_name}', implement "
											f"`std.core.shareable.Share` "
											f"for it (an inherent "
											f"`.share()` method does NOT "
											f"satisfy `share x`)."
										),
										severity="error",
										span=_share_span,
									)
								)
							expr._share_expr_diagnosed = True
							return record_expr(expr, self._unknown)
				if isinstance(expr.fn, H.HVar) and expr.fn.binding_id is not None:
					pending = pending_lambda_by_binding.get(expr.fn.binding_id)
					if pending is not None:
						setattr(pending, "expected_fn_inferred", True)
						arg_types = [type_expr(a) for a in expr.args]
						fn_params = [t if t is not None else self._unknown for t in arg_types]
						fn_ret = expected_type if expected_type is not None else self._unknown
						fn_ty = self.type_table.ensure_function(fn_params, fn_ret, can_throw=True)
						typed = type_expr(pending, expected_type=fn_ty)
						if typed is not None:
							td = self.type_table.get(typed)
							if td.kind is TypeKind.FUNCTION and getattr(pending, "can_throw_effective", None) is False and td.can_throw():
								params = list(td.param_types[:-1]) if td.param_types else []
								ret = td.param_types[-1] if td.param_types else self._unknown
								typed = self.type_table.ensure_function(params, ret, can_throw=False)
						binding_types[expr.fn.binding_id] = typed if typed is not None else self._unknown
						pending_lambda_by_binding.pop(expr.fn.binding_id, None)
				preseed = preseed_type_params or {}
				call_ctx = make_call_ctx(type_table=self.type_table, diagnostics=diagnostics, current_module_name=current_module_name, current_module=current_module, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, preseed_type_params=preseed, type_param_names=type_param_names, current_fn_id=fn_id, int_ty=self._int, uint_ty=self._uint, uint64_ty=self._uint64, byte_ty=self.type_table.ensure_byte(), bool_ty=self._bool, float_ty=self._float, string_ty=self._string, void_ty=self._void, error_ty=self._error, dv_ty=self._dv, unknown_ty=self._unknown, signatures_by_id=signatures_by_id, callable_registry=callable_registry, trait_index=trait_index, trait_impl_index=trait_impl_index, impl_index=impl_index, visible_modules=visible_modules, visible_trait_world=visible_trait_world, global_trait_world=global_trait_world, trait_scope_by_module=trait_scope_by_module, require_env_local=require_env_local, fn_require_assumed=fn_require_assumed, binding_mutable=binding_mutable, binding_id_by_name={name: bid for bid, name in binding_names.items()}, traits_in_scope=_traits_in_scope, trait_key_for_id=trait_key_for_id, tc_diag=_tc_diag, type_expr=type_expr, optional_variant_type=self._optional_variant_type, unwrap_ref_type=_unwrap_ref_type, struct_base_and_args=_struct_base_and_args, receiver_place=_receiver_place, receiver_can_mut_borrow=_receiver_can_mut_borrow, receiver_compat=_receiver_compat, receiver_preference=_receiver_preference, args_match_params=_args_match_params, coerce_args_for_params=_coerce_args_for_params, infer_receiver_arg_type=_infer_receiver_arg_type, instantiate_sig_with_subst=_instantiate_sig_with_subst, apply_autoborrow_args=_apply_autoborrow_args, label_typeid=_label_typeid, trait_label=_trait_label, require_for_fn=_require_for_fn, extract_conjunctive_facts=_extract_conjunctive_facts, subject_name=_subject_name, normalize_type_key=_normalize_type_key, collect_trait_subjects=_collect_trait_subjects, require_failure=_require_failure, format_failure_message=_format_failure_message, failure_code=_failure_code, requirement_notes=_requirement_notes, pick_best_failure=_pick_best_failure, param_scope_map=_param_scope_map, candidate_key_for_decl=_candidate_key_for_decl, visibility_note=_visibility_note, intrinsic_method_fn_id=_intrinsic_method_fn_id, instantiate_sig=_instantiate_sig, self_mode_from_sig=_self_mode_from_sig, match_impl_type_args=_match_impl_type_args, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=self._pretty_type_name, format_ctor_signature_list=self._format_ctor_signature_list, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw, record_call_resolution=record_call_resolution, record_iface_coercion=record_iface_coercion, iface_assignable=iface_assignable, record_instantiation=record_instantiation, alloc_callsite_id=_alloc_callsite_id, alloc_node_id=_assign_node_id, allow_unsafe=unsafe_allowed_module, unsafe_context=unsafe_context, allow_unsafe_without_block=allow_unsafe_without_block_local, allow_rawbuffer=self._is_toolchain_trusted_module(current_module_name))
				return resolve_call_expr(call_ctx, expr, expected_type, record_expr=record_expr, record_call_info=record_call_info, record_invoke_call_info=record_invoke_call_info)
			if isinstance(expr, getattr(H, "HInvoke", ())):
				if drift_debug.enabled("call"):
					try:
						import sys
						print(f"[debug] HInvoke in {function_symbol(fn_id)} callee={expr.callee}", file=sys.stderr)
					except Exception:
						pass
				arg_types = [type_expr(a) for a in expr.args]
				if isinstance(expr.callee, H.HVar) and expr.callee.binding_id is not None:
					pending = pending_lambda_by_binding.get(expr.callee.binding_id)
					if pending is not None:
						setattr(pending, "expected_fn_inferred", True)
						fn_params = [t if t is not None else self._unknown for t in arg_types]
						fn_ret = expected_type if expected_type is not None else self._unknown
						fn_ty = self.type_table.ensure_function(fn_params, fn_ret, can_throw=True)
						typed = type_expr(pending, expected_type=fn_ty)
						if typed is not None:
							td = self.type_table.get(typed)
							if td.kind is TypeKind.FUNCTION and getattr(pending, "can_throw_effective", None) is False and td.can_throw():
								params = list(td.param_types[:-1]) if td.param_types else []
								ret = td.param_types[-1] if td.param_types else self._unknown
								typed = self.type_table.ensure_function(params, ret, can_throw=False)
						binding_types[expr.callee.binding_id] = typed if typed is not None else self._unknown
						pending_lambda_by_binding.pop(expr.callee.binding_id, None)
				kw_pairs = list(getattr(expr, "kwargs", []) or [])
				if getattr(expr, "type_args", None):
					diagnostics.append(
						_tc_diag(
							message="type arguments are not supported on function values; apply them on the named function",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if kw_pairs:
					diagnostics.append(
						_tc_diag(
							message="keyword arguments are not supported on function values in v1",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				callee_expected: TypeId | None = None
				if isinstance(expr.callee, H.HLambda):
					fn_params = [t if t is not None else self._unknown for t in arg_types]
					fn_ret = expected_type if expected_type is not None else self._unknown
					callee_expected = self.type_table.ensure_function(fn_params, fn_ret, can_throw=True)
					expr.callee.allow_capture_invoke = True
				callee_ty = type_expr(expr.callee, expected_type=callee_expected)
				if callee_ty is None:
					return record_expr(expr, self._unknown)
				callee_def = self.type_table.get(callee_ty)
				if callee_def.kind is TypeKind.FUNCTION:
					fn_params = list(callee_def.param_types[:-1]) if callee_def.param_types else []
					fn_ret = callee_def.param_types[-1] if callee_def.param_types else self._unknown
					if len(fn_params) != len(arg_types):
						diagnostics.append(
							_tc_diag(
								message=f"function value expects {len(fn_params)} arguments, got {len(arg_types)}",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, fn_ret)
					for want, have in zip(fn_params, arg_types):
						if have is not None and want != have:
							diagnostics.append(
								_tc_diag(
									message=(
										f"function value argument type mismatch (have {self.type_table.get(have).name}, "
										f"expected {self.type_table.get(want).name})"
									),
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
					invoke_can_throw = callee_def.can_throw()
					if isinstance(expr.callee, H.HLambda) and getattr(expr.callee, "can_throw_effective", None) is not None:
						invoke_can_throw = bool(expr.callee.can_throw_effective)
					record_invoke_call_info(
						expr,
						param_types=fn_params,
						return_type=fn_ret,
						can_throw=invoke_can_throw,
					)
					return record_expr(expr, fn_ret)
				callable_kind = getattr(TypeKind, "CALLABLE", None)
				callable_dyn_kind = getattr(TypeKind, "CALLABLE_DYN", None)
				if callable_kind is not None and callable_dyn_kind is not None and callee_def.kind in (callable_kind, callable_dyn_kind):
					diagnostics.append(
						_tc_diag(
							message="calling Callback values is not supported yet; use FnN values in v1",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				diagnostics.append(
					_tc_diag(
						message="call target is not a function value",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				if drift_debug.enabled("call"):
					try:
						diagnostics[-1].notes.append(f"in '{function_symbol(fn_id)}'")
					except Exception:
						pass
				return record_expr(expr, self._unknown)
	
			if isinstance(expr, H.HTryExpr):
				attempt_ty = type_expr(expr.attempt)
				result_ty = attempt_ty
				if attempt_ty is not None:
					td_attempt = self.type_table.get(attempt_ty)
					if td_attempt.kind is TypeKind.FNRESULT and td_attempt.param_types:
						result_ty = td_attempt.param_types[0]
				for arm in expr.arms:
					catch_depth += 1
					scope_env.append(dict())
					scope_bindings.append(dict())
					try:
						if arm.binder:
							bid = self._alloc_local_id()
							locals.append(bid)
							scope_env[-1][arm.binder] = self._error
							scope_bindings[-1][arm.binder] = bid
							binding_types[bid] = self._error
							binding_names[bid] = arm.binder
							binding_mutable[bid] = False
							binding_place_kind[bid] = PlaceKind.LOCAL
						type_block_in_scope(arm.block)
						if arm.result is not None:
							type_expr(arm.result, expected_type=result_ty)
					finally:
						scope_env.pop()
						scope_bindings.pop()
						catch_depth -= 1
				return record_expr(expr, result_ty or self._unknown)

			if hasattr(H, "HUnsafeExpr") and isinstance(expr, getattr(H, "HUnsafeExpr")):
				if not unsafe_allowed_module:
					diagnostics.append(_tc_diag(message="unsafe block requires --allow-unsafe", severity="error", span=getattr(expr, "loc", Span())))
				prev_unsafe = unsafe_context
				unsafe_context = True
				scope_env.append(dict())
				scope_bindings.append(dict())
				try:
					type_block_in_scope(expr.body)
					result_ty = type_expr(expr.result)
				finally:
					unsafe_context = prev_unsafe
					scope_env.pop()
					scope_bindings.pop()
				return record_expr(expr, result_ty)

			if isinstance(expr, H.HMethodCall):
				if isinstance(expr.receiver, H.HVar) and expr.receiver.binding_id is None:
					recv_name = getattr(expr.receiver, "name", None)
					if recv_name is not None and module_ids_by_name is not None and recv_name in module_ids_by_name:
						free_call = H.HCall(
							fn=H.HVar(name=expr.method_name, binding_id=None, module_id=recv_name),
							args=list(expr.args),
							kwargs=list(getattr(expr, "kwargs", []) or []),
							type_args=list(getattr(expr, "type_args", []) or []) if getattr(expr, "type_args", None) else None,
							callsite_id=getattr(expr, "callsite_id", None),
						)
						return type_expr(free_call, expected_type=expected_type)
				preseed = preseed_type_params or {}
				call_ctx = make_call_ctx(type_table=self.type_table, diagnostics=diagnostics, current_module_name=current_module_name, current_module=current_module, default_package=default_package, module_packages=module_packages, type_param_map=type_param_map, preseed_type_params=preseed, type_param_names=type_param_names, current_fn_id=fn_id, int_ty=self._int, uint_ty=self._uint, uint64_ty=self._uint64, byte_ty=self.type_table.ensure_byte(), bool_ty=self._bool, float_ty=self._float, string_ty=self._string, void_ty=self._void, error_ty=self._error, dv_ty=self._dv, unknown_ty=self._unknown, signatures_by_id=signatures_by_id, callable_registry=callable_registry, trait_index=trait_index, trait_impl_index=trait_impl_index, impl_index=impl_index, visible_modules=visible_modules, visible_trait_world=visible_trait_world, global_trait_world=global_trait_world, trait_scope_by_module=trait_scope_by_module, require_env_local=require_env_local, fn_require_assumed=fn_require_assumed, binding_mutable=binding_mutable, binding_id_by_name={name: bid for bid, name in binding_names.items()}, traits_in_scope=_traits_in_scope, trait_key_for_id=trait_key_for_id, tc_diag=_tc_diag, type_expr=type_expr, optional_variant_type=self._optional_variant_type, unwrap_ref_type=_unwrap_ref_type, struct_base_and_args=_struct_base_and_args, receiver_place=_receiver_place, receiver_can_mut_borrow=_receiver_can_mut_borrow, receiver_compat=_receiver_compat, receiver_preference=_receiver_preference, args_match_params=_args_match_params, coerce_args_for_params=_coerce_args_for_params, infer_receiver_arg_type=_infer_receiver_arg_type, instantiate_sig_with_subst=_instantiate_sig_with_subst, apply_autoborrow_args=_apply_autoborrow_args, label_typeid=_label_typeid, trait_label=_trait_label, require_for_fn=_require_for_fn, extract_conjunctive_facts=_extract_conjunctive_facts, subject_name=_subject_name, normalize_type_key=_normalize_type_key, collect_trait_subjects=_collect_trait_subjects, require_failure=_require_failure, format_failure_message=_format_failure_message, failure_code=_failure_code, requirement_notes=_requirement_notes, pick_best_failure=_pick_best_failure, param_scope_map=_param_scope_map, candidate_key_for_decl=_candidate_key_for_decl, visibility_note=_visibility_note, intrinsic_method_fn_id=_intrinsic_method_fn_id, instantiate_sig=_instantiate_sig, self_mode_from_sig=_self_mode_from_sig, match_impl_type_args=_match_impl_type_args, fixed_width_allowed=_fixed_width_allowed, reject_zst_array=_reject_zst_array, pretty_type_name=self._pretty_type_name, format_ctor_signature_list=self._format_ctor_signature_list, enforce_struct_requires=_enforce_struct_requires, ensure_field_visible=_ensure_field_visible, visible_modules_for_free_call=_visible_modules_for_free_call, module_ids_by_name=module_ids_by_name, visibility_provenance=visibility_provenance, infer=_infer, format_infer_failure=_format_infer_failure, lambda_can_throw=_lambda_can_throw, record_call_resolution=record_call_resolution, record_iface_coercion=record_iface_coercion, iface_assignable=iface_assignable, record_instantiation=record_instantiation, alloc_callsite_id=_alloc_callsite_id, alloc_node_id=_assign_node_id, allow_unsafe=unsafe_allowed_module, unsafe_context=unsafe_context, allow_unsafe_without_block=allow_unsafe_without_block_local, allow_rawbuffer=self._is_toolchain_trusted_module(current_module_name))
				method_ctx = make_method_ctx(call_ctx, diagnostics=diagnostics, traits_in_scope=_traits_in_scope, trait_key=None)
				method_res = resolve_method_call(method_ctx, expr, expected_type=expected_type)
				if method_res.call_info is None and method_res.resolution is not None and getattr(method_res.resolution, "decl", None) is not None:
					decl = method_res.resolution.decl
					fn_id_local = getattr(decl, "fn_id", None)
					if fn_id_local is not None:
						sig_for_throw = signatures_by_id.get(fn_id_local) if signatures_by_id is not None else None
						fallback_param_types = tuple(decl.signature.param_types) if decl.signature is not None else tuple()
						fallback_ret = decl.signature.result_type if decl.signature is not None else self._unknown
						if sig_for_throw is not None:
							if sig_for_throw.param_type_ids:
								fallback_param_types = tuple(sig_for_throw.param_type_ids)
							if sig_for_throw.return_type_id is not None:
								fallback_ret = sig_for_throw.return_type_id
						fallback_can_throw = bool(getattr(sig_for_throw, "declared_can_throw", False)) if sig_for_throw is not None else False
						fallback_terminal = bool(getattr(sig_for_throw, "declared_terminal_throws", False)) if sig_for_throw is not None else False
						method_res = MethodCallResult(
							method_res.return_type,
							CallInfo(
								target=CallTarget.direct(fn_id_local),
								sig=CallSig(
									param_types=fallback_param_types,
									user_ret_type=fallback_ret,
									can_throw=fallback_can_throw,
									includes_callee=False,
									declared_terminal_throws=fallback_terminal,
								),
							),
							method_res.resolution,
						)
				if (
					method_res.call_info is not None
					and method_res.resolution is not None
					and getattr(method_res.resolution, "decl", None) is not None
				):
					decl = method_res.resolution.decl
					fn_id_local = getattr(decl, "fn_id", None)
					if fn_id_local is not None and method_res.call_info.target.kind is CallTargetKind.INDIRECT:
						method_res = MethodCallResult(
							method_res.return_type,
							CallInfo(
								target=CallTarget.direct(fn_id_local),
								sig=CallSig(
									param_types=method_res.call_info.sig.param_types,
									user_ret_type=method_res.call_info.sig.user_ret_type,
									can_throw=bool(method_res.call_info.sig.can_throw),
									includes_callee=method_res.call_info.sig.includes_callee,
									declared_terminal_throws=method_res.call_info.sig.declared_terminal_throws,
								),
							),
							method_res.resolution,
						)
				# Arc runtime boundary (Stage 2) — method-resolution
				# intrinsic target rewrite.
				#
				# When the resolved method is `@intrinsic` with
				# `sig.intrinsic_kind` set (Arc.clone / Arc.get /
				# Arc::Destructible::destroy / Arc.as_interface),
				# the DIRECT target points at a bodyless template.
				# Rewriting to INTRINSIC lets
				# `_lower_method_call_with_info` redirect to the
				# `_arc_*_impl<T>` helper (concrete T, Stage 2) or
				# the fat-handle lowering (Stage 3).
				#
				# Must stay in sync with every `call_info_by_callsite_id`
				# override site — see `_write_call_info_respecting_intrinsic`.
				if (
					method_res.call_info is not None
					and method_res.resolution is not None
					and getattr(method_res.resolution, "decl", None) is not None
					and method_res.call_info.target.kind is CallTargetKind.DIRECT
				):
					_arc_decl = method_res.resolution.decl
					_arc_fn_id = getattr(_arc_decl, "fn_id", None)
					_arc_sig = signatures_by_id.get(_arc_fn_id) if _arc_fn_id is not None and signatures_by_id is not None else None
					if _arc_sig is not None and bool(getattr(_arc_sig, "is_intrinsic", False)):
						_arc_intrinsic_kind = getattr(_arc_sig, "intrinsic_kind", None)
						if _arc_intrinsic_kind is not None:
							method_res = MethodCallResult(
								method_res.return_type,
								CallInfo(
									target=CallTarget.intrinsic(_arc_intrinsic_kind),
									sig=method_res.call_info.sig,
								),
								method_res.resolution,
							)
				if method_res.call_info is not None:
					if method_res.resolution is not None and getattr(method_res.resolution, "decl", None) is not None:
						decl_self_mode = getattr(method_res.resolution.decl, "self_mode", None)
						autoborrow_mode = getattr(method_res.resolution, "receiver_autoborrow", None)
						if decl_self_mode is SelfMode.SELF_BY_REF and autoborrow_mode is None:
							recv_place = place_expr_from_lvalue_expr(expr.receiver)
							if recv_place is None and not isinstance(expr.receiver, H.HBorrow):
								diagnostics.append(
									_tc_diag(
										message="borrow requires an addressable place; bind to a local first",
										severity="error",
										phase="typecheck",
										span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
									)
								)
					if method_res.resolution is not None and getattr(method_res.resolution, "receiver_autoborrow", None) is not None:
						receiver_mode = method_res.resolution.receiver_autoborrow
						is_mut = receiver_mode is SelfMode.SELF_BY_REF_MUT
						if not is_mut:
							recv_place_expr = place_expr_from_lvalue_expr(expr.receiver)
							allow_rvalue_receiver = isinstance(expr.receiver, (H.HCall, H.HMethodCall, H.HInvoke))
							if recv_place_expr is None and not allow_rvalue_receiver and not isinstance(expr.receiver, H.HBorrow):
								diagnostics.append(
									_tc_diag(
										message="borrow requires an addressable place; bind to a local first",
										severity="error",
										phase="typecheck",
										span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
									)
								)
						recv_ty = type_expr(expr.receiver, used_as_value=False)
						if recv_ty is not None:
							ref_info = _ref_param_info(recv_ty)
							if ref_info is not None:
								ref_mut, _inner = ref_info
								if not is_mut or ref_mut:
									pass
								else:
									ref_info = None
							if ref_info is not None:
								pass
							else:
								place_expr = place_expr_from_lvalue_expr(expr.receiver)
								if place_expr is None:
									if is_mut:
										diagnostics.append(
											_tc_diag(
												message="borrow requires an addressable place; bind to a local first",
												severity="error",
												phase="typecheck",
												span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
											)
										)
									else:
										allow_rvalue_receiver = isinstance(expr.receiver, (H.HCall, H.HMethodCall, H.HInvoke))
										if allow_rvalue_receiver:
											borrow_expr = H.HBorrow(subject=expr.receiver, is_mut=False, allow_rvalue=True)
											_assign_node_id(borrow_expr)
											expr.receiver = borrow_expr
											type_expr(borrow_expr)
										else:
											diagnostics.append(
												_tc_diag(
													message="borrow requires an addressable place; bind to a local first",
													severity="error",
													phase="typecheck",
													span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
												)
											)
								else:
									_assign_place_expr_ids(place_expr)
									borrow_expr = H.HBorrow(subject=place_expr, is_mut=is_mut)
									_assign_node_id(borrow_expr)
									expr.receiver = borrow_expr
									type_expr(borrow_expr)
					param_types = list(method_res.call_info.sig.param_types)
					if method_res.call_info.sig.includes_callee:
						param_types = param_types[1:]
					csid = getattr(expr, "callsite_id", None)
					fn_id_local = None
					decl_sig: FnSignature | None = None
					if method_res.resolution is not None and getattr(method_res.resolution, "decl", None) is not None:
						fn_id_local = getattr(method_res.resolution.decl, "fn_id", None)
					if fn_id_local is None and method_res.call_info is not None and method_res.call_info.target.kind is CallTargetKind.DIRECT:
						fn_id_local = method_res.call_info.target.symbol
					if fn_id_local is not None and signatures_by_id is not None:
						decl_sig = signatures_by_id.get(fn_id_local)
					if fn_id_local is not None:
						req = _require_for_fn(fn_id_local)
						if req is not None:
							local_type_params: dict[str, TypeId] = {}
							if signatures_by_id is not None and isinstance(csid, int):
								sig = signatures_by_id.get(fn_id_local)
								inst = instantiations_by_callsite_id.get(csid)
								if sig is not None and inst is not None:
									tp_names: list[str] = []
									for tp in (getattr(sig, "type_params", []) or []):
										tp_names.append(tp.name)
									for name, arg in zip(tp_names, getattr(inst, "type_args", ()) or ()):
										local_type_params[name] = arg
							if decl_sig is not None:
								recv_ty = type_expr(expr.receiver, used_as_value=False)
								impl_target = getattr(decl_sig, "impl_target_type_id", None)
								if impl_target is not None and recv_ty is not None:
									_base_id, base_args = _struct_base_and_args(_unwrap_ref_type(recv_ty))
									for tp, arg in zip(getattr(decl_sig, "impl_type_params", []) or [], base_args):
										if tp.name not in local_type_params:
											local_type_params[tp.name] = arg
									base_def = self.type_table.get(_base_id)
									base_param_names: list[str] = []
									if base_def.kind is TypeKind.VARIANT:
										schema = self.type_table.variant_schemas.get(_base_id)
										if schema is not None:
											base_param_names = list(schema.type_params)
									elif base_def.kind is TypeKind.STRUCT:
										schema = self.type_table.struct_bases.get(_base_id)
										if schema is not None:
											base_param_names = list(schema.type_params)
									elif base_def.kind is TypeKind.INTERFACE:
										schema = self.type_table.interface_bases.get(_base_id)
										if schema is not None:
											base_param_names = list(schema.type_params)
									if base_param_names:
										for name, arg in zip(base_param_names, base_args):
											if name not in local_type_params:
												local_type_params[name] = arg
							facts = _extract_conjunctive_facts(req)
							if facts:
								for idx, arg in enumerate(expr.args):
									if idx >= len(param_types) or not isinstance(arg, H.HLambda):
										continue
									td = self.type_table.get(param_types[idx])
									sig_param_tp: TypeParamId | None = None
									if sig is not None and sig.param_type_ids:
										sig_idx = idx
										if sig.param_names and sig.param_names[0] == "self":
											sig_idx = idx + 1
										if sig_idx < len(sig.param_type_ids):
											sig_td = self.type_table.get(sig.param_type_ids[sig_idx])
											if sig_td.kind is TypeKind.TYPEVAR:
												sig_param_tp = sig_td.type_param_id
									if td.kind is TypeKind.TYPEVAR and td.type_param_id is not None:
										sig_param_tp = td.type_param_id
									for atom in facts:
										if not isinstance(atom, parser_ast.TraitIs):
											continue
										trait_name = getattr(atom.trait, "name", None)
										exp = _fn_trait_expected(trait_name) if trait_name is not None else None
										if exp is None:
											continue
										expected_arity, trait_can_throw = exp
										subj = atom.subject
										subj_name = _subject_name(subj)
										if isinstance(subj, TypeParamId):
											if sig_param_tp is None or subj != sig_param_tp:
												continue
										elif subj_name is not None:
											if sig_param_tp is None or subj_name != (type_param_names.get(sig_param_tp) or ""):
												continue
										else:
											continue
										trait_args = list(getattr(atom.trait, "args", []) or [])
										if len(trait_args) != expected_arity:
											continue
										arg_types: list[TypeId] = []
										for targ in trait_args:
											try:
												arg_types.append(resolve_opaque_type(targ, self.type_table, module_id=fn_id_local.module or current_module_name, type_params=local_type_params))
											except Exception:
												arg_types.append(self._unknown)
										if not arg_types:
											continue
										ret_ty = arg_types[-1]
										param_tys = arg_types[:-1]
										param_types[idx] = self.type_table.ensure_function(param_tys, ret_ty, can_throw=trait_can_throw)
										arg.expected_fn_inferred = True
										break
					for idx, arg in enumerate(expr.args):
						if idx < len(param_types):
							type_expr(arg, expected_type=param_types[idx], used_as_value=False)
				if method_res.call_info is not None and method_res.resolution is not None and getattr(method_res.resolution, "decl", None) is not None:
					decl = method_res.resolution.decl
					fn_id_local = getattr(decl, "fn_id", None)
					if fn_id_local is not None and method_res.call_info.target.kind is CallTargetKind.DIRECT:
						sig_for_throw = signatures_by_id.get(fn_id_local) if signatures_by_id is not None else None
						boundary = _apply_method_boundary(expr, target_fn_id=fn_id_local, sig_for_throw=sig_for_throw, call_can_throw=method_res.call_info.sig.can_throw)
						if boundary is None:
							return record_expr(expr, self._unknown)
						wrap_id, call_can_throw = boundary
						if wrap_id != fn_id_local or call_can_throw != method_res.call_info.sig.can_throw:
							method_res = MethodCallResult(method_res.return_type, CallInfo(target=CallTarget.direct(wrap_id), sig=CallSig(param_types=method_res.call_info.sig.param_types, user_ret_type=method_res.call_info.sig.user_ret_type, can_throw=bool(call_can_throw), includes_callee=method_res.call_info.sig.includes_callee, declared_terminal_throws=method_res.call_info.sig.declared_terminal_throws)), method_res.resolution)
				if method_res.resolution is not None:
					call_resolutions[expr.node_id] = method_res.resolution
				csid = getattr(expr, "callsite_id", None)
				if method_res.call_info is not None:
					if isinstance(csid, int):
						if self.type_table is not None and self.type_table.type_provenance_enabled():
							span = getattr(expr, "loc", None)
							note = f"callsite:{getattr(expr, 'callsite_id', None)}"
							for tid in method_res.call_info.sig.param_types:
								self.type_table.record_type_provenance(
									tid,
									phase="typecheck",
									kind="call_param",
									span=span,
									note=note,
								)
							self.type_table.record_type_provenance(
								method_res.call_info.sig.user_ret_type,
								phase="typecheck",
								kind="call_ret",
								span=span,
								note=note,
							)
						csid = _record_call_info(expr, method_res.call_info)
						inst = instantiations_by_callsite_id.get(csid)
						if inst is not None:
							key = getattr(inst, "target_key", None)
							type_args = tuple(getattr(inst, "type_args", ()) or ())
							if isinstance(key, FunctionKey) and type_args:
								inst_key = build_instantiation_key(
									key,
									type_args,
									type_table=self.type_table,
									can_throw=bool(method_res.call_info.sig.can_throw),
								)
								# Arc runtime boundary: skip the
								# Direct(inst_fn_id) overwrite when
								# the template is `@intrinsic` —
								# keeps the CallInfo at
								# `CallTarget.intrinsic(...)` set
								# by the method-resolution rewrite
								# above.  Sibling to the same skip in
								# the free-function record path.
								_mth_rec_intrinsic = False
								if signatures_by_id is not None:
									for _fid2, _sig2 in signatures_by_id.items():
										if _fid2.module != key.module_path:
											continue
										if _fid2.name != key.name:
											continue
										if bool(getattr(_sig2, "is_intrinsic", False)):
											_mth_rec_intrinsic = True
										break
								if not _mth_rec_intrinsic:
									inst_name = f"{key.name}__inst__{instantiation_key_hash(inst_key)}"
									inst_fn_id = FunctionId(module=key.module_path, name=inst_name, ordinal=0)
									inst_can_throw = method_res.call_info.sig.can_throw
									if signatures_by_id is not None and method_res.resolution is not None:
										base_fn_id = getattr(method_res.resolution.decl, "fn_id", None)
										sig_for_throw = signatures_by_id.get(base_fn_id) if base_fn_id is not None else None
										if sig_for_throw is not None and sig_for_throw.declared_can_throw is not None:
											inst_can_throw = bool(sig_for_throw.declared_can_throw)
									call_info_by_callsite_id[csid] = CallInfo(
										target=CallTarget.direct(inst_fn_id),
										sig=CallSig(
											param_types=method_res.call_info.sig.param_types,
											user_ret_type=method_res.call_info.sig.user_ret_type,
											can_throw=bool(inst_can_throw),
											includes_callee=method_res.call_info.sig.includes_callee,
											declared_terminal_throws=method_res.call_info.sig.declared_terminal_throws,
										),
									)
					elif callable_registry is not None:
						diagnostics.append(_tc_diag(message="internal: missing callsite_id on method call node", severity="error", span=getattr(expr, "loc", Span())))
				return record_expr(expr, method_res.return_type)

			if isinstance(expr, H.HField):
				# Suppress Copy checks on the element type when projecting a field
				# through array indexing (entries[i].name): the element is borrowed
				# for field access, not copied.  Scoped via try/finally to avoid
				# leaking the exemption beyond this field projection.
				_field_suppress_id = None
				if isinstance(expr.subject, H.HIndex):
					_idx_subj_ty_hint = None
					_idx_subj = expr.subject.subject
					if hasattr(_idx_subj, "node_id") and _idx_subj.node_id in expr_types:
						_idx_subj_ty_hint = expr_types[_idx_subj.node_id]
					else:
						_idx_subj_ty_hint = type_expr(_idx_subj, used_as_value=False)
					if _idx_subj_ty_hint is not None:
						_arr_def = self.type_table.get(_idx_subj_ty_hint)
						if _arr_def.kind is TypeKind.REF and _arr_def.param_types:
							_arr_def = self.type_table.get(_arr_def.param_types[0])
						if _arr_def.kind is TypeKind.ARRAY and _arr_def.param_types:
							_field_suppress_id = _arr_def.param_types[0]
							_suppress_copy_type_ids.add(_field_suppress_id)
				try:
					sub_ty = type_expr(expr.subject, used_as_value=False)
				finally:
					if _field_suppress_id is not None:
						_suppress_copy_type_ids.discard(_field_suppress_id)
				inner_ty = sub_ty
				inner_def = self.type_table.get(inner_ty)
				subject_is_ref = False
				if inner_def.kind is TypeKind.REF and inner_def.param_types:
					subject_is_ref = True
					inner_ty = inner_def.param_types[0]
					inner_def = self.type_table.get(inner_ty)
				if inner_def.kind is TypeKind.STRUCT:
					info = _resolve_struct_field_type(inner_ty, expr.name)
					if info is not None:
						_, field_ty = info
						if not _ensure_field_visible(inner_ty, expr.name, getattr(expr, "loc", Span())):
							return record_expr(expr, self._unknown)
						if subject_is_ref or _expr_reads_through_ref_projection(expr.subject):
							_require_copy_value(field_ty, span=_best_effort_span_for_expr(expr), used_as_value=used_as_value)
						return record_expr(expr, field_ty)
				if expr.name in ("len", "cap", "capacity", "gen"):
					# Array/String length/capacity/gen sugar returns Int.
					if inner_def.kind is TypeKind.ARRAY:
						return record_expr(expr, self._int)
					if expr.name == "len" and inner_ty == self._string:
						return record_expr(expr, self._int)
					if expr.name in ("cap", "capacity", "gen"):
						diagnostics.append(_tc_diag(message=f"{expr.name} is only supported on Array values", severity="error", span=getattr(expr, "loc", Span())))
					else:
						diagnostics.append(_tc_diag(message="len(x): unsupported argument type", severity="error", span=getattr(expr, "loc", Span())))
					return record_expr(expr, self._unknown)
				if expr.name == "attrs":
					diagnostics.append(
						_tc_diag(
							message='attrs must be indexed: use error.attrs["key"]',
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if expr.name == "captures":
					diagnostics.append(
						_tc_diag(
							message='captures must be indexed: use error.captures["frame"]["key"]',
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				# Struct fields: `x.field`
				if inner_def.kind is TypeKind.STRUCT:
					if not _ensure_field_visible(inner_ty, expr.name, getattr(expr, "loc", Span())):
						return record_expr(expr, self._unknown)
					info = _resolve_struct_field_type(inner_ty, expr.name)
					if info is None:
						diagnostics.append(
							_tc_diag(
								message=f"unknown field '{expr.name}' on struct '{inner_def.name}'",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					_, field_ty = info
					if subject_is_ref or _expr_reads_through_ref_projection(expr.subject):
						_require_copy_value(field_ty, span=_best_effort_span_for_expr(expr), used_as_value=used_as_value)
					return record_expr(expr, field_ty)
				return record_expr(expr, self._unknown)

			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				cur = type_expr(expr.base, used_as_value=False)
				for proj in expr.projections:
					if isinstance(proj, H.HPlaceDeref):
						if cur is None:
							return record_expr(expr, self._unknown)
						inner = _deref_inner_type(cur, span=getattr(expr, "loc", Span()))
						if inner is None:
							return record_expr(expr, self._unknown)
						cur = inner
					elif isinstance(proj, H.HPlaceField):
						if cur is None:
							return record_expr(expr, self._unknown)
						td = self.type_table.get(cur)
						if td.kind is TypeKind.REF and td.param_types:
							cur = td.param_types[0]
							td = self.type_table.get(cur)
						if td.kind is not TypeKind.STRUCT:
							diagnostics.append(_tc_diag(message="field access requires a struct value", severity="error", span=getattr(expr, "loc", Span())))
							return record_expr(expr, self._unknown)
						if not _ensure_field_visible(cur, proj.name, getattr(expr, "loc", Span())):
							return record_expr(expr, self._unknown)
						info = _resolve_struct_field_type(cur, proj.name)
						if info is None:
							diagnostics.append(_tc_diag(message=f"unknown field '{proj.name}' on struct '{td.name}'", severity="error", span=getattr(expr, "loc", Span())))
							return record_expr(expr, self._unknown)
						_, cur = info
					elif isinstance(proj, H.HPlaceIndex):
						if cur is None:
							return record_expr(expr, self._unknown)
						idx_ty = type_expr(proj.index)
						if not _require_int_index_type(idx_ty, span=getattr(proj.index, "loc", Span())):
							return record_expr(expr, self._unknown)
						elem_ty = _array_element_type(cur, span=_best_effort_span_for_expr(expr))
						if elem_ty is None:
							return record_expr(expr, self._unknown)
						cur = elem_ty
				if cur is None:
					return record_expr(expr, self._unknown)
				_require_copy_value(cur, span=_best_effort_span_for_expr(expr), used_as_value=used_as_value)
				return record_expr(expr, cur)

			if isinstance(expr, H.HIndex):
				# Special-case Error.captures["frame"]["key"] → DiagnosticValue.
				if (
					isinstance(expr.subject, H.HIndex)
					and (
						(
							isinstance(expr.subject.subject, H.HField)
							and expr.subject.subject.name == "captures"
						)
						or (
							hasattr(H, "HPlaceExpr")
							and isinstance(expr.subject.subject, getattr(H, "HPlaceExpr"))
							and len(expr.subject.subject.projections) == 1
							and isinstance(expr.subject.subject.projections[0], H.HPlaceField)
							and expr.subject.subject.projections[0].name == "captures"
						)
					)
				):
					if isinstance(expr.subject.subject, H.HField):
						err_base = expr.subject.subject.subject
					else:
						err_base = expr.subject.subject.base
					sub_ty = type_expr(err_base, used_as_value=False)
					frame_ty = type_expr(expr.subject.index)
					key_ty = type_expr(expr.index)
					sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is TypeKind.REF and sub_def.param_types:
						sub_ty = sub_def.param_types[0]
						sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is not TypeKind.ERROR:
						diagnostics.append(
							_tc_diag(
								message="captures access is only supported on Error values",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if self.type_table.get(frame_ty).name != "String":
						diagnostics.append(
							_tc_diag(
								message="Error.captures expects a String frame key",
								severity="error",
								span=getattr(expr.subject.index, "loc", Span()),
							)
						)
					if self.type_table.get(key_ty).name != "String":
						diagnostics.append(
							_tc_diag(
								message="Error.captures expects a String local key",
								severity="error",
								span=getattr(expr.index, "loc", Span()),
							)
						)
					return record_expr(expr, self._dv)
				if isinstance(expr.subject, H.HField) and expr.subject.name == "captures":
					diagnostics.append(
						_tc_diag(
							message='captures frame must be indexed by key: use error.captures["frame"]["key"]',
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if (
					hasattr(H, "HPlaceExpr")
					and isinstance(expr.subject, getattr(H, "HPlaceExpr"))
					and len(expr.subject.projections) == 1
					and isinstance(expr.subject.projections[0], H.HPlaceField)
					and expr.subject.projections[0].name == "captures"
				):
					diagnostics.append(
						_tc_diag(
							message='captures frame must be indexed by key: use error.captures["frame"]["key"]',
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				# Special-case Error.attrs["key"] → DiagnosticValue.
				if isinstance(expr.subject, H.HField) and expr.subject.name == "attrs":
					sub_ty = type_expr(expr.subject.subject, used_as_value=False)
					key_ty = type_expr(expr.index)
					sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is TypeKind.REF and sub_def.param_types:
						sub_ty = sub_def.param_types[0]
						sub_def = self.type_table.get(sub_ty)
					if sub_def.kind is not TypeKind.ERROR:
						diagnostics.append(
							_tc_diag(
								message="attrs access is only supported on Error values",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if self.type_table.get(key_ty).name != "String":
						diagnostics.append(
							_tc_diag(
								message="Error.attrs expects a String key",
								severity="error",
								span=getattr(expr, "loc", Span()),
								code="E-ERROR-ATTR-KEY-NOT-STRING",
							)
						)
					return record_expr(expr, self._dv)
				if hasattr(H, "HPlaceExpr") and isinstance(expr.subject, getattr(H, "HPlaceExpr")):
					subject = expr.subject
					for idx, proj in enumerate(subject.projections):
						if isinstance(proj, H.HPlaceField) and proj.name == "captures":
							if idx + 1 >= len(subject.projections) or not isinstance(subject.projections[idx + 1], H.HPlaceIndex):
								continue
							if idx + 2 != len(subject.projections):
								continue
							frame_ty = type_expr(subject.projections[idx + 1].index)
							sub_ty = type_expr(subject.base, used_as_value=False)
							sub_def = self.type_table.get(sub_ty)
							if sub_def.kind is TypeKind.REF and sub_def.param_types:
								sub_ty = sub_def.param_types[0]
								sub_def = self.type_table.get(sub_ty)
							if sub_def.kind is not TypeKind.ERROR:
								diagnostics.append(
									_tc_diag(
										message="captures access is only supported on Error values",
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
								return record_expr(expr, self._unknown)
							if self.type_table.get(frame_ty).name != "String":
								diagnostics.append(
									_tc_diag(
										message="Error.captures expects a String frame key",
										severity="error",
										span=getattr(subject.projections[idx + 1].index, "loc", Span()),
									)
								)
							key_ty = type_expr(expr.index)
							if self.type_table.get(key_ty).name != "String":
								diagnostics.append(
									_tc_diag(
										message="Error.captures expects a String local key",
										severity="error",
										span=getattr(expr.index, "loc", Span()),
									)
								)
							return record_expr(expr, self._dv)
						if not (isinstance(proj, H.HPlaceField) and proj.name == "attrs"):
							continue
						if idx + 1 >= len(subject.projections) or not isinstance(subject.projections[idx + 1], H.HPlaceIndex):
							continue
						if idx + 2 != len(subject.projections):
							continue
						sub_ty = type_expr(subject.base, used_as_value=False)
						sub_def = self.type_table.get(sub_ty)
						if sub_def.kind is TypeKind.REF and sub_def.param_types:
							sub_ty = sub_def.param_types[0]
							sub_def = self.type_table.get(sub_ty)
						if sub_def.kind is not TypeKind.ERROR:
							diagnostics.append(
								_tc_diag(
									message="attrs access is only supported on Error values",
									severity="error",
									span=getattr(expr, "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
						key_ty = type_expr(expr.index)
						if self.type_table.get(key_ty).name != "String":
							diagnostics.append(
								_tc_diag(
									message="Error.attrs expects a String key",
									severity="error",
									span=getattr(expr.index, "loc", Span()),
									code="E-ERROR-ATTR-KEY-NOT-STRING",
								)
							)
						return record_expr(expr, self._dv)

				sub_ty = type_expr(expr.subject, used_as_value=False)
				idx_ty = type_expr(expr.index)
				if not _require_int_index_type(idx_ty, span=getattr(expr.index, "loc", getattr(expr, "loc", Span()))):
					return record_expr(expr, self._unknown)
				elem_ty = _array_element_type(sub_ty, span=_best_effort_span_for_expr(expr))
				if elem_ty is None:
					return record_expr(expr, self._unknown)
				# Copy check for array element access is deferred to MIR lowering,
				# which can distinguish field projection (borrow) from value use (copy).
				# Standalone non-Copy element reads are still caught by the MIR lowering
				# NotImplementedError for non-Copy array index access.
				return record_expr(expr, elem_ty)

			# Disallow implicit setters; attrs require explicit runtime helpers in MIR.
			if isinstance(expr, H.HCall) and isinstance(expr.fn, H.HField) and expr.fn.name == "attrs":
				diagnostics.append(
					_tc_diag(
						message="attrs values must be DiagnosticValue; implicit setters are not supported",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			# Unary/binary ops (MVP).
			if isinstance(expr, H.HUnary):
				sub_ty = type_expr(expr.expr, used_as_value=(expr.op is not H.UnaryOp.DEREF))
				if expr.op is H.UnaryOp.NEG:
					if sub_ty in (self._uint, self._uint64):
						_ty_name = "Uint64" if sub_ty == self._uint64 else "Uint"
						diagnostics.append(_tc_diag(message=f"unary negation is not supported on unsigned type {_ty_name}", code="E-NEG-UNSIGNED", severity="error", span=getattr(expr, "loc", Span())))
						return record_expr(expr, self._unknown)
					return record_expr(expr, sub_ty if sub_ty in (self._int, self._float) else self._unknown)
				if expr.op in (H.UnaryOp.NOT,):
					return record_expr(expr, self._bool)
				if expr.op is H.UnaryOp.BIT_NOT:
					return record_expr(expr, sub_ty if sub_ty in (self._uint, self._uint64) else self._unknown)
				if expr.op is H.UnaryOp.DEREF:
					inner = _deref_inner_type(sub_ty, span=getattr(expr, "loc", Span()))
					if inner is None:
						return record_expr(expr, self._unknown)
					_require_copy_value(inner, span=getattr(expr, "loc", Span()), used_as_value=used_as_value)
					return record_expr(expr, inner)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HBinary):
				left_expr = expr.left
				right_expr = expr.right
				if isinstance(left_expr, H.HLiteralInt) and not isinstance(right_expr, H.HLiteralInt):
					right_ty = type_expr(right_expr)
					left_ty = type_expr(left_expr, expected_type=right_ty)
				elif isinstance(right_expr, H.HLiteralInt) and not isinstance(left_expr, H.HLiteralInt):
					left_ty = type_expr(left_expr)
					right_ty = type_expr(right_expr, expected_type=left_ty)
				else:
					left_ty = type_expr(left_expr)
					right_ty = type_expr(right_expr)
				# G3: after operand type-check has resolved HVar
				# binding_id, rewrite any operand that is a bare HVar
				# referring to a Copy match-arm binder
				# (`Ref<Copy>`) as `HUnary(DEREF, HVar)`.  The HIR
				# now reflects the load explicitly; HIR→MIR lowering
				# at `_visit_expr_HUnary` (DEREF case) emits a
				# `LoadRef`.  Scoped to match-arm binders only via
				# `copy_arm_binder_ids` — `&Int` from any other
				# source is unchanged.
				def _maybe_deref_arm_binder(e: object, ty: TypeId | None) -> tuple[object, TypeId | None]:
					if not isinstance(e, H.HVar):
						return e, ty
					bid = getattr(e, "binding_id", None)
					if bid is None or bid not in copy_arm_binder_ids:
						return e, ty
					# Wrap and re-type so the new node has a recorded
					# type (Int/Uint/.../Bool, the inner Copy).
					new_e = H.HUnary(op=H.UnaryOp.DEREF, expr=e)
					_assign_node_id(new_e)
					new_ty = type_expr(new_e)
					return new_e, new_ty
				expr.left, left_ty = _maybe_deref_arm_binder(expr.left, left_ty)
				expr.right, right_ty = _maybe_deref_arm_binder(expr.right, right_ty)
				if left_ty == self._string and right_ty == self._string:
					if expr.op is H.BinaryOp.ADD:
						return record_expr(expr, self._string)
					if expr.op in (
						H.BinaryOp.EQ,
						H.BinaryOp.NE,
						H.BinaryOp.LT,
						H.BinaryOp.LE,
						H.BinaryOp.GT,
						H.BinaryOp.GE,
					):
						return record_expr(expr, self._bool)
				if expr.op in (
					H.BinaryOp.ADD,
					H.BinaryOp.SUB,
					H.BinaryOp.MUL,
					H.BinaryOp.MOD,
				):
					# Arithmetic on Int/Float; MOD also on Uint.
					if left_ty == self._int and right_ty == self._int:
						return record_expr(expr, self._int)
					if left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					if left_ty == self._uint64 and right_ty == self._uint64:
						return record_expr(expr, self._uint64)
					if left_ty == self._float and right_ty == self._float:
						return record_expr(expr, self._float)
					if expr.op is H.BinaryOp.MOD and left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					if left_ty is not None and right_ty is not None and left_ty == right_ty:
						if left_ty == self._unknown:
							return record_expr(expr, self._unknown)
						if self.type_table.get(left_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message=(
									"arithmetic operators require Int/Uint/Uint64/Float operands "
									f"(have {self._pretty_type_name(left_ty, current_module=current_module_name)})"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if left_ty is not None and right_ty is not None and left_ty != right_ty:
						if left_ty == self._unknown or right_ty == self._unknown:
							return record_expr(expr, self._unknown)
						if self.type_table.get(left_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						if self.type_table.get(right_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message=(
									"arithmetic operators require matching Int/Uint/Uint64/Float operands "
									f"(have {self._pretty_type_name(left_ty, current_module=current_module_name)} "
									f"vs {self._pretty_type_name(right_ty, current_module=current_module_name)})"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					return record_expr(expr, self._unknown)
				if expr.op in (H.BinaryOp.DIV,):
					if left_ty == self._int and right_ty == self._int:
						return record_expr(expr, self._int)
					if left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					if left_ty == self._uint64 and right_ty == self._uint64:
						return record_expr(expr, self._uint64)
					if left_ty == self._float and right_ty == self._float:
						return record_expr(expr, self._float)
					if left_ty is not None and right_ty is not None and left_ty == right_ty:
						if left_ty == self._unknown:
							return record_expr(expr, self._unknown)
						if self.type_table.get(left_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message=(
									"division requires Int/Uint/Uint64/Float operands "
									f"(have {self._pretty_type_name(left_ty, current_module=current_module_name)})"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					if left_ty is not None and right_ty is not None and left_ty != right_ty:
						if left_ty == self._unknown or right_ty == self._unknown:
							return record_expr(expr, self._unknown)
						if self.type_table.get(left_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						if self.type_table.get(right_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._unknown)
						diagnostics.append(
							_tc_diag(
								message=(
									"division requires matching Int/Uint/Uint64/Float operands "
									f"(have {self._pretty_type_name(left_ty, current_module=current_module_name)} "
									f"vs {self._pretty_type_name(right_ty, current_module=current_module_name)})"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
					return record_expr(expr, self._unknown)
				if expr.op in (
					H.BinaryOp.BIT_AND,
					H.BinaryOp.BIT_OR,
					H.BinaryOp.BIT_XOR,
					H.BinaryOp.SHL,
					H.BinaryOp.SHR,
				):
					if left_ty == self._uint and right_ty == self._uint:
						return record_expr(expr, self._uint)
					if left_ty == self._uint64 and right_ty == self._uint64:
						return record_expr(expr, self._uint64)
					diagnostics.append(
						_tc_diag(
							message="bitwise operators require Uint or Uint64 operands",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if expr.op in (
					H.BinaryOp.EQ,
					H.BinaryOp.NE,
					H.BinaryOp.LT,
					H.BinaryOp.LE,
					H.BinaryOp.GT,
					H.BinaryOp.GE,
				):
					if left_ty is not None and right_ty is not None and left_ty != right_ty:
						if left_ty == self._unknown or right_ty == self._unknown:
							return record_expr(expr, self._bool)
						if self.type_table.get(left_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._bool)
						if self.type_table.get(right_ty).kind is TypeKind.TYPEVAR:
							return record_expr(expr, self._bool)
						diagnostics.append(
							_tc_diag(
								message=(
									"comparison requires matching operand types "
									f"(have {self._pretty_type_name(left_ty, current_module=current_module_name)} "
									f"vs {self._pretty_type_name(right_ty, current_module=current_module_name)})"
								),
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					return record_expr(expr, self._bool)
				if expr.op in (H.BinaryOp.AND, H.BinaryOp.OR):
					return record_expr(expr, self._bool)
				return record_expr(expr, self._unknown)

			# Arrays/ternary.
			if isinstance(expr, H.HArrayLiteral):
				elem_types = [type_expr(e) for e in expr.elements]
				if drift_debug.enabled("array_literal_ty"):
					import sys as _sys
					if not elem_types:
						print(f"[drift:debug][array_literal_ty] array_literal node_id={expr.node_id} elem_types=[]", file=_sys.stderr)
					else:
						parts = []
						for t in elem_types:
							td = self.type_table.get(t)
							parts.append(f"{t}:{td.kind.name}:{td.name}:{td.module_id}")
						print(f"[drift:debug][array_literal_ty] array_literal node_id={expr.node_id} elem_types={','.join(parts)}", file=_sys.stderr)
				if not elem_types:
					if expected_type is not None:
						td = self.type_table.get(expected_type)
						if td.kind is TypeKind.ARRAY:
							return record_expr(expr, expected_type)
					if getattr(expr, "defer_infer_diag", False):
						return record_expr(expr, self._unknown)
					diagnostics.append(
						_tc_diag(
							message="cannot infer element type for array literal; add a type annotation",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if elem_types and all(t == elem_types[0] for t in elem_types):
					if _reject_zst_array(elem_types[0], span=getattr(expr, "loc", Span())):
						return record_expr(expr, self._unknown)
					copy_status = self.type_table.copy_status(elem_types[0])
					if copy_status is None:
						reason = self.type_table.copy_unknown_reason(elem_types[0])
						diagnostics.append(
							_tc_diag(
								message=f"array literal element type Copy proof is unknown ({reason})",
								severity="error",
								span=getattr(expr, "loc", Span()),
								code="E-ARRAY-LITERAL-COPY-UNKNOWN",
							)
						)
						return record_expr(expr, self._unknown)
					if not copy_status:
						diagnostics.append(
							_tc_diag(
								message="array literals require Copy element type in v1",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					return record_expr(expr, self.type_table.new_array(elem_types[0]))
				return record_expr(expr, self._unknown)

			if hasattr(H, "HMapLiteral") and isinstance(expr, getattr(H, "HMapLiteral")):
				key_types = [type_expr(entry.key) for entry in expr.entries]
				value_types = [type_expr(entry.value) for entry in expr.entries]
				def _request_map_insert_instantiation(map_ty: TypeId) -> None:
					if signatures_by_id is None:
						return
					map_inst = self.type_table.get_struct_instance(map_ty)
					if map_inst is None:
						return
					base_def = self.type_table.get(map_inst.base_id)
					if base_def.kind is not TypeKind.STRUCT or base_def.module_id != "std.containers":
						return
					insert_template_name = None
					if base_def.name == "HashMapCore":
						insert_template_name = "HashMapCore<K, V, B>::insert"
					elif base_def.name == "TreeMap":
						insert_template_name = "TreeMap<K, V>::insert"
					if insert_template_name is None:
						return
					insert_fn_id = None
					for cand_fn_id, cand_sig in signatures_by_id.items():
						if cand_fn_id.module != "std.containers":
							continue
						if cand_fn_id.name != insert_template_name:
							continue
						if getattr(cand_sig, "method_name", None) != "insert":
							continue
						insert_fn_id = cand_fn_id
						break
					if insert_fn_id is None:
						return
					record_instantiation(
						callsite_id=getattr(expr, "callsite_id", None),
						node_id=getattr(expr, "node_id", None),
						target_fn_id=insert_fn_id,
						impl_args=tuple(map_inst.type_args),
						fn_args=(),
						callsite_span=getattr(expr, "loc", None),
					)

				if not value_types:
					if _is_map_like_target_type(expected_type):
						return record_expr(expr, expected_type if expected_type is not None else self._unknown)
					if getattr(expr, "defer_infer_diag", False):
						return record_expr(expr, self._unknown)
					diagnostics.append(
						_tc_diag(
							message="cannot infer target type for empty map literal; add a type annotation",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if not all(t == key_types[0] for t in key_types):
					diagnostics.append(
						_tc_diag(
							message="map literal keys do not have a consistent type",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if not all(t == value_types[0] for t in value_types):
					diagnostics.append(
						_tc_diag(
							message="map literal values do not have a consistent type",
							severity="error",
							span=getattr(expr, "loc", Span()),
						)
					)
					return record_expr(expr, self._unknown)
				if _is_map_like_target_type(expected_type):
					if expected_type is not None and expr.entries:
						map_inst = self.type_table.get_struct_instance(expected_type)
						if map_inst is not None and len(map_inst.type_args) >= 2:
							key_ty = map_inst.type_args[0]
							value_ty = map_inst.type_args[1]
							inferred_key_ty = key_ty
							inferred_value_ty = value_ty
							key_td = self.type_table.get(key_ty)
							value_td = self.type_table.get(value_ty)
							if key_td.kind is TypeKind.TYPEVAR and key_types:
								inferred_key_ty = key_types[0]
							if value_td.kind is TypeKind.TYPEVAR and value_types:
								inferred_value_ty = value_types[0]
							if key_types and key_td.kind is not TypeKind.TYPEVAR and key_types[0] != key_ty:
								diagnostics.append(
									_tc_diag(
										message="map literal key type does not match target map key type",
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
								return record_expr(expr, self._unknown)
							if value_types and value_td.kind is not TypeKind.TYPEVAR and value_types[0] != value_ty:
								diagnostics.append(
									_tc_diag(
										message="map literal value type does not match target map value type",
										severity="error",
										span=getattr(expr, "loc", Span()),
									)
								)
								return record_expr(expr, self._unknown)
							inferred_args = list(map_inst.type_args)
							inferred_args[0] = inferred_key_ty
							inferred_args[1] = inferred_value_ty
							inferred_map_ty = self.type_table.ensure_struct_instantiated(map_inst.base_id, inferred_args)
							_request_map_insert_instantiation(inferred_map_ty)
							return record_expr(expr, inferred_map_ty)
						_request_map_insert_instantiation(expected_type)
					return record_expr(expr, expected_type if expected_type is not None else self._unknown)
				hash_map_core_base = self.type_table.ensure_named("HashMapCore", module_id="std.containers")
				default_hasher_ty = self.type_table.ensure_named("DefaultBuildHasher", module_id="std.core.hash")
				base_td = self.type_table.get(hash_map_core_base)
				if base_td.kind is TypeKind.STRUCT and hash_map_core_base in self.type_table.struct_bases:
					inferred_map_ty = self.type_table.ensure_struct_instantiated(hash_map_core_base, [key_types[0], value_types[0], default_hasher_ty])
					_request_map_insert_instantiation(inferred_map_ty)
					return record_expr(expr, inferred_map_ty)
				diagnostics.append(
					_tc_diag(
						message="cannot infer target type for map literal; add a type annotation",
						severity="error",
						span=getattr(expr, "loc", Span()),
					)
				)
				return record_expr(expr, self._unknown)

			if isinstance(expr, H.HTernary):
				type_expr(expr.cond)
				# G3: rewrite a bare HVar ternary condition that
				# refers to a Copy match-arm binder (`Ref<Bool>`) as
				# `HUnary(DEREF, HVar)` so the lowered IR sees an
				# `i1` rather than a `ptr`.  Same shape as the
				# HBinary operand rewrite — see comments there.
				if isinstance(expr.cond, H.HVar):
					_bid = getattr(expr.cond, "binding_id", None)
					if _bid is not None and _bid in copy_arm_binder_ids:
						_new_cond = H.HUnary(op=H.UnaryOp.DEREF, expr=expr.cond)
						_assign_node_id(_new_cond)
						expr.cond = _new_cond
						type_expr(expr.cond)
				then_ty = type_expr(expr.then_expr)
				else_ty = type_expr(expr.else_expr)
				return record_expr(expr, then_ty if then_ty == else_ty else self._unknown)

			if isinstance(expr, H.HExceptionInit):
				from lang.driftc.core.exception_ctor_args import KwArg as _KwArg, resolve_exception_ctor_args

				schemas: dict[str, tuple[str, list[str]]] = getattr(self.type_table, "exception_schemas", {}) or {}
				schema = schemas.get(expr.event_fqn)
				decl_fields: list[str] | None
				if schema is None:
					decl_fields = None
				else:
					_decl_fqn, decl_fields = schema

				resolved, diags = resolve_exception_ctor_args(
					event_fqn=expr.event_fqn,
					declared_fields=decl_fields,
					pos_args=[(a, getattr(a, "loc", Span())) for a in expr.pos_args],
					kw_args=[
						_KwArg(name=kw.name, value=kw.value, name_span=getattr(kw, "loc", Span()))
						for kw in expr.kw_args
					],
					span=getattr(expr, "loc", Span()),
				)
				diagnostics.extend(diags)

				values_to_validate = [v for _name, v in resolved]
				if decl_fields is None:
					values_to_validate = list(expr.pos_args) + [kw.value for kw in expr.kw_args]

				replacements: dict[int, H.HExpr] = {}
				for val_expr in values_to_validate:
					if isinstance(val_expr, H.HMove):
						val_check_expr = val_expr.subject
						if hasattr(H, "HPlaceExpr") and isinstance(val_check_expr, getattr(H, "HPlaceExpr")):
							val_check_expr = val_check_expr.base
					else:
						val_check_expr = val_expr
					val_ty = type_expr(val_check_expr, used_as_value=True)
					if val_ty == self._dv:
						continue
					val_nom_ty = val_ty
					val_td = self.type_table.get(val_nom_ty)
					if val_td.kind is TypeKind.REF and val_td.param_types:
						val_nom_ty = val_td.param_types[0]
					if not self.type_table.is_diagnostic(val_nom_ty):
						diagnostics.append(
							_tc_diag(
								message="exception field value must implement Diagnostic",
								severity="error",
								span=getattr(val_expr, "loc", Span()),
							)
						)
						continue
					if isinstance(val_expr, (H.HLiteralInt, H.HLiteralBool, H.HLiteralString)):
						continue
					if hasattr(H, "HLiteralUint") and isinstance(val_expr, getattr(H, "HLiteralUint")):
						continue
					if hasattr(H, "HLiteralUint64") and isinstance(val_expr, getattr(H, "HLiteralUint64")):
						continue
					if isinstance(val_expr, H.HDVInit):
						continue
					if val_nom_ty in (self._int, self._uint, self._bool, self._string, self._float):
						kind_name = "Int"
						if val_nom_ty == self._bool:
							kind_name = "Bool"
						elif val_nom_ty == self._string:
							kind_name = "String"
						elif val_nom_ty == self._float:
							kind_name = "Float"
						dv_init = H.HDVInit(dv_type_name=kind_name, args=[val_expr])
						type_expr(dv_init)
						replacements[id(val_expr)] = dv_init
						continue
					to_diag_call = H.HMethodCall(receiver=val_expr, method_name="to_diag", args=[])
					to_diag_call.callsite_id = _alloc_callsite_id()
					type_expr(to_diag_call)
					replacements[id(val_expr)] = to_diag_call

				if replacements:
					expr.pos_args = [replacements.get(id(a), a) for a in expr.pos_args]
					for kw in expr.kw_args:
						kw.value = replacements.get(id(kw.value), kw.value)
				return record_expr(expr, self._error)

			# DiagnosticValue constructors.
			if isinstance(expr, H.HDVInit):
				arg_types = [type_expr(a) for a in expr.args]
				if expr.args:
					# Only zero-arg, single-arg primitive DV ctors, or Object(entries)
					# are supported in v1.
					if len(expr.args) > 1:
						diagnostics.append(
							_tc_diag(
								message="DiagnosticValue constructors support at most one argument in v1",
								severity="error",
								span=getattr(expr, "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
					inner_ty = arg_types[0]
					if expr.dv_type_name == "Object":
						td_inner = self.type_table.get(inner_ty)
						is_ok = False
						if td_inner.kind is TypeKind.ARRAY and td_inner.param_types:
							elem_ty = td_inner.param_types[0]
							elem_td = self.type_table.get(elem_ty)
							if elem_td.kind is TypeKind.STRUCT:
								key_info = _resolve_struct_field_type(elem_ty, "key")
								value_info = _resolve_struct_field_type(elem_ty, "value")
								if key_info is not None and value_info is not None:
									_, key_ty = key_info
									_, value_ty = value_info
									if key_ty == self._string and value_ty == self._dv:
										is_ok = True
						if not is_ok:
							diagnostics.append(
								_tc_diag(
									message="DiagnosticValue::Object requires Array<DiagnosticEntry>-shaped argument (fields: key:String, value:DiagnosticValue)",
									severity="error",
									span=getattr(expr.args[0], "loc", Span()),
								)
							)
							return record_expr(expr, self._unknown)
					elif inner_ty not in (self._int, self._uint, self._bool, self._string, self._float):
						diagnostics.append(
							_tc_diag(
								message="unsupported DiagnosticValue constructor argument type",
								severity="error",
								span=getattr(expr.args[0], "loc", Span()),
							)
						)
						return record_expr(expr, self._unknown)
				return record_expr(expr, self._dv)

			# Result/try sugar.
			if isinstance(expr, H.HResultOk):
				ok_ty = type_expr(expr.value)
				err_ty = self._unknown
				return record_expr(expr, self.type_table.new_fnresult(ok_ty, err_ty))

			# Fallback: unknown type.
			return record_expr(expr, self._unknown)

		catch_depth = 0

		fn_declared_throws = bool(getattr(fn_sig, "declared_throws", False))
		if drift_debug.enabled("try_auto"):
			if fn_sig is None:
				print(f"[try_auto] fn={function_symbol(fn_id)} signature=missing", file=sys.stderr)
			else:
				print(f"[try_auto] fn={function_symbol(fn_id)} signature=present sig_id={id(fn_sig)} declared_throws_raw={getattr(fn_sig, 'declared_throws', None)}", file=sys.stderr)
			print(f"[try_auto] fn={function_symbol(fn_id)} sig_map_id={id(signatures_by_id)} sig_map_type={type(signatures_by_id).__name__}", file=sys.stderr)
			if getattr(fn_id, "module", None) == "m" and isinstance(signatures_by_id, ChainMap):
				derived_sig = signatures_by_id.maps[0].get(fn_id)
				base_sig = signatures_by_id.maps[1].get(fn_id)
				print(f"[try_auto] fn={function_symbol(fn_id)} derived_sig_id={id(derived_sig)} derived_declared_throws={getattr(derived_sig, 'declared_throws', None)}", file=sys.stderr)
				print(f"[try_auto] fn={function_symbol(fn_id)} base_sig_id={id(base_sig)} base_declared_throws={getattr(base_sig, 'declared_throws', None)}", file=sys.stderr)
			print(f"[try_auto] fn={function_symbol(fn_id)} declared_throws={fn_declared_throws}", file=sys.stderr)
		try_block_depth = 0
		def _is_core_result_variant(ty: TypeId) -> bool:
			if ty is None:
				return False
			schema = self.type_table.get_variant_schema(ty)
			if schema is not None:
				return schema.name == "Result" and schema.module_id in ("std.core", "core")
			# Fallback: annotations may resolve to a TypeDef that isn't yet
			# in variant_schemas/variant_instances (e.g. package-consumer
			# contexts create FORWARD_NOMINAL placeholders, or resolve_opaque_type
			# creates fresh instantiations).  Accept any TypeDef named
			# "Result" — in Drift, `Result` refers exclusively to
			# std.core.Result (no user-defined type is permitted to shadow it).
			try:
				td = self.type_table.get(ty)
			except Exception:
				return False
			if td.kind not in (TypeKind.VARIANT, TypeKind.FORWARD_NOMINAL):
				return False
			return td.name == "Result"

		def _auto_try_context() -> bool:
			return try_block_depth > 0 or fn_declared_throws

		def _should_auto_try(expr_ty: TypeId | None, expected_ty: TypeId | None) -> bool:
			if expr_ty is None:
				return False
			if not _auto_try_context():
				return False
			if not _is_core_result_variant(expr_ty):
				return False
			# Opt-out: an explicit `Result<T, E>` type annotation preserves
			# the Result object so the user can call `.or_throw()` / pattern
			# match explicitly.  Otherwise auto-try is eager — inside a
			# `throws` function or `try {}` block, unannotated local bindings,
			# return expressions, and discarded expression statements all
			# unwrap Result<T, E> to T via compiler-synthesized or_throw().
			if expected_ty is not None and _is_core_result_variant(expected_ty):
				return False
			return True

		def _wrap_auto_try(expr: H.HExpr) -> H.HExpr:
			# Auto-try synthesizes or_throw() — an inherent method on
			# Result<T, E> that requires no trait scope.
			call = H.HMethodCall(receiver=expr, method_name="or_throw", args=[], kwargs=[])
			call.callsite_id = _alloc_callsite_id()
			_assign_node_id(call)
			return call

		def type_stmt(stmt: H.HStmt) -> None:
			nonlocal catch_depth
			nonlocal unsafe_context
			nonlocal try_block_depth
			# Borrow conflicts are diagnosed within a single statement.
			borrows_in_stmt.clear()
			borrow_expr_ids_in_stmt.clear()
			if isinstance(stmt, H.HLocalConst):
				# Block-scope constant: register in scope, no storage.
				if stmt.binding_id is None:
					stmt.binding_id = self._alloc_local_id()
				declared_ty = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					try:
						declared_ty = resolve_opaque_type(
							stmt.declared_type_expr,
							self.type_table,
							module_id=current_module_name,
							type_params=type_param_map,
						)
					except Exception:
						declared_ty = None
				if declared_ty is not None:
					scope_env[-1][stmt.name] = declared_ty
					scope_bindings[-1][stmt.name] = stmt.binding_id
					binding_types[stmt.binding_id] = declared_ty
					binding_names[stmt.binding_id] = stmt.name
					binding_mutable[stmt.binding_id] = False
					binding_place_kind[stmt.binding_id] = PlaceKind.LOCAL
					# Mark as local const so use sites skip the Copy check.
					local_const_binding_ids.add(int(stmt.binding_id))
				return
			if isinstance(stmt, H.HLet):
				if stmt.binding_id is None:
					stmt.binding_id = self._alloc_local_id()
				elif stmt.binding_id in binding_names and binding_names.get(stmt.binding_id) != stmt.name:
					stmt.binding_id = self._alloc_local_id()
				locals.append(stmt.binding_id)
				declared_ty: TypeId | None = None
				if getattr(stmt, "declared_type_expr", None) is not None:
					try:
						_reject_fixed_width_type_expr(
							stmt.declared_type_expr,
							getattr(stmt.declared_type_expr, "module_id", None) or current_module_name,
							Span.from_loc(getattr(stmt.declared_type_expr, "loc", None)),
						)
						declared_ty = resolve_opaque_type(
							stmt.declared_type_expr,
							self.type_table,
							module_id=current_module_name,
							type_params=type_param_map,
						)
					except Exception:
						declared_ty = None
					if declared_ty is not None:
						_enforce_struct_requires(
							declared_ty,
							Span.from_loc(getattr(stmt.declared_type_expr, "loc", None)),
						)
				if (
					getattr(stmt, "declared_type_expr", None) is None
					and isinstance(stmt.value, H.HLambda)
					and not getattr(stmt, "is_placeholder", False)
				):
					val_ty = self._unknown
					pending_lambda_by_binding[stmt.binding_id] = stmt.value
					scope_env[-1][stmt.name] = val_ty
					scope_bindings[-1][stmt.name] = stmt.binding_id
					binding_types[stmt.binding_id] = val_ty
					binding_names[stmt.binding_id] = stmt.name
					binding_mutable[stmt.binding_id] = bool(getattr(stmt, "is_mutable", False))
					binding_place_kind[stmt.binding_id] = PlaceKind.LOCAL
					return
				# If the user provides a type annotation, treat it as the expected type
				# for the initializer. This enables constructor calls like:
				#   val x: Optional<Int> = Some(1)
				inferred_ty = type_expr(stmt.value, expected_type=declared_ty)
				if _should_auto_try(inferred_ty, declared_ty):
					stmt.value = _wrap_auto_try(stmt.value)
					inferred_ty = type_expr(stmt.value, expected_type=declared_ty)
				val_ty = inferred_ty
				if declared_ty is not None:
					# MVP: treat the declared type as authoritative for the binding.
					# If the initializer is obviously incompatible, emit a diagnostic.
					# Numeric literals are allowed to flow into Int/Uint without requiring
					# an explicit cast.
					if inferred_ty is not None and inferred_ty != declared_ty:
						def _same_type(lhs: TypeId, rhs: TypeId) -> bool:
							if lhs == rhs:
								return True
							try:
								return _normalize_type_key(type_key_from_typeid(self.type_table, lhs)) == _normalize_type_key(
									type_key_from_typeid(self.type_table, rhs)
								)
							except Exception:
								return False
						if _same_type(inferred_ty, declared_ty):
							inferred_ty = declared_ty
							val_ty = declared_ty
						if self.type_table.get(declared_ty).kind is TypeKind.INTERFACE:
							if self.type_table.get(inferred_ty).kind is TypeKind.INTERFACE:
								if iface_assignable(inferred_ty, declared_ty):
									record_iface_coercion(stmt.value, declared_ty)
								else:
									diagnostics.append(
										_tc_diag(
											message=f"initializer type '{self.type_table.get(inferred_ty).name}' does not match declared type '{self.type_table.get(declared_ty).name}'",
											severity="error",
											span=getattr(stmt, "loc", Span()),
										)
									)
							else:
								# Patch B Site 5: prefer implicit Callback* wrap over a raw
								# iface_coercion when the target is a concrete callback iface.
								_cb_res = _try_callback_wrap_for_iface_slot(stmt.value, inferred_ty, declared_ty)
								if _cb_res.is_wrapped:
									stmt.value = _cb_res.cb_call
									inferred_ty = _cb_res.cb_ty
								elif _cb_res.is_rejected:
									# Diagnostic already in stream (or arg already
									# poisoned upstream). Do NOT record a raw
									# iface_coercion over the failed slot.
									pass
								else:
									record_iface_coercion(stmt.value, declared_ty)
						else:
							is_int_lit = isinstance(stmt.value, H.HLiteralInt)
							is_uint_lit = hasattr(H, "HLiteralUint") and isinstance(stmt.value, getattr(H, "HLiteralUint"))
							is_uint64_lit = hasattr(H, "HLiteralUint64") and isinstance(stmt.value, getattr(H, "HLiteralUint64"))
							decl_name = self.type_table.get(declared_ty).name
							inf_name = self.type_table.get(inferred_ty).name
							if inferred_ty == self._unknown:
								pass  # upstream error already poisoned the expression; suppress cascading mismatch
							elif not (is_int_lit and decl_name in ("Int", "Uint") and inf_name == "Int") and not (is_uint_lit and decl_name == "Uint" and inf_name == "Uint") and not (is_uint64_lit and decl_name == "Uint64" and inf_name == "Uint64"):
								diagnostics.append(
									_tc_diag(
										message=f"initializer type '{inf_name}' does not match declared type '{decl_name}'",
										severity="error",
										span=getattr(stmt, "loc", Span()),
									)
								)
					val_ty = declared_ty
				if getattr(stmt, "capture_alias", None) is not None and not bool(getattr(stmt, "capture", False)):
					diagnostics.append(
						_tc_diag(
							message='capture alias requires capture marker: use `val ^name as "alias" = ...`',
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if bool(getattr(stmt, "capture", False)):
					cap_ty = val_ty
					if cap_ty is not None and cap_ty != self._unknown:
						allowed = {self._dv, self._int, self._uint, self._bool, self._string, self._float}
						if cap_ty not in allowed:
							diagnostics.append(
								_tc_diag(
									message="captured locals currently support Int/Uint/Bool/Float/String/DiagnosticValue only",
									severity="error",
									span=getattr(stmt, "loc", Span()),
								)
							)
				scope_env[-1][stmt.name] = val_ty
				scope_bindings[-1][stmt.name] = stmt.binding_id
				binding_types[stmt.binding_id] = val_ty
				binding_names[stmt.binding_id] = stmt.name
				binding_mutable[stmt.binding_id] = bool(getattr(stmt, "is_mutable", False))
				binding_place_kind[stmt.binding_id] = PlaceKind.LOCAL
				if drift_debug.enabled("typecheck"):
					import sys
					try:
						pretty_val = self._pretty_type_name(val_ty, current_module=current_module_name)
					except Exception:
						pretty_val = str(val_ty)
					print(f"[drift:debug] let {stmt.name} id={stmt.binding_id} type={pretty_val} fn={function_symbol(fn_id)}", file=sys.stderr)
				if val_ty is not None:
					vd = self.type_table.get(val_ty)
					if vd.kind is TypeKind.ARRAY and vd.param_types:
						elem_ty = vd.param_types[0]
						elem_td = self.type_table.get(elem_ty)
						if elem_td.kind is TypeKind.STRUCT:
							elem_fields: list[TypeId] = []
							elem_inst = self.type_table.get_struct_instance(elem_ty)
							if elem_inst is not None:
								elem_fields = list(elem_inst.field_types)
							elif elem_td.param_types is not None:
								elem_fields = list(elem_td.param_types)
							if any(self.type_table.get(ft).kind is TypeKind.REF for ft in elem_fields):
								diagnostics.append(
									_tc_diag(
										message="owning Array cannot contain borrowed aggregate element type in v1",
										severity="error",
										span=getattr(stmt, "loc", Span()),
									)
								)
				# Track origin for ref-typed locals: allow propagation from an existing
				# ref binding, otherwise treat as local/temporary.
				if val_ty is not None and self.type_table.get(val_ty).kind is TypeKind.REF:
					origin: Optional[int] = None
					origin_known = False
					# val r = p;  (p is a ref param or a local ref derived from param)
					if isinstance(stmt.value, H.HVar) and getattr(stmt.value, "binding_id", None) is not None:
						if stmt.value.binding_id in ref_origin_param:
							origin = ref_origin_param.get(stmt.value.binding_id)
							origin_known = True
					# val r = &(*p).x;  (reborrow through a ref that derives from param)
					if isinstance(stmt.value, H.HBorrow):
						def _base_lookup(hv: object) -> Optional[PlaceBase]:
							bid = getattr(hv, "binding_id", None)
							if bid is None:
								return None
							kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
							name = hv.name if hasattr(hv, "name") else str(hv)
							return PlaceBase(kind=kind, local_id=bid, name=name)

						sub_place = place_from_expr(stmt.value.subject, base_lookup=_base_lookup)
						if sub_place is not None and any(isinstance(p, DerefProj) for p in sub_place.projections):
							if sub_place.base.local_id in ref_origin_param:
								origin = ref_origin_param.get(sub_place.base.local_id)
								origin_known = True
						elif sub_place is not None:
							origin = None
							origin_known = True
					if origin_known:
						ref_origin_param[stmt.binding_id] = origin
			elif isinstance(stmt, H.HBlock):
				# Block statements introduce a nested lexical scope.
				#
				# This is used by desugarings like `for` which need to introduce hidden
				# temporaries without leaking them to the surrounding scope.
				scope_env.append(dict())
				scope_bindings.append(dict())
				try:
					for s in stmt.statements:
						type_stmt(s)
				finally:
					scope_env.pop()
					scope_bindings.pop()
			elif hasattr(H, "HUnsafeBlock") and isinstance(stmt, getattr(H, "HUnsafeBlock")):
				if not unsafe_allowed_module:
					diagnostics.append(_tc_diag(message="unsafe block requires --allow-unsafe", severity="error", span=getattr(stmt, "loc", Span())))
				scope_env.append(dict())
				scope_bindings.append(dict())
				prev_unsafe = unsafe_context
				unsafe_context = True
				try:
					for s in stmt.block.statements:
						type_stmt(s)
				finally:
					unsafe_context = prev_unsafe
					scope_env.pop()
					scope_bindings.pop()
			elif isinstance(stmt, H.HAssign):
				cap_bid = None
				cap_name = None
				if hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr")) and not stmt.target.projections:
					if isinstance(stmt.target.base, H.HVar):
						cap_bid = getattr(stmt.target.base, "binding_id", None)
						cap_name = stmt.target.base.name
				elif isinstance(stmt.target, H.HVar):
					cap_bid = getattr(stmt.target, "binding_id", None)
					cap_name = stmt.target.name
				if cap_bid is not None and cap_name is not None:
					cap_kind = _explicit_capture_kind(cap_bid)
					if cap_kind == "ref":
						diagnostics.append(
							_tc_diag(
								message=f"capture '{cap_name}' is shared; capture &mut {cap_name} to mutate",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
						return
				type_expr(stmt.value)
				type_expr(stmt.target, used_as_value=False)
				# Assignment target must be an addressable place.
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				if place_from_expr(stmt.target, base_lookup=_base_lookup) is None:
					diagnostics.append(
						_tc_diag(
							message="assignment target must be an addressable place",
							severity="error",
							span=getattr(stmt, "loc", Span()),
							)
						)
				# F1: assignment through a place rooted in a shared
				# reference (`&T`) is read-only.  Auto-deref through a
				# ref base for field / index projections, and explicit
				# `*p` deref, both require `&mut T`.  Without this check
				# the MIR-lowering contract at
				# `hir_to_mir.py:_lower_place_address` fires as
				# `mutable field place without &mut reached MIR
				# lowering (checker bug)` — an internal-form message
				# reaching the user.  Surfaces on the canonical shape
				# `match &r { Ok(x) => { x.status = 99; ... } }`.
				if (
					hasattr(H, "HPlaceExpr")
					and isinstance(stmt.target, getattr(H, "HPlaceExpr"))
					and stmt.target.projections
				):
					cur = type_expr(stmt.target.base, used_as_value=False)
					immut_ref_rejected = False
					for pr in stmt.target.projections:
						td = self.type_table.get(cur)
						# Auto-deref through a ref base for the next
						# projection step requires &mut.
						if td.kind is TypeKind.REF and td.param_types:
							if not td.ref_mut:
								diagnostics.append(
									_tc_diag(
										message=(
											"cannot assign through a shared reference (&T); "
											"the place is read-only — use a `&mut` reference to mutate"
										),
										severity="error",
										span=getattr(stmt, "loc", Span()),
									)
								)
								immut_ref_rejected = True
								break
							cur = td.param_types[0]
							td = self.type_table.get(cur)
						if isinstance(pr, H.HPlaceField):
							if td.kind is TypeKind.STRUCT:
								info = self.type_table.struct_field(cur, pr.name)
								if info is not None:
									_, cur = info
						elif isinstance(pr, H.HPlaceIndex):
							if td.kind is TypeKind.ARRAY and td.param_types:
								cur = td.param_types[0]
						elif isinstance(pr, H.HPlaceDeref):
							if td.kind is TypeKind.REF and td.param_types:
								if not td.ref_mut:
									diagnostics.append(
										_tc_diag(
											message="cannot assign through *p unless p is a mutable reference (&mut T)",
											severity="error",
											span=getattr(stmt, "loc", Span()),
										)
									)
									immut_ref_rejected = True
									break
								cur = td.param_types[0]
					if immut_ref_rejected:
						return
				# If assigning to a ref-typed binding, track origin (simple propagation).
				if isinstance(stmt.target, H.HVar) and getattr(stmt.target, "binding_id", None) is not None:
					tgt_bid = stmt.target.binding_id
					tgt_ty = binding_types.get(tgt_bid)
					if tgt_ty is not None and self.type_table.get(tgt_ty).kind is TypeKind.REF:
						origin: Optional[int] = None
						origin_known = False
						if isinstance(stmt.value, H.HVar) and getattr(stmt.value, "binding_id", None) is not None:
							if stmt.value.binding_id in ref_origin_param:
								origin = ref_origin_param.get(stmt.value.binding_id)
								origin_known = True
						if origin_known:
							ref_origin_param[tgt_bid] = origin
			elif hasattr(H, "HAugAssign") and isinstance(stmt, getattr(H, "HAugAssign")):
				"""
				Augmented assignment (`+=`) type rules (MVP).

				- Target must be an addressable place (same as `=`).
				- Operand types must match.
				- Currently supported for numeric scalars only (Int/Float).

				We enforce *writability* here as well:
				- Writes to owned storage require a `var` base binding.
				- Writes through deref require a mutable reference (`&mut`) at each deref.
				"""
				cap_bid = None
				cap_name = None
				if hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr")) and not stmt.target.projections:
					if isinstance(stmt.target.base, H.HVar):
						cap_bid = getattr(stmt.target.base, "binding_id", None)
						cap_name = stmt.target.base.name
				elif isinstance(stmt.target, H.HVar):
					cap_bid = getattr(stmt.target, "binding_id", None)
					cap_name = stmt.target.name
				if cap_bid is not None and cap_name is not None:
					cap_kind = _explicit_capture_kind(cap_bid)
					if cap_kind == "ref":
						diagnostics.append(
							_tc_diag(
								message=f"capture '{cap_name}' is shared; capture &mut {cap_name} to mutate",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
						return
				tgt_ty = type_expr(stmt.target, used_as_value=False)
				val_ty = type_expr(stmt.value)

				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				tgt_place = place_from_expr(stmt.target, base_lookup=_base_lookup)
				if tgt_place is None:
					diagnostics.append(
						_tc_diag(
							message="assignment target must be an addressable place",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
					return

				# Writability: owned storage requires `var`; reborrow writes require `&mut`.
				has_deref = any(isinstance(p, DerefProj) for p in tgt_place.projections)
				if not has_deref and tgt_place.base.local_id is not None and not binding_mutable.get(tgt_place.base.local_id, False):
					diagnostics.append(
						_tc_diag(
							message="cannot assign through an immutable binding; declare it with `var`",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if has_deref and hasattr(H, "HPlaceExpr") and isinstance(stmt.target, getattr(H, "HPlaceExpr")):
					cur = type_expr(stmt.target.base, used_as_value=False)
					for pr in stmt.target.projections:
						if isinstance(pr, H.HPlaceDeref):
							ptr_def = self.type_table.get(cur)
							if ptr_def.kind is not TypeKind.REF or not ptr_def.ref_mut:
								diagnostics.append(
									_tc_diag(
										message="cannot assign through *p unless p is a mutable reference (&mut T)",
										severity="error",
										span=getattr(stmt, "loc", Span()),
									)
								)
								break
							if ptr_def.param_types:
								cur = ptr_def.param_types[0]
						elif isinstance(pr, H.HPlaceField):
							td = self.type_table.get(cur)
							if td.kind is TypeKind.STRUCT:
								info = self.type_table.struct_field(cur, pr.name)
								if info is not None:
									_, cur = info
						elif isinstance(pr, H.HPlaceIndex):
							td = self.type_table.get(cur)
							if td.kind is TypeKind.ARRAY and td.param_types:
								cur = td.param_types[0]

				arith_ops = {"+=", "-=", "*=", "/="}
				bit_ops = {"&=", "|=", "^=", "<<=", ">>="}
				mod_ops = {"%="}
				# Type check: supported augmented assignment operators.
				if stmt.op not in (arith_ops | bit_ops | mod_ops):
					diagnostics.append(
						_tc_diag(
							message=f"unsupported augmented assignment operator '{stmt.op}'",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if tgt_ty != val_ty:
					diagnostics.append(
						_tc_diag(
							message="augmented assignment requires matching operand types",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if stmt.op in arith_ops:
					if tgt_ty not in (self._int, self._float):
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							_tc_diag(
								message=f"augmented assignment '{stmt.op}' is not supported for type '{pretty}' in v1",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
				elif stmt.op in mod_ops:
					if tgt_ty not in (self._int, self._uint):
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							_tc_diag(
								message=f"augmented assignment '{stmt.op}' is not supported for type '{pretty}' in v1",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
				elif stmt.op in bit_ops:
					if tgt_ty not in (self._uint, self._uint64):
						pretty = self.type_table.get(tgt_ty).name if tgt_ty is not None else "Unknown"
						diagnostics.append(
							_tc_diag(
								message=f"bitwise augmented assignment requires Uint or Uint64 operands (have '{pretty}')",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
			elif isinstance(stmt, H.HExprStmt):
				expr_ty = type_expr(stmt.expr, used_as_value=False)
				if _auto_try_context() and expr_ty is not None and _is_core_result_variant(expr_ty):
					stmt.expr = _wrap_auto_try(stmt.expr)
					type_expr(stmt.expr, used_as_value=False)
			elif isinstance(stmt, H.HAssert):
				cond_ty = type_expr(stmt.cond)
				if cond_ty is not None and cond_ty != self._bool:
					pretty = self._pretty_type_name(cond_ty, current_module=current_module_name)
					diagnostics.append(
						_tc_diag(
							message=f"assert condition must be Bool (have '{pretty}')",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
				if stmt.msg is not None:
					msg_ty = type_expr(stmt.msg)
					if msg_ty is not None and msg_ty != self._string:
						pretty = self._pretty_type_name(msg_ty, current_module=current_module_name)
						diagnostics.append(
							_tc_diag(
								message=f"assert message must be String (have '{pretty}')",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
			elif isinstance(stmt, H.HReturn):
				if stmt.value is not None:
					used_as_value = True
					if (
						return_type is not None
						and self_binding_id is not None
						and isinstance(stmt.value, H.HVar)
						and stmt.value.binding_id == self_binding_id
					):
						bound_self = binding_types.get(self_binding_id)
						if bound_self is not None and bound_self == return_type:
							bound_def = self.type_table.get(bound_self)
							if bound_def.kind is TypeKind.REF and bound_def.ref_mut:
								used_as_value = False
					inferred = type_expr(stmt.value, expected_type=return_type, used_as_value=used_as_value)
					if _should_auto_try(inferred, return_type):
						stmt.value = _wrap_auto_try(stmt.value)
						inferred = type_expr(stmt.value, expected_type=return_type, used_as_value=used_as_value)
					if return_type is not None and inferred is not None and inferred != return_type:
						if self.type_table.get(return_type).kind is TypeKind.INTERFACE:
							if self.type_table.get(inferred).kind is TypeKind.INTERFACE:
								if iface_assignable(inferred, return_type):
									record_iface_coercion(stmt.value, return_type)
								else:
									diagnostics.append(
										_tc_diag(
											message=f"return type '{self.type_table.get(inferred).name}' does not match declared type '{self.type_table.get(return_type).name}'",
											severity="error",
											span=getattr(stmt, "loc", Span()),
										)
									)
							else:
								# Patch B Site 6: prefer implicit Callback* wrap over a raw
								# iface_coercion when the declared return type is a concrete
								# callback iface and the returned expression is a bare lambda
								# / fn-typed value.
								_cb_res = _try_callback_wrap_for_iface_slot(stmt.value, inferred, return_type)
								if _cb_res.is_wrapped:
									stmt.value = _cb_res.cb_call
									inferred = _cb_res.cb_ty
								elif _cb_res.is_rejected:
									pass  # see Patch B Site 5 comment
								else:
									record_iface_coercion(stmt.value, return_type)
			elif isinstance(stmt, H.HIf):
				if isinstance(stmt.cond, H.HTraitExpr):
					parser_expr = _trait_expr_to_parser(stmt.cond)
					guard_key = _guard_key(stmt.cond)
					if type_param_map:
						parser_expr = _resolve_trait_subjects_for_type_params(parser_expr, type_param_map)
					subst: dict[object, object] = {}
					subjects: set[object] = set()
					_collect_trait_subjects(parser_expr, subjects)
					for subj in subjects:
						if subj == "Self":
							if self_type_id is None:
								continue
							subj_type_id = self_type_id
							subj_def = self.type_table.get(subj_type_id)
							if subj_def.kind is TypeKind.REF and subj_def.param_types:
								subj_type_id = subj_def.param_types[0]
								subj_def = self.type_table.get(subj_type_id)
							key = _normalize_type_key(type_key_from_typeid(self.type_table, subj_type_id))
							subst["Self"] = key
							if subj_def.kind is TypeKind.TYPEVAR and subj_def.type_param_id is not None:
								subst.setdefault(subj_def.type_param_id, key)
							continue
						for scope in reversed(scope_env):
							if subj in scope:
								subst[subj] = _normalize_type_key(type_key_from_typeid(self.type_table, scope[subj]))
								break
					world = global_trait_world or visible_trait_world
					if world is None:
						diagnostics.append(
							_tc_diag(
								message="trait guard cannot be evaluated without a trait world",
								severity="error",
								span=getattr(stmt.cond, "loc", Span()),
							)
						)
						type_block(stmt.then_block)
						if stmt.else_block:
							type_block(stmt.else_block)
					else:
						env = TraitEnv(
							default_module=current_module_name,
							default_package=default_package,
							module_packages=module_packages or {},
							assumed_true=set(fn_require_assumed),
							type_table=self.type_table,
						)
						res = prove_expr(world, env, subst, parser_expr)
						if res.status is ProofStatus.PROVED:
							guard_outcomes[guard_key] = res.status
							assumed = _guard_assumptions(parser_expr, subst=subst)
							_with_guard_assumptions(assumed, stmt.then_block)
						elif res.status is ProofStatus.REFUTED:
							guard_outcomes[guard_key] = res.status
							if stmt.else_block:
								type_block(stmt.else_block)
						else:
							if res.status is ProofStatus.AMBIGUOUS:
								guard_outcomes[guard_key] = res.status
								diagnostics.append(
									_tc_diag(
										message="trait guard is ambiguous at compile time",
										severity="error",
										span=getattr(stmt.cond, "loc", Span()),
									)
								)
								type_block(stmt.then_block)
								if stmt.else_block:
									type_block(stmt.else_block)
							else:
								is_generic_guard = False
								for subj in subjects:
									if isinstance(subj, TypeParamId):
										is_generic_guard = True
										break
									if isinstance(subj, str) and subj in type_param_map:
										is_generic_guard = True
										break
									if subj == "Self":
										if self_type_id is None:
											continue
										subj_type_id = self_type_id
										subj_def = self.type_table.get(subj_type_id)
										if subj_def.kind is TypeKind.REF and subj_def.param_types:
											subj_type_id = subj_def.param_types[0]
										if _type_has_typevar(subj_type_id):
											is_generic_guard = True
											break
								if not is_generic_guard:
									diagnostics.append(
										_tc_diag(
											message="internal: trait guard is not decidable for a concrete type",
											severity="error",
											span=getattr(stmt.cond, "loc", Span()),
											code="E-TRAIT-GUARD-NOT-DECIDABLE",
										)
									)
									type_block(stmt.then_block)
									if stmt.else_block:
										type_block(stmt.else_block)
								else:
									assumed = _guard_assumptions(parser_expr, subst=subst)
									_type_block_defer_diags(
										stmt.then_block,
										guard_key=guard_key,
										branch="then",
										assumed=assumed,
									)
									if stmt.else_block:
										_type_block_defer_diags(
											stmt.else_block,
											guard_key=guard_key,
											branch="else",
										)
				else:
					type_expr(stmt.cond)
					type_block(stmt.then_block)
					if stmt.else_block:
						type_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				type_block(stmt.body)
			elif isinstance(stmt, H.HTry):
				try_block_depth += 1
				type_block(stmt.body)
				try_block_depth -= 1
				for arm in stmt.catches:
					catch_depth += 1
					scope_env.append(dict())
					scope_bindings.append(dict())
					try:
						if arm.binder:
							bid = self._alloc_local_id()
							locals.append(bid)
							scope_env[-1][arm.binder] = self._error
							scope_bindings[-1][arm.binder] = bid
							binding_types[bid] = self._error
							binding_names[bid] = arm.binder
							binding_mutable[bid] = False
							binding_place_kind[bid] = PlaceKind.LOCAL
						type_block(arm.block)
					finally:
						scope_env.pop()
						scope_bindings.pop()
						catch_depth -= 1
			elif isinstance(stmt, H.HThrow):
				if isinstance(stmt.value, H.HMethodCall) and stmt.value.method_name == "unwrap_err":
					type_expr(stmt.value)
				else:
					val_ty = type_expr(stmt.value, allow_exception_init=True)
					if not isinstance(stmt.value, H.HExceptionInit) and val_ty != self._error:
						diagnostics.append(
							_tc_diag(
								message="throw payload must be an exception constructor",
								severity="error",
								span=getattr(stmt, "loc", Span()),
							)
						)
			elif isinstance(stmt, H.HRethrow):
				# Valid only inside a catch; outside catches it is reported here.
				if catch_depth == 0:
					diagnostics.append(
						_tc_diag(
							message="rethrow is only valid inside a catch block",
							severity="error",
							span=getattr(stmt, "loc", Span()),
						)
					)
			# HBreak/HContinue are typeless here.

		def type_block(block: H.HBlock) -> None:
			scope_env.append(dict())
			scope_bindings.append(dict())
			try:
				for s in block.statements:
					type_stmt(s)
			finally:
				scope_env.pop()
				scope_bindings.pop()

		def type_block_in_scope(block: H.HBlock) -> None:
			for s in block.statements:
				type_stmt(s)

		type_block(body)

		def _apply_fnptr_consts(obj: object) -> object:
			if isinstance(obj, H.HNode):
				entry = fnptr_consts_by_node_id.get(obj.node_id)
				if entry is not None and not isinstance(obj, H.HFnPtrConst):
					fn_ref, call_sig = entry
					repl = H.HFnPtrConst(fn_ref=fn_ref, call_sig=call_sig)
					repl.node_id = obj.node_id
					return repl
			if is_dataclass(obj):
				updates: dict[str, object] = {}
				for f in fields(obj):
					val = getattr(obj, f.name)
					new_val = _apply_fnptr_consts(val)
					if new_val is not val:
						updates[f.name] = new_val
				if updates:
					if getattr(obj, "__dataclass_params__", None) and obj.__dataclass_params__.frozen:
						new_obj = replace(obj, **updates)
						if isinstance(obj, H.HNode):
							object.__setattr__(new_obj, "node_id", obj.node_id)
						return new_obj
					for name, val in updates.items():
						setattr(obj, name, val)
				return obj
			if isinstance(obj, list):
				for idx, val in enumerate(obj):
					new_val = _apply_fnptr_consts(val)
					if new_val is not val:
						obj[idx] = new_val
				return obj
			if isinstance(obj, dict):
				for key, val in list(obj.items()):
					new_val = _apply_fnptr_consts(val)
					if new_val is not val:
						obj[key] = new_val
				return obj
			return obj

		if drift_debug.enabled("local_types_trace") and getattr(fn_id, "module", None) == "main" and getattr(fn_id, "name", None) == "run":
			def _check_dup_expr_ids(tag: str) -> None:
				print(f"[drift:debug][local_types_trace] fn={fn_id} scan={tag}", file=sys.stderr)
				seen_expr_ids: Dict[int, tuple[str, object]] = {}
				def _walk_expr_ids(obj: object) -> None:
					if isinstance(obj, H.HExpr):
						node_id = getattr(obj, "node_id", 0)
						if node_id == 0:
							return
						kind = type(obj).__name__
						span = getattr(obj, "loc", Span())
						prev = seen_expr_ids.get(node_id)
						if prev is None:
							seen_expr_ids[node_id] = (kind, span)
						else:
							prev_kind, prev_span = prev
							if prev_kind != kind:
								print(f"[drift:debug][local_types_trace] fn={fn_id} {tag}_dup_node_id={node_id} prev={prev_kind} now={kind} prev_span={prev_span} now_span={span}", file=sys.stderr)
					if not (is_dataclass(obj) or isinstance(obj, (list, tuple, dict))):
						return
					if is_dataclass(obj):
						for f in fields(obj):
							_walk_expr_ids(getattr(obj, f.name))
						return
					if isinstance(obj, (list, tuple)):
						for item in obj:
							_walk_expr_ids(item)
						return
					if isinstance(obj, dict):
						for key in sorted(obj.keys(), key=repr):
							_walk_expr_ids(obj[key])
						return
				_walk_expr_ids(body)
			_check_dup_expr_ids("pre_fnptr")
		if fnptr_consts_by_node_id:
			_apply_fnptr_consts(body)
		if drift_debug.enabled("local_types_trace") and getattr(fn_id, "module", None) == "main" and getattr(fn_id, "name", None) == "run":
			_check_dup_expr_ids("post_fnptr")

		typed = TypedFn(
			fn_id=fn_id,
			name=fn_id.name,
			params=params,
			param_bindings=param_bindings,
			locals=locals,
			body=body,
			expr_types={ref: ty for ref, ty in expr_types.items()},
			binding_for_var=binding_for_var,
			binding_types=binding_types,
			binding_names=binding_names,
			binding_mutable=binding_mutable,
			binding_place_kind=binding_place_kind,
			call_resolutions=call_resolutions,
			call_info_by_callsite_id=call_info_by_callsite_id,
			instantiations_by_callsite_id=instantiations_by_callsite_id,
			instantiations_by_node_id=instantiations_by_node_id,
			iface_coercions=iface_coercions,
			preseed_type_params=dict(preseed_type_params or {}),
		)
		if self.type_table is not None and self.type_table.type_provenance_enabled():
			for bid, bty in binding_types.items():
				note = binding_names.get(bid)
				self.type_table.record_type_provenance(
					bty,
					phase="typecheck",
					kind="binding",
					span=None,
					note=note,
				)

		if callable_registry is not None:
			missing_callsite_nodes: list[object] = []
			missing_info: list[int] = []
			callsite_nodes_by_id: dict[int, object] = {}

			def _collect_callsite_ids(block: H.HBlock) -> set[int]:
				# Uses the shared iterative HIR walker from
				# `stage1/node_ids.py` with a lambda-skipping
				# `should_descend` variant so the call collector does not
				# cross closure boundaries. Row #15 dedup pass.
				from lang.driftc.stage1.node_ids import default_should_descend, iter_hir_walk

				def _no_descend_into_lambda(obj: object) -> bool:
					if isinstance(obj, H.HLambda):
						return False
					return default_should_descend(obj)

				ids: set[int] = set()
				for obj in iter_hir_walk(block, should_descend=_no_descend_into_lambda):
					if isinstance(obj, (H.HCall, H.HMethodCall, H.HInvoke)):
						csid = getattr(obj, "callsite_id", None)
						if isinstance(csid, int):
							ids.add(csid)
							callsite_nodes_by_id.setdefault(csid, obj)
						else:
							missing_callsite_nodes.append(obj)
				return ids

			callsite_ids = _collect_callsite_ids(body)
			for csid in sorted(callsite_ids):
				if csid not in call_info_by_callsite_id:
					missing_info.append(csid)
			if (
				not any(getattr(d, "severity", None) == "error" for d in diagnostics)
				and not deferred_guard_diags
			):
				if missing_callsite_nodes:
					first_missing_node = missing_callsite_nodes[0]
					diagnostics.append(
						_tc_diag(
							message=(
								"internal: missing callsite_id on call nodes "
								f"in '{function_symbol(fn_id)}' (nodes: {sorted((getattr(n, 'node_id', -1) for n in missing_callsite_nodes))[:5]})"
							),
							severity="error",
							span=Span.from_loc(getattr(first_missing_node, "loc", None)),
						)
					)
				if missing_info:
					first_missing_info_node = callsite_nodes_by_id.get(sorted(missing_info)[0])
					diagnostics.append(
						_tc_diag(
							message=(
								"internal: missing CallInfo for callsite_id "
								f"in '{function_symbol(fn_id)}' (ids: {sorted(missing_info)[:5]})"
							),
							severity="error",
							span=Span.from_loc(getattr(first_missing_info_node, "loc", None)),
						)
					)
					if drift_debug.enabled("callsite"):
						for csid in sorted(missing_info)[:6]:
							node = callsite_nodes_by_id.get(csid)
							kind = type(node).__name__ if node is not None else "unknown"
							name = None
							fn_kind = None
							fn_mod = None
							if isinstance(node, H.HCall):
								fn = getattr(node, "fn", None)
								name = getattr(fn, "name", None) or getattr(fn, "member", None)
								fn_kind = type(fn).__name__ if fn is not None else None
								fn_mod = getattr(fn, "module_id", None)
							elif isinstance(node, H.HMethodCall):
								name = getattr(node, "method_name", None)
							print(f"[callsite] missing CallInfo fn={function_symbol(fn_id)} csid={csid} kind={kind} fn_kind={fn_kind} name={name} module_id={fn_mod}", file=sys.stderr)
		if callable_registry is not None:
			if not any(getattr(d, "severity", None) == "error" for d in diagnostics) and not deferred_guard_diags:
				sig_info = signatures_by_id.get(fn_id) if signatures_by_id is not None else None
				typed_validation = validate_typed_hir(
					body,
					call_info_by_callsite_id=call_info_by_callsite_id,
					expr_types=expr_types,
					type_table=self.type_table,
					tc_diag=_tc_diag,
					current_module_name=current_module_name,
					unsafe_trusted_modules=self._unsafe_trusted_modules,
					mir_bound=bool(getattr(sig_info, "is_mir_bound", False)) if sig_info is not None else False,
				)
				if typed_validation.diagnostics:
					diagnostics.extend(typed_validation.diagnostics)

		# Seed origin for reference parameters.
		for bid in param_bindings:
			pty = binding_types.get(bid)
			if pty is not None and self.type_table.get(pty).kind is TypeKind.REF:
				ref_origin_param[bid] = bid

		def _return_origin(expr: H.HExpr) -> Optional[int]:
			if isinstance(expr, H.HCall):
				if isinstance(expr.fn, H.HVar) and expr.fn.module_id == "std.mem" and expr.fn.name in ("ptr_at_ref", "ptr_at_mut"):
					if expr.args:
						return _return_origin(expr.args[0])
			if isinstance(expr, H.HMethodCall):
				origin = _return_origin(expr.receiver)
				if origin is not None:
					return origin
			# Returning an existing reference value (param or local ref).
			if isinstance(expr, H.HVar) and getattr(expr, "binding_id", None) is not None:
				return ref_origin_param.get(expr.binding_id)
			if hasattr(H, "HPlaceExpr") and isinstance(expr, getattr(H, "HPlaceExpr")):
				if isinstance(expr.base, H.HVar) and getattr(expr.base, "binding_id", None) is not None:
					return ref_origin_param.get(expr.base.binding_id)
			# Returning a borrow is only allowed when it reborrows through a ref
			# that originates from a reference parameter (e.g. &(*p).x).
			if isinstance(expr, H.HBorrow):
				def _base_lookup(hv: object) -> Optional[PlaceBase]:
					bid = getattr(hv, "binding_id", None)
					if bid is None:
						return None
					kind = binding_place_kind.get(bid, PlaceKind.LOCAL)
					name = hv.name if hasattr(hv, "name") else str(hv)
					return PlaceBase(kind=kind, local_id=bid, name=name)

				sub_place = place_from_expr(expr.subject, base_lookup=_base_lookup)
				if sub_place is None:
					return None
				if sub_place.base.local_id in ref_origin_param:
					return ref_origin_param.get(sub_place.base.local_id)
				if not any(isinstance(p, DerefProj) for p in sub_place.projections):
					return None
				return ref_origin_param.get(sub_place.base.local_id)
			return None

		def _struct_field_layout(struct_ty: TypeId) -> tuple[list[str], list[TypeId]] | None:
			inst = self.type_table.get_struct_instance(struct_ty)
			if inst is not None:
				return list(inst.field_names), list(inst.field_types)
			td = self.type_table.get(struct_ty)
			if td.kind is not TypeKind.STRUCT or td.field_names is None:
				return None
			return list(td.field_names), list(td.param_types)

		def _is_borrowed_aggregate_type(ty: TypeId) -> bool:
			td = self.type_table.get(ty)
			if td.kind is not TypeKind.STRUCT:
				return False
			layout = _struct_field_layout(ty)
			if layout is None:
				return False
			_field_names, field_types = layout
			return any(self.type_table.get(ft).kind is TypeKind.REF for ft in field_types)

		def _lambda_capture_root_ids(lam: H.HLambda) -> set[int]:
			out: set[int] = set()
			for cap in getattr(lam, "explicit_captures", None) or []:
				bid = getattr(cap, "binding_id", None)
				if bid is not None:
					out.add(int(bid))
			for cap in getattr(lam, "captures", []) or []:
				key = getattr(cap, "key", None)
				root = getattr(key, "root_local", None) if key is not None else None
				if root is not None:
					out.add(int(root))
			return out

		def _lambda_captures_borrowed_aggregate(lam: H.HLambda) -> bool:
			for bid in _lambda_capture_root_ids(lam):
				ty = binding_types.get(bid)
				if ty is not None and _is_borrowed_aggregate_type(ty):
					return True
			return False

		def _borrowed_aggregate_requires_mut_origin(ty: TypeId) -> bool:
			td = self.type_table.get(ty)
			if td.kind is not TypeKind.STRUCT:
				return False
			layout = _struct_field_layout(ty)
			if layout is None:
				return False
			_field_names, field_types = layout
			return any(self.type_table.get(ft).kind is TypeKind.REF and bool(getattr(self.type_table.get(ft), "ref_mut", False)) for ft in field_types)

		def _ctor_name(fn_expr: H.HExpr) -> Optional[str]:
			if isinstance(fn_expr, H.HQualifiedMember):
				return fn_expr.member
			if isinstance(fn_expr, H.HVar):
				return fn_expr.name
			return None

		def _arg_for_field(call: H.HCall, *, field_name: str, field_index: int) -> Optional[H.HExpr]:
			for kw in call.kwargs:
				if kw.name == field_name:
					return kw.value
			if field_index < len(call.args):
				return call.args[field_index]
			return None

		borrowed_return_origins_by_binding: Dict[int, Optional[set[int]]] = {}

		def _borrowed_aggregate_origins(expr: H.HExpr, expected_ty: TypeId) -> Optional[set[int]]:
			if not _is_borrowed_aggregate_type(expected_ty):
				return None
			if not isinstance(expr, H.HCall):
				return None
			layout = _struct_field_layout(expected_ty)
			if layout is None:
				return None
			field_names, field_types = layout
			out: set[int] = set()
			for idx, fty in enumerate(field_types):
				ftd = self.type_table.get(fty)
				if ftd.kind is not TypeKind.REF:
					continue
				arg_expr = _arg_for_field(expr, field_name=field_names[idx], field_index=idx)
				if arg_expr is None:
					return None
				origin = _return_origin(arg_expr)
				if origin is None:
					if isinstance(arg_expr, H.HBorrow):
						return set()
					if isinstance(arg_expr, H.HVar) and getattr(arg_expr, "binding_id", None) is not None:
						if arg_expr.binding_id in ref_origin_param:
							return set()
					return None
				out.add(origin)
			return out

		def _return_borrowed_aggregate_origins(expr: H.HExpr, expected_ty: TypeId) -> Optional[set[int]]:
			d = self.type_table.get(expected_ty)
			if isinstance(expr, H.HVar) and getattr(expr, "binding_id", None) is not None:
				origins_cached = borrowed_return_origins_by_binding.get(int(expr.binding_id))
				if origins_cached is not None:
					return set(origins_cached)
			if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
				return _return_borrowed_aggregate_origins(expr.subject, expected_ty)
			if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
				return _return_borrowed_aggregate_origins(expr.subject, expected_ty)
			if _is_borrowed_aggregate_type(expected_ty):
				return _borrowed_aggregate_origins(expr, expected_ty)
			if d.kind is not TypeKind.VARIANT:
				return None
			inst = self.type_table.get_variant_instance(expected_ty)
			if inst is None:
				return None
			if not isinstance(expr, H.HCall):
				return None
			ctor = _ctor_name(expr.fn)
			if ctor is None:
				return None
			arm = inst.arms_by_name.get(ctor)
			if arm is None or len(arm.field_types) != 1:
				return None
			inner_ty = arm.field_types[0]
			if not _is_borrowed_aggregate_type(inner_ty):
				return None
			inner_arg = _arg_for_field(
				expr,
				field_name=arm.field_names[0] if arm.field_names else "value",
				field_index=0,
			)
			if inner_arg is None:
				return None
			return _borrowed_aggregate_origins(inner_arg, inner_ty)

		def _return_type_carries_borrowed_aggregate(ty: TypeId) -> tuple[bool, bool]:
			if _is_borrowed_aggregate_type(ty):
				return True, _borrowed_aggregate_requires_mut_origin(ty)
			td = self.type_table.get(ty)
			if td.kind is not TypeKind.VARIANT:
				return False, False
			inst = self.type_table.get_variant_instance(ty)
			if inst is None:
				return False, False
			for arm in inst.arms:
				if len(arm.field_types) != 1:
					continue
				inner_ty = arm.field_types[0]
				if _is_borrowed_aggregate_type(inner_ty):
					return True, _borrowed_aggregate_requires_mut_origin(inner_ty)
			return False, False

		def _merge_borrowed_origin_maps(
			left: dict[int, Optional[set[int]]], right: dict[int, Optional[set[int]]]
		) -> dict[int, Optional[set[int]]]:
			out: dict[int, Optional[set[int]]] = {}
			for bid in sorted(set(left.keys()) | set(right.keys())):
				lv = left.get(bid)
				rv = right.get(bid)
				if lv is None or rv is None:
					out[bid] = None
					continue
				if lv == rv:
					out[bid] = set(lv)
				else:
					out[bid] = None
			return out

		def _infer_borrowed_return_origins(block: H.HBlock) -> dict[int, Optional[set[int]]]:
			def _expr_origins(
				expr: H.HExpr,
				expected_ty: TypeId,
				env: dict[int, Optional[set[int]]],
			) -> Optional[set[int]]:
				if isinstance(expr, H.HVar) and getattr(expr, "binding_id", None) is not None:
					v = env.get(int(expr.binding_id))
					if v is None:
						return None
					return set(v)
				if hasattr(H, "HMove") and isinstance(expr, getattr(H, "HMove")):
					return _expr_origins(expr.subject, expected_ty, env)
				if hasattr(H, "HCopy") and isinstance(expr, getattr(H, "HCopy")):
					return _expr_origins(expr.subject, expected_ty, env)
				return _return_borrowed_aggregate_origins(expr, expected_ty)

			def _walk(cur: H.HBlock, in_env: dict[int, Optional[set[int]]]) -> dict[int, Optional[set[int]]]:
				env: dict[int, Optional[set[int]]] = {k: (None if v is None else set(v)) for k, v in in_env.items()}
				for s in cur.statements:
					if isinstance(s, H.HLet) and s.binding_id is not None:
						bid = int(s.binding_id)
						ty = binding_types.get(bid)
						if ty is not None and _return_type_carries_borrowed_aggregate(ty)[0]:
							env[bid] = _expr_origins(s.value, ty, env)
					elif isinstance(s, H.HAssign) and isinstance(s.target, H.HVar) and getattr(s.target, "binding_id", None) is not None:
						bid = int(s.target.binding_id)
						ty = binding_types.get(bid)
						if ty is not None and _return_type_carries_borrowed_aggregate(ty)[0]:
							env[bid] = _expr_origins(s.value, ty, env)
					elif isinstance(s, H.HBlock):
						env = _walk(s, env)
					elif isinstance(s, H.HIf):
						then_env = _walk(s.then_block, env)
						else_env = _walk(s.else_block, env) if s.else_block is not None else env
						env = _merge_borrowed_origin_maps(then_env, else_env)
					elif isinstance(s, H.HTry):
						body_env = _walk(s.body, env)
						cur_env = body_env
						for arm in s.catches:
							arm_env = _walk(arm.block, env)
							cur_env = _merge_borrowed_origin_maps(cur_env, arm_env)
						env = cur_env
					elif isinstance(s, H.HLoop):
						loop_env = _walk(s.body, env)
						env = _merge_borrowed_origin_maps(env, loop_env)
					elif hasattr(H, "HUnsafeBlock") and isinstance(s, getattr(H, "HUnsafeBlock")):
						env = _walk(s.block, env)
				return env

			return _walk(block, {})

		borrowed_return_origins_by_binding = _infer_borrowed_return_origins(body)

		def _decl_and_sig_for_call(expr: H.HExpr) -> tuple[CallableDecl | None, FnSignature | None]:
			resolution = call_resolutions.get(getattr(expr, "node_id", None))
			if isinstance(resolution, MethodResolution):
				decl = resolution.decl
			elif isinstance(resolution, CallableDecl):
				decl = resolution
			else:
				decl = None
			if decl is None:
				return None, None
			return decl, signatures_by_id.get(decl.fn_id) if signatures_by_id is not None else None

		def _param_index_for_call(sig: FnSignature, *, arg_index: int | None = None, kw_name: str | None = None) -> Optional[int]:
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
			if sig.is_method:
				return arg_index + 1
			return arg_index

		def _nonretaining_param_state(sig: FnSignature, param_index: int, *, fn_id: FunctionId | None = None) -> Optional[bool]:
			from lang.driftc.borrow_checker import EscapeLevel
			# Production (world-backed): reads from SemanticWorld overlay.
			if self.semantic_world is not None and fn_id is not None:
				pel = self.semantic_world.get_signature_annotation(fn_id, "param_escape_level")
				if pel is None:
					return None
				if param_index >= len(pel) or pel[param_index] is None:
					return None
				lvl = pel[param_index]
				if lvl in (EscapeLevel.IMMEDIATE, EscapeLevel.LOCAL, EscapeLevel.SCOPED):
					return True
				return False
			# Test-only fallback: no SemanticWorld available.
			if sig.param_escape_level is not None and 0 <= param_index < len(sig.param_escape_level):
				lvl = sig.param_escape_level[param_index]
				if lvl is not None:
					if lvl in (EscapeLevel.IMMEDIATE, EscapeLevel.LOCAL, EscapeLevel.SCOPED):
						return True
					return False
			return None

		def _check_borrowed_arg_boundary(
			*,
			arg_expr: H.HExpr,
			param_ty: Optional[TypeId],
			nonretaining_state: Optional[bool],
			retaining_default: bool,
			target_name: str,
			target_module: str | None,
			param_label: str,
			span: Span,
		) -> None:
			if param_ty is None:
				return
			if target_name == "drop_value":
				return
			if isinstance(arg_expr, H.HLambda):
				param_td = self.type_table.get(param_ty)
				should_reject = (nonretaining_state is False) or retaining_default
				if param_td.kind is not TypeKind.REF and should_reject and _lambda_captures_borrowed_aggregate(arg_expr):
					diagnostics.append(
						_tc_diag(
							message=(
								f"lambda capturing borrowed aggregate cannot escape through retaining {param_label} of '{target_name}' "
								"(generic/default-retaining boundary); require explicit non-retaining parameter"
							),
							severity="error",
							span=span,
						)
					)
				return
			arg_ty = expr_types.get(getattr(arg_expr, "node_id", None))
			if arg_ty is None and isinstance(arg_expr, H.HVar) and getattr(arg_expr, "binding_id", None) is not None:
				arg_ty = binding_types.get(arg_expr.binding_id)
			if arg_ty is None and hasattr(H, "HMove") and isinstance(arg_expr, getattr(H, "HMove")):
				subj = arg_expr.subject
				arg_ty = expr_types.get(getattr(subj, "node_id", None))
				if arg_ty is None and isinstance(subj, H.HVar) and getattr(subj, "binding_id", None) is not None:
					arg_ty = binding_types.get(subj.binding_id)
			if arg_ty is None and hasattr(H, "HCopy") and isinstance(arg_expr, getattr(H, "HCopy")):
				subj = arg_expr.subject
				arg_ty = expr_types.get(getattr(subj, "node_id", None))
				if arg_ty is None and isinstance(subj, H.HVar) and getattr(subj, "binding_id", None) is not None:
					arg_ty = binding_types.get(subj.binding_id)
			if arg_ty is None:
				return
			if not _is_borrowed_aggregate_type(arg_ty):
				return
			param_td = self.type_table.get(param_ty)
			if param_td.kind is TypeKind.REF:
				return
			if nonretaining_state is True:
				return
			should_reject = (nonretaining_state is False) or retaining_default
			if not should_reject:
				return
			diagnostics.append(
				_tc_diag(
					message=(
						f"borrowed aggregate argument cannot flow through retaining {param_label} of '{target_name}' "
						"(generic/default-retaining boundary); require explicit non-retaining parameter"
					),
					severity="error",
					span=span,
				)
			)

		def _check_call_expr_boundaries(expr: H.HExpr) -> None:
			if isinstance(expr, H.HCall):
				decl, sig = _decl_and_sig_for_call(expr)
				if decl is not None and sig is not None:
					for idx, arg in enumerate(expr.args):
						param_index = _param_index_for_call(sig, arg_index=idx)
						param_ty = None
						param_label = f"parameter #{param_index}" if param_index is not None else "parameter"
						nonret_state: Optional[bool] = None
						retaining_default = False
						if param_index is not None and sig.param_type_ids and param_index < len(sig.param_type_ids):
							param_ty = sig.param_type_ids[param_index]
							nonret_state = _nonretaining_param_state(sig, param_index, fn_id=getattr(decl, "fn_id", None) if decl is not None else None)
							retaining_default = self.type_table.get(param_ty).kind is TypeKind.TYPEVAR
							if sig.param_names and param_index < len(sig.param_names):
								param_label = f"parameter '{sig.param_names[param_index]}'"
						_check_borrowed_arg_boundary(
						arg_expr=arg,
						param_ty=param_ty,
						nonretaining_state=nonret_state,
						retaining_default=retaining_default,
						target_name=decl.fn_id.name,
						target_module=decl.fn_id.module,
						param_label=param_label,
						span=getattr(arg, "loc", getattr(expr, "loc", Span())),
					)
					for kw in expr.kwargs:
						param_index = _param_index_for_call(sig, kw_name=kw.name)
						param_ty = None
						param_label = f"parameter #{param_index}" if param_index is not None else "parameter"
						nonret_state: Optional[bool] = None
						retaining_default = False
						if param_index is not None and sig.param_type_ids and param_index < len(sig.param_type_ids):
							param_ty = sig.param_type_ids[param_index]
							nonret_state = _nonretaining_param_state(sig, param_index, fn_id=getattr(decl, "fn_id", None) if decl is not None else None)
							retaining_default = self.type_table.get(param_ty).kind is TypeKind.TYPEVAR
							if sig.param_names and param_index < len(sig.param_names):
								param_label = f"parameter '{sig.param_names[param_index]}'"
						_check_borrowed_arg_boundary(
						arg_expr=kw.value,
						param_ty=param_ty,
						nonretaining_state=nonret_state,
						retaining_default=retaining_default,
						target_name=decl.fn_id.name,
						target_module=decl.fn_id.module,
						param_label=param_label,
						span=getattr(kw.value, "loc", getattr(expr, "loc", Span())),
					)
					return
				call_info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if call_info is None:
					return
				if call_info.target.kind is not CallTargetKind.INTRINSIC:
					return
				if call_info.target.intrinsic not in {
					IntrinsicKind.CALLBACK0,
					IntrinsicKind.CALLBACK1,
					IntrinsicKind.CALLBACK2,
					IntrinsicKind.CALLBACK_THROW0,
					IntrinsicKind.CALLBACK_THROW1,
					IntrinsicKind.CALLBACK_THROW2,
				}:
					return
				target_name = call_info.target.intrinsic.name if call_info.target.intrinsic is not None else "intrinsic"
				param_types = list(call_info.sig.param_types)
				for idx, arg in enumerate(expr.args):
					param_ty = param_types[idx] if idx < len(param_types) else None
					_check_borrowed_arg_boundary(
						arg_expr=arg,
						param_ty=param_ty,
						nonretaining_state=None,
						retaining_default=True,
						target_name=target_name,
						target_module=None,
						param_label=f"parameter #{idx}",
						span=getattr(arg, "loc", getattr(expr, "loc", Span())),
					)
				for kw in expr.kwargs:
					_check_borrowed_arg_boundary(
						arg_expr=kw.value,
						param_ty=None,
						nonretaining_state=None,
						retaining_default=False,
						target_name=target_name,
						target_module=None,
						param_label=f"parameter '{kw.name}'",
						span=getattr(kw.value, "loc", getattr(expr, "loc", Span())),
					)
			elif isinstance(expr, H.HMethodCall):
				decl, sig = _decl_and_sig_for_call(expr)
				if decl is not None and sig is not None:
					recv_param_ty = None
					recv_nonret_state: Optional[bool] = None
					recv_retaining_default = False
					if sig.is_method and sig.param_type_ids and len(sig.param_type_ids) > 0:
						recv_param_ty = sig.param_type_ids[0]
						recv_nonret_state = _nonretaining_param_state(sig, 0, fn_id=getattr(decl, "fn_id", None) if decl is not None else None)
						recv_retaining_default = self.type_table.get(recv_param_ty).kind is TypeKind.TYPEVAR
					_check_borrowed_arg_boundary(
						arg_expr=expr.receiver,
						param_ty=recv_param_ty,
						nonretaining_state=recv_nonret_state,
						retaining_default=recv_retaining_default,
						target_name=decl.fn_id.name,
						target_module=decl.fn_id.module,
						param_label="receiver parameter",
						span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
					)
					for idx, arg in enumerate(expr.args):
						param_index = _param_index_for_call(sig, arg_index=idx)
						param_ty = None
						param_label = f"parameter #{param_index}" if param_index is not None else "parameter"
						nonret_state: Optional[bool] = None
						retaining_default = False
						if param_index is not None and sig.param_type_ids and param_index < len(sig.param_type_ids):
							param_ty = sig.param_type_ids[param_index]
							nonret_state = _nonretaining_param_state(sig, param_index, fn_id=getattr(decl, "fn_id", None) if decl is not None else None)
							retaining_default = self.type_table.get(param_ty).kind is TypeKind.TYPEVAR
							if sig.param_names and param_index < len(sig.param_names):
								param_label = f"parameter '{sig.param_names[param_index]}'"
						_check_borrowed_arg_boundary(
							arg_expr=arg,
							param_ty=param_ty,
							nonretaining_state=nonret_state,
							retaining_default=retaining_default,
							target_name=decl.fn_id.name,
							target_module=decl.fn_id.module,
							param_label=param_label,
							span=getattr(arg, "loc", getattr(expr, "loc", Span())),
						)
					for kw in expr.kwargs:
						param_index = _param_index_for_call(sig, kw_name=kw.name)
						param_ty = None
						param_label = f"parameter #{param_index}" if param_index is not None else "parameter"
						nonret_state: Optional[bool] = None
						retaining_default = False
						if param_index is not None and sig.param_type_ids and param_index < len(sig.param_type_ids):
							param_ty = sig.param_type_ids[param_index]
							nonret_state = _nonretaining_param_state(sig, param_index, fn_id=getattr(decl, "fn_id", None) if decl is not None else None)
							retaining_default = self.type_table.get(param_ty).kind is TypeKind.TYPEVAR
							if sig.param_names and param_index < len(sig.param_names):
								param_label = f"parameter '{sig.param_names[param_index]}'"
						_check_borrowed_arg_boundary(
							arg_expr=kw.value,
							param_ty=param_ty,
							nonretaining_state=nonret_state,
							retaining_default=retaining_default,
							target_name=decl.fn_id.name,
							target_module=decl.fn_id.module,
							param_label=param_label,
							span=getattr(kw.value, "loc", getattr(expr, "loc", Span())),
						)
					return
				call_info = call_info_by_callsite_id.get(getattr(expr, "callsite_id", None))
				if call_info is None or call_info.target.kind is not CallTargetKind.INTRINSIC:
					return
				intr_name = call_info.target.intrinsic.name if call_info.target.intrinsic is not None else "intrinsic"
				param_types = list(call_info.sig.param_types)
				if param_types:
					_check_borrowed_arg_boundary(
						arg_expr=expr.receiver,
						param_ty=param_types[0],
						nonretaining_state=None,
						retaining_default=True,
						target_name=intr_name,
						target_module=None,
						param_label="receiver parameter",
						span=getattr(expr.receiver, "loc", getattr(expr, "loc", Span())),
					)
				for idx, arg in enumerate(expr.args):
					param_ty = param_types[idx + 1] if idx + 1 < len(param_types) else None
					_check_borrowed_arg_boundary(
						arg_expr=arg,
						param_ty=param_ty,
						nonretaining_state=None,
						retaining_default=True,
						target_name=intr_name,
						target_module=None,
						param_label=f"parameter #{idx + 1}",
						span=getattr(arg, "loc", getattr(expr, "loc", Span())),
					)

		def _walk_expr_for_borrowed_boundaries(expr: H.HExpr) -> None:
			_check_call_expr_boundaries(expr)
			if isinstance(expr, H.HCall):
				_walk_expr_for_borrowed_boundaries(expr.fn)
				for a in expr.args:
					_walk_expr_for_borrowed_boundaries(a)
				for kw in expr.kwargs:
					_walk_expr_for_borrowed_boundaries(kw.value)
			elif isinstance(expr, H.HMethodCall):
				_walk_expr_for_borrowed_boundaries(expr.receiver)
				for a in expr.args:
					_walk_expr_for_borrowed_boundaries(a)
				for kw in expr.kwargs:
					_walk_expr_for_borrowed_boundaries(kw.value)
			elif isinstance(expr, H.HBinary):
				_walk_expr_for_borrowed_boundaries(expr.left)
				_walk_expr_for_borrowed_boundaries(expr.right)
			elif isinstance(expr, H.HUnary):
				_walk_expr_for_borrowed_boundaries(expr.expr)
			elif isinstance(expr, H.HTernary):
				_walk_expr_for_borrowed_boundaries(expr.cond)
				_walk_expr_for_borrowed_boundaries(expr.then_expr)
				_walk_expr_for_borrowed_boundaries(expr.else_expr)
			elif isinstance(expr, H.HArrayLiteral):
				for el in expr.elements:
					_walk_expr_for_borrowed_boundaries(el)
			elif isinstance(expr, H.HMatchExpr):
				_walk_expr_for_borrowed_boundaries(expr.scrutinee)
				for arm in expr.arms:
					_walk_expr_for_borrowed_boundaries(arm.result)

		def _walk_block_for_borrowed_boundaries(block: H.HBlock) -> None:
			for s in block.statements:
				if isinstance(s, H.HLet):
					_walk_expr_for_borrowed_boundaries(s.value)
				elif isinstance(s, H.HAssign):
					_walk_expr_for_borrowed_boundaries(s.target)
					_walk_expr_for_borrowed_boundaries(s.value)
				elif isinstance(s, H.HExprStmt):
					_walk_expr_for_borrowed_boundaries(s.expr)
				elif isinstance(s, H.HReturn):
					if s.value is not None:
						_walk_expr_for_borrowed_boundaries(s.value)
				elif isinstance(s, H.HIf):
					_walk_expr_for_borrowed_boundaries(s.cond)
					_walk_block_for_borrowed_boundaries(s.then_block)
					if s.else_block is not None:
						_walk_block_for_borrowed_boundaries(s.else_block)
				elif isinstance(s, H.HLoop):
					_walk_block_for_borrowed_boundaries(s.body)
				elif isinstance(s, H.HTry):
					_walk_block_for_borrowed_boundaries(s.body)
					for arm in s.catches:
						_walk_block_for_borrowed_boundaries(arm.block)
				elif isinstance(s, H.HBlock):
					_walk_block_for_borrowed_boundaries(s)
				elif hasattr(H, "HUnsafeBlock") and isinstance(s, getattr(H, "HUnsafeBlock")):
					_walk_block_for_borrowed_boundaries(s.block)

		_walk_block_for_borrowed_boundaries(body)

		# MVP escape policy: reference returns must be derived from a single
		# reference parameter.
		if return_type is not None and self.type_table.get(return_type).kind is TypeKind.REF:

			def _walk_returns(block: H.HBlock, out: List[tuple[Optional[int], Span]]) -> None:
				for s in block.statements:
					if isinstance(s, H.HReturn) and s.value is not None:
						out.append((_return_origin(s.value), getattr(s, "loc", getattr(s.value, "loc", Span()))))
					elif isinstance(s, H.HIf):
						_walk_returns(s.then_block, out)
						if s.else_block:
							_walk_returns(s.else_block, out)
					elif isinstance(s, H.HLoop):
						_walk_returns(s.body, out)
					elif isinstance(s, H.HTry):
						_walk_returns(s.body, out)
						for arm in s.catches:
							_walk_returns(arm.block, out)

			returns: List[tuple[Optional[int], Span]] = []
			_walk_returns(body, returns)

			# Determine the single allowed origin param (if any).
			origin_param: Optional[int] = None
			for origin, span in returns:
				if origin is None:
					diagnostics.append(
						_tc_diag(
							message="reference return must be derived from a reference parameter (MVP escape rule)",
							severity="error",
							span=span,
						)
					)
					continue
				if self.type_table.get(return_type).ref_mut and not binding_param_ref_mut.get(origin, False):
					diagnostics.append(
						_tc_diag(
							message="mutable reference return must derive from an &mut parameter",
							severity="error",
							span=span,
						)
					)
				if origin_param is None:
					origin_param = origin
				elif origin != origin_param:
					diagnostics.append(
						_tc_diag(
							message="reference return must derive from a single reference parameter (cannot return from different params)",
							severity="error",
							span=span,
						)
					)

		# MVP borrowed-aggregate return policy:
		# - return must derive from reference parameter provenance
		# - exactly one origin parameter is allowed
		# - mutable borrowed fields require origin from an &mut parameter
		if return_type is not None:
			carries_borrowed_aggregate, requires_mut_origin = _return_type_carries_borrowed_aggregate(return_type)
			if carries_borrowed_aggregate:
				returns2: List[tuple[Optional[set[int]], Span]] = []

				def _walk_returns_borrowed(block: H.HBlock, out: List[tuple[Optional[set[int]], Span]]) -> None:
					for s in block.statements:
						if isinstance(s, H.HReturn) and s.value is not None:
							out.append(
								(
									_return_borrowed_aggregate_origins(s.value, return_type),
									getattr(s, "loc", getattr(s.value, "loc", Span())),
								)
							)
						elif isinstance(s, H.HIf):
							_walk_returns_borrowed(s.then_block, out)
							if s.else_block:
								_walk_returns_borrowed(s.else_block, out)
						elif isinstance(s, H.HLoop):
							_walk_returns_borrowed(s.body, out)
						elif isinstance(s, H.HTry):
							_walk_returns_borrowed(s.body, out)
							for arm in s.catches:
								_walk_returns_borrowed(arm.block, out)

				_walk_returns_borrowed(body, returns2)
				for origins, span in returns2:
					if origins is None:
						continue
					if len(origins) == 0:
						diagnostics.append(
							_tc_diag(
								message="borrowed aggregate return must derive from a reference parameter (MVP escape rule)",
								severity="error",
								span=span,
							)
						)
						continue
					if len(origins) != 1:
						diagnostics.append(
							_tc_diag(
								message="borrowed aggregate return must derive from a single reference parameter (cannot return from different params)",
								severity="error",
								span=span,
							)
						)
						continue
					origin = next(iter(origins))
					if requires_mut_origin and not binding_param_ref_mut.get(origin, False):
						diagnostics.append(
							_tc_diag(
								message="borrowed aggregate return with mutable references must derive from an &mut parameter",
								severity="error",
								span=span,
							)
						)

		for d in diagnostics:
			self._stamp_diag_phase(d)
		return TypeCheckResult(
			typed_fn=typed,
			diagnostics=diagnostics,
			deferred_guard_diags=deferred_guard_diags,
			guard_outcomes=guard_outcomes,
		)

	def _alloc_param_id(self) -> ParamId:
		pid = self._next_binding_id
		self._next_binding_id += 1
		return pid

	def _alloc_local_id(self) -> LocalId:
		lid = self._next_binding_id
		self._next_binding_id += 1
		return lid


def validate_entrypoint(
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
	diagnostics: list[Diagnostic],
	*,
	entry_module: str,
	entry_name: str,
) -> None:
	entry_defs: list[tuple[FunctionId, FnSignature]] = []
	for fn_id, sig in signatures_by_id.items():
		if sig.is_method:
			continue
		if fn_id.name != entry_name:
			continue
		if fn_id.module != entry_module:
			continue
		entry_defs.append((fn_id, sig))

	if not entry_defs:
		entry_label = entry_name
		missing_entry_span = Span()
		for _fid, sig in signatures_by_id.items():
			cand = Span.from_loc(getattr(sig, "loc", None))
			if cand.line is not None and cand.column is not None:
				missing_entry_span = cand
				break
		diagnostics.append(
			_tc_diag(
				message=f"missing entry point '{entry_label}' for code generation",
				severity="error",
				phase="typecheck",
				span=missing_entry_span,
			)
		)
		return

	def _span_for_sig(sig: FnSignature) -> Span:
		return Span.from_loc(getattr(sig, "loc", None))

	if len(entry_defs) > 1:
		first_id, first_sig = entry_defs[0]
		first_span = _span_for_sig(first_sig)
		for fn_id, sig in entry_defs[1:]:
			diagnostics.append(
				_tc_diag(
					message=f"duplicate entry point definition for '{entry_module}::{entry_name}'",
					severity="error",
					phase="typecheck",
					span=_span_for_sig(sig),
				)
			)
			diagnostics.append(
				_tc_diag(
					message="previous definition of 'main' is here",
					severity="note",
					phase="typecheck",
					span=first_span,
				)
			)
		return

	fn_id, sig = entry_defs[0]
	int_id = type_table.ensure_int()
	string_id = type_table.ensure_string()

	ret_id = sig.return_type_id
	if ret_id is None and sig.return_type is not None:
		ret_id = resolve_opaque_type(sig.return_type, type_table, module_id=sig.module)
	param_ids = sig.param_type_ids
	if param_ids is None and sig.param_types is not None:
		param_ids = [resolve_opaque_type(p, type_table, module_id=sig.module) for p in sig.param_types]
	if param_ids is None:
		param_ids = []

	if ret_id != int_id:
		diagnostics.append(
			_tc_diag(
				message=f"entrypoint {entry_name} must return Int",
				severity="error",
				phase="typecheck",
				span=_span_for_sig(sig),
			)
		)

	params = list(param_ids or [])
	param_names = list(sig.param_names or [])
	if params:
		valid = False
		if len(params) == 1 and len(param_names) == 1 and param_names[0] == "argv":
			td = type_table.get(params[0])
			if td.kind is TypeKind.ARRAY and td.param_types and td.param_types[0] == string_id:
				valid = True
		if not valid:
			diagnostics.append(
				_tc_diag(
					message=f"entrypoint {entry_name} has invalid signature; expected fn() or fn(argv: Array<String>)",
					severity="error",
					phase="typecheck",
					span=_span_for_sig(sig),
				)
			)

	if sig.declared_can_throw is not False:
		diagnostics.append(
			_tc_diag(
				message=f"entrypoint {entry_name} must be declared nothrow (uncaught exceptions are not supported yet)",
				severity="error",
				phase="typecheck",
				span=_span_for_sig(sig),
				notes=["add 'nothrow' to main or handle failures explicitly"],
			)
		)


def validate_entrypoint_main(
	signatures_by_id: Mapping[FunctionId, FnSignature],
	type_table: TypeTable,
	diagnostics: list[Diagnostic],
) -> None:
	validate_entrypoint(
		signatures_by_id,
		type_table,
		diagnostics,
		entry_module="main",
		entry_name="main",
	)
