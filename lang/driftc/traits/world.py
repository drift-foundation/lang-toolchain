# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Mapping

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeKind, TypeParamId
from lang.driftc.parser import ast as parser_ast


@dataclass(frozen=True)
class TraitKey:
	package_id: Optional[str]
	module: Optional[str]
	name: str


@dataclass(frozen=True, eq=False)
class TypeKey:
	package_id: Optional[str]
	module: Optional[str]
	name: str
	args: Tuple["TypeKey", ...] = ()
	# Only meaningful when name == "fn"; None means non-function types.
	fn_throws: Optional[bool] = None

	def __post_init__(self) -> None:
		# Cache the hash on construction. The default frozen-dataclass
		# `__hash__` recursively hashes the `args` tuple, which on deeply
		# nested types (e.g. `Array<Array<...<Int>>>` at d=5000) overflows
		# Python's recursion stack inside `tuple.__hash__`.
		#
		# Because `type_key_from_typeid` builds TypeKeys bottom-up, every
		# entry in `self.args` is an already-constructed TypeKey whose
		# `_cached_hash` is set. Therefore `hash(a)` for each child returns
		# the cached integer with no recursion into the child's args; the
		# total work here is O(len(args)) per node and the whole-tree
		# computation is O(N) over the type DAG without any stack growth
		# proportional to nesting depth.
		#
		# Surfaced by the row #11 cleanup pass on the robustness matrix.
		arg_hashes = tuple(hash(a) for a in self.args)
		h = hash((self.package_id, self.module, self.name, self.fn_throws, arg_hashes))
		object.__setattr__(self, "_cached_hash", h)

	def __hash__(self) -> int:
		return self._cached_hash  # type: ignore[attr-defined]

	def __eq__(self, other: object) -> bool:
		# Iterative deep equality. The auto-generated dataclass `__eq__`
		# compares the `args` tuple element-wise via `tuple.__eq__`, which
		# recursively calls `TypeKey.__eq__` on each pair — overflowing
		# Python's recursion stack on deeply nested types at d≥5000.
		#
		# Strategy: short-circuit on cached hash inequality (no false
		# negatives because equal objects have equal hashes), then walk
		# both trees in lockstep using a `(left, right)` pair stack.
		# Returns False on the first structural mismatch.
		if self is other:
			return True
		if other.__class__ is not TypeKey:
			return NotImplemented
		if self._cached_hash != other._cached_hash:  # type: ignore[attr-defined]
			return False
		stack: list[tuple["TypeKey", "TypeKey"]] = [(self, other)]
		while stack:
			a, b = stack.pop()
			if a is b:
				continue
			if (
				a.package_id != b.package_id
				or a.module != b.module
				or a.name != b.name
				or a.fn_throws != b.fn_throws
			):
				return False
			if len(a.args) != len(b.args):
				return False
			# Push child pairs for deeper comparison.
			for ca, cb in zip(a.args, b.args):
				if ca is cb:
					continue
				stack.append((ca, cb))
		return True

	def head(self) -> "TypeHeadKey":
		return TypeHeadKey(package_id=self.package_id, module=self.module, name=self.name)


@dataclass(frozen=True)
class TypeHeadKey:
	package_id: Optional[str]
	module: Optional[str]
	name: str


@dataclass(frozen=True)
class ImplKey:
	package_id: Optional[str]
	module: Optional[str]
	trait: TraitKey
	target_head: TypeHeadKey
	decl_fingerprint: str


@dataclass(frozen=True)
class FnKey:
	package_id: Optional[str]
	module: Optional[str]
	name: str


@dataclass
class TraitDef:
	key: TraitKey
	name: str
	methods: List[parser_ast.TraitMethodSig]
	require: Optional[parser_ast.TraitExpr]
	loc: Optional[object] = None
	type_params: List[str] = field(default_factory=list)


@dataclass
class ImplDef:
	trait: TraitKey
	trait_args: Tuple[TypeKey, ...]
	target: TypeKey
	target_head: TypeHeadKey
	methods: List[parser_ast.FunctionDef]
	require: Optional[parser_ast.TraitExpr]
	type_params: List[str] = field(default_factory=list)
	loc: Optional[object] = None


@dataclass
class TraitWorld:
	traits: Dict[TraitKey, TraitDef] = field(default_factory=dict)
	impls: List[ImplDef] = field(default_factory=list)
	impls_by_trait: Dict[TraitKey, List[int]] = field(default_factory=dict)
	impls_by_target_head: Dict[TypeHeadKey, List[int]] = field(default_factory=dict)
	impls_by_trait_target: Dict[Tuple[TraitKey, TypeHeadKey], List[int]] = field(default_factory=dict)
	requires_by_struct: Dict[TypeKey, parser_ast.TraitExpr] = field(default_factory=dict)
	requires_by_fn: Dict[FunctionId, parser_ast.TraitExpr] = field(default_factory=dict)
	# Interfaces participate in generic `require` constraints the same way
	# traits do: `require T is SomeInterface` proves when `implement
	# SomeInterface for T` exists.  These two side maps let the solver
	# recognize such requirements without polluting the trait-impl
	# registry (which is traversed by other machinery assuming pure
	# trait semantics).
	interfaces: Dict[TraitKey, "InterfaceDef"] = field(default_factory=dict)
	interface_impls_by_iface_target: Dict[Tuple[TraitKey, TypeHeadKey], List["InterfaceImplRef"]] = field(default_factory=dict)
	diagnostics: List[Diagnostic] = field(default_factory=list)


@dataclass
class InterfaceDef:
	"""Minimal interface metadata for generic-constraint purposes.

	Tracks only what the require-solver needs — identity + declaration
	location.  Method signatures, vtable slot ordering, and dispatch
	mechanics live in the interface type-table machinery, not here.
	"""
	key: TraitKey
	name: str
	loc: Optional[object] = None


@dataclass(frozen=True)
class InterfaceImplRef:
	"""Lightweight `implement InterfaceName for Target` record for the
	trait-require solver.

	Carries enough metadata to gate generic/conditional impls in the
	`require T is I` prover (Phase 1 rejects those; full applicability
	checking is a later phase):

	- `type_params`: impl-level type parameters (`implement<T> I for Box<T>`
	  has `type_params = ("T",)`).
	- `require_expr`: impl-level require clause, if any.

	A non-generic, non-conditional impl has `type_params == ()` and
	`require_expr is None`; those are the only impls the Phase 1 solver
	will accept when proving an interface requirement.
	"""
	iface: TraitKey
	target: TypeKey
	target_head: TypeHeadKey
	type_params: Tuple[str, ...] = ()
	require_expr: Optional["parser_ast.TraitExpr"] = None
	loc: Optional[object] = None


def _qual_from_type_expr(typ: parser_ast.TypeExpr) -> Optional[str]:
	return getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)


# Slice 7c-3 (ABI 14, 2026-05-06): "DiagnosticValue" removed —
# `TypeKind.DIAGNOSTICVALUE` and `ensure_diagnostic_value()` are
# deleted; the name is no longer a builtin type and must not be
# treated as one for trait-impl key canonicalization.
BUILTIN_TYPE_NAMES = {"Int", "Bool", "String", "Uint", "Uint64", "Byte", "Float", "Void", "Error"}
BUILTIN_TRAIT_NAMES: set[str] = set()
# Prelude-aliased nominal variants whose canonical type-table key
# lives under `lang.core` rather than the source-file's module.  An
# unqualified mention of one of these names in user/stdlib source
# (e.g., `for Optional<T>` in `stdlib/std/core/copy.drift`) must
# canonicalize to `lang.core::<name>` to match `type_key_from_typeid`'s
# query-side answer for the same type.  Without this, source-side
# trait-impl registration keys impls under the wrong module (the
# source file's module), the trait prover fails to match the impl
# against the canonical query subject, and the impl is silently
# unreachable.  See `work/ownership-ledger/whole-scrutinee-investigation.md`
# (Vector-4 root cause).
#
# Mirror of `_CORE_VARIANT_ALLOWLIST` in
# `lang/driftc/core/type_resolve_common.py`; the two MUST stay in
# sync — the type-resolver uses its set to route unqualified names
# to `lang.core` at type-table interning time, and this set must
# apply the same routing at impl-registration time.
PRELUDE_LANG_CORE_NOMINAL_NAMES = {"Optional"}


def type_key_from_expr(
	typ: parser_ast.TypeExpr,
	*,
	default_module: Optional[str] = None,
	default_package: Optional[str] = None,
	module_packages: Mapping[str, str] | None = None,
) -> TypeKey:
	name = typ.name
	if name == "&":
		name = "Ref"
	elif name == "&mut":
		name = "RefMut"
	qual = _qual_from_type_expr(typ)
	if name in {"Ref", "RefMut"}:
		mod = None
	elif qual is None and name in BUILTIN_TYPE_NAMES:
		mod = None
	elif qual is None and name in PRELUDE_LANG_CORE_NOMINAL_NAMES:
		# Unqualified prelude-aliased nominal — canonicalize to lang.core
		# to match `type_key_from_typeid`'s query-side answer for the
		# same type at the type-table layer.
		mod = "lang.core"
	else:
		mod = qual or default_module
	pkg = None
	if mod == "lang.core":
		# Prelude-aliased nominals live in the lang.core package by
		# convention (see parser/__init__.py:3169-3173 setting
		# `module_packages["lang.core"] = "lang.core"`).  Fix the
		# package alongside the module to keep the canonical key
		# self-consistent regardless of caller's `module_packages`
		# population state.
		pkg = "lang.core"
	elif mod is not None:
		pkg = (module_packages or {}).get(mod, default_package)
	elif name not in BUILTIN_TYPE_NAMES:
		pkg = default_package
	fn_throws = typ.fn_throws_raw() if getattr(typ, "name", None) == "fn" else None
	return TypeKey(
		package_id=pkg,
		module=mod,
		name=name,
		args=tuple(
			type_key_from_expr(
				a,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			)
			for a in getattr(typ, "args", []) or []
		),
		fn_throws=fn_throws,
	)


def _type_id_children(type_table: object, tid: int) -> list[int]:
	"""Return the child tids that participate in `type_key_from_typeid`'s
	recursive descent for a given tid.

	Mirrors the branching in the original recursive form: STRUCT/VARIANT
	with an instance use `inst.type_args`; everything else uses
	`td.param_types`. Factored out so the iterative walker can ask for
	children once per node.
	"""
	td = type_table.get(tid)
	if td.kind is TypeKind.STRUCT:
		inst = type_table.get_struct_instance(tid)
		if inst is not None:
			return list(inst.type_args)
	if td.kind is TypeKind.VARIANT:
		inst = type_table.get_variant_instance(tid)
		if inst is not None:
			return list(inst.type_args)
	return list(getattr(td, "param_types", []) or [])


def type_key_from_typeid(type_table: object, tid: int) -> TypeKey:
	# Iterative post-order builder. The recursive form (one frame per
	# type-nesting level) overflowed Python's recursion stack on deeply
	# nested types like `Array<Array<...<Int>>>` at d≥5000. Surfaced by
	# the row #11 cleanup pass on the robustness matrix; same fix shape
	# as `_type_expr_key` (parser/__init__.py) and rows #2 / #5.
	#
	# Cache is keyed by tid: type tables intern type ids, so the same tid
	# always produces the same key. Caching also dedups shared subtrees
	# in the type DAG (a small win over the recursive form).
	cache: dict[int, TypeKey] = {}
	stack: list[tuple[int, bool]] = [(tid, False)]
	while stack:
		cur_tid, expanded = stack.pop()
		if expanded:
			td = type_table.get(cur_tid)
			module_id = getattr(td, "module_id", None)
			module_packages = getattr(type_table, "module_packages", {}) or {}
			default_package = getattr(type_table, "package_id", None)
			package_id = None
			if module_id is not None:
				package_id = module_packages.get(module_id, default_package)
			child_tids = _type_id_children(type_table, cur_tid)
			args = tuple(cache[c] for c in child_tids)
			fn_throws = None
			if td.kind is TypeKind.FUNCTION:
				fn_throws = bool(getattr(td, "fn_throws", True))
			cache[cur_tid] = TypeKey(
				package_id=package_id,
				module=module_id,
				name=getattr(td, "name", ""),
				args=args,
				fn_throws=fn_throws,
			)
			continue
		if cur_tid in cache:
			continue
		# First visit: schedule the post-order build, then push children.
		stack.append((cur_tid, True))
		for c in _type_id_children(type_table, cur_tid):
			if c not in cache:
				stack.append((c, False))
	return cache[tid]


def normalize_type_key(
	key: TypeKey,
	*,
	module_name: str,
	default_package: Optional[str] = None,
	module_packages: Mapping[str, str] | None = None,
) -> TypeKey:
	"""
	Normalize a TypeKey with the same rule used by trait resolution.

	If the key has no module id, it is resolved to the current module name.
	"""
	if key.module is None:
		if key.name in BUILTIN_TYPE_NAMES:
			return key
		if key.name in ("Ref", "RefMut", "fn"):
			# Structural types have no home module: stamping the CALLER's
			# module onto a reference/function type-arg made the call-side
			# obligation key diverge from the impl-registration key, so
			# `implement Taker<&String> for Sink` was unreachable from
			# `Taker<&String>::take(...)` while `Taker<Int>` worked
			# (LANGUAGE_BUG, found 2026-07-29 via the round-2 W0 pins).
			return key
		pkg = key.package_id or (module_packages or {}).get(module_name, default_package)
		return TypeKey(package_id=pkg, module=module_name, name=key.name, args=key.args, fn_throws=key.fn_throws)
	if key.package_id is None:
		pkg = (module_packages or {}).get(key.module, default_package)
		if pkg is not None:
			return TypeKey(package_id=pkg, module=key.module, name=key.name, args=key.args, fn_throws=key.fn_throws)
	return key


def trait_key_from_expr(
	typ: parser_ast.TypeExpr,
	*,
	default_module: Optional[str] = None,
	default_package: Optional[str] = None,
	module_packages: Mapping[str, str] | None = None,
	type_param_subst: Mapping[object, "TypeKey"] | None = None,
) -> TraitKey:
	# Method-/impl-level type-parameter substitution on the trait
	# side of a `require T is I` clause: if `typ` is a bare
	# unqualified name that matches a type parameter bound in
	# `type_param_subst` (keyed by parameter name), the resolved
	# TraitKey comes from the substituted TypeKey — not the
	# declaration-local name.  This is what makes
	# `fn check<I>(self: &Holder<T>) require T is I` prove
	# correctly at `h.check<type Face>()` rather than refuting
	# against a phantom `<module>.I` trait.
	#
	# Callers that don't pass `type_param_subst` get the original
	# behavior (resolve `typ.name` in the declaration module).
	if type_param_subst:
		name = getattr(typ, "name", None)
		has_args = bool(getattr(typ, "args", None))
		explicit_module = _qual_from_type_expr(typ)
		if name is not None and not has_args and explicit_module is None:
			substituted = type_param_subst.get(name)
			if isinstance(substituted, TypeKey):
				return TraitKey(
					package_id=substituted.package_id,
					module=substituted.module,
					name=substituted.name,
				)
	module = _qual_from_type_expr(typ)
	if module is None:
		module = default_module
	pkg = None
	if module is not None:
		pkg = (module_packages or {}).get(module, default_package)
	return TraitKey(package_id=pkg, module=module, name=typ.name)


def _type_key_str(key: TypeKey | TypeHeadKey) -> str:
	pkg = getattr(key, "package_id", None)
	module = getattr(key, "module", None)
	name = getattr(key, "name", "")
	base = f"{module}.{name}" if module else name
	if pkg:
		base = f"{pkg}::{base}"
	if isinstance(key, TypeKey) and key.args:
		args = ", ".join(_type_key_str(a) for a in key.args)
		return f"{base}<{args}>"
	return base


def _trait_key_str(key: TraitKey) -> str:
	base = f"{key.module}.{key.name}" if key.module else key.name
	if key.package_id:
		return f"{key.package_id}::{base}"
	return base


def type_key_str(key: TypeKey | TypeHeadKey) -> str:
	"""Render a TypeKey/TypeHeadKey as a canonical string label."""
	return _type_key_str(key)


def _diag(message: str, loc: object | None, *, code: str | None = None, phase: str | None = None) -> Diagnostic:
	return Diagnostic(message=message, severity="error", phase=phase, span=Span.from_loc(loc), code=code)


def _collect_trait_is(expr: parser_ast.TraitExpr) -> List[parser_ast.TraitIs]:
	out: List[parser_ast.TraitIs] = []
	if isinstance(expr, parser_ast.TraitIs):
		out.append(expr)
	elif isinstance(expr, (parser_ast.TraitAnd, parser_ast.TraitOr)):
		out.extend(_collect_trait_is(expr.left))
		out.extend(_collect_trait_is(expr.right))
	elif isinstance(expr, parser_ast.TraitNot):
		out.extend(_collect_trait_is(expr.expr))
	return out


def _walk_atoms_all(expr: parser_ast.TraitExpr) -> List[parser_ast.TraitIs]:
	return _collect_trait_is(expr)


def _extract_conjunctive_facts(expr: parser_ast.TraitExpr) -> List[parser_ast.TraitIs]:
	if isinstance(expr, parser_ast.TraitIs):
		return [expr]
	if isinstance(expr, parser_ast.TraitAnd):
		return _extract_conjunctive_facts(expr.left) + _extract_conjunctive_facts(expr.right)
	return []


def _has_non_conjunctive(expr: parser_ast.TraitExpr) -> bool:
	if isinstance(expr, (parser_ast.TraitOr, parser_ast.TraitNot)):
		return True
	if isinstance(expr, parser_ast.TraitAnd):
		return _has_non_conjunctive(expr.left) or _has_non_conjunctive(expr.right)
	return False


def build_trait_world(
	prog: parser_ast.Program,
	*,
	diagnostics: Optional[List[Diagnostic]] = None,
	package_id: Optional[str] = None,
	module_packages: Mapping[str, str] | None = None,
	diag_phase: str | None = None,
) -> TraitWorld:
	if diag_phase is None:
		raise ValueError("build_trait_world requires an explicit diag_phase")
	diags: List[Diagnostic] = diagnostics if diagnostics is not None else []
	world = TraitWorld(diagnostics=diags)
	module_id = getattr(prog, "module", None) or "main"
	local_pkg = (module_packages or {}).get(module_id, package_id)
	def diag(message: str, loc: object | None, *, code: str | None = None) -> Diagnostic:
		return _diag(message, loc, code=code, phase=diag_phase)
	def _subject_name(subject: object) -> str | None:
		if isinstance(subject, parser_ast.SelfRef):
			return "Self"
		if isinstance(subject, parser_ast.TypeNameRef):
			return subject.name
		if isinstance(subject, str):
			return subject
		return None

	# Collect trait declarations.
	local_trait_keys = {
		TraitKey(package_id=local_pkg, module=module_id, name=tr.name)
		for tr in getattr(prog, "traits", []) or []
	}

	# Collect interface declarations.  Interfaces participate in
	# generic `require T is I` constraints the same way traits do, so
	# the require-clause validator below accepts either a local trait
	# key or a local interface key as the named constraint.
	local_interface_keys = {
		TraitKey(package_id=local_pkg, module=module_id, name=iface.name)
		for iface in getattr(prog, "interfaces", []) or []
	}
	for iface in getattr(prog, "interfaces", []) or []:
		iface_key = TraitKey(package_id=local_pkg, module=module_id, name=iface.name)
		if iface_key in world.interfaces:
			continue
		world.interfaces[iface_key] = InterfaceDef(
			key=iface_key,
			name=iface.name,
			loc=getattr(iface, "loc", None),
		)

	def _is_known_local_constraint(k: TraitKey) -> bool:
		# True if `k` is a locally-declared trait OR interface.
		# For non-local keys (module != current), we leave the check
		# unchanged — cross-module resolution is the caller's concern.
		if k.module != module_id:
			return True
		return k in local_trait_keys or k in local_interface_keys
	method_seen: Dict[Tuple[TraitKey, str], object | None] = {}
	for tr in getattr(prog, "traits", []) or []:
		key = TraitKey(package_id=local_pkg, module=module_id, name=tr.name)
		if key in world.traits:
			world.diagnostics.append(diag(f"duplicate trait definition '{_trait_key_str(key)}'", tr.loc))
			continue
		trait_type_params = list(getattr(tr, "type_params", []) or [])
		require_expr = getattr(tr, "require", None).expr if getattr(tr, "require", None) is not None else None
		world.traits[key] = TraitDef(
			key=key,
			name=tr.name,
			methods=list(getattr(tr, "methods", []) or []),
			require=require_expr,
			loc=getattr(tr, "loc", None),
			type_params=trait_type_params,
		)
		if require_expr is not None:
			if _has_non_conjunctive(require_expr):
				world.diagnostics.append(
					diag(
						"trait require clause only supports conjunctions of 'Self is Trait'",
						getattr(require_expr, "loc", None),
						code="E-TRAIT-REQUIRE-UNSUPPORTED",
					)
				)
				continue
			for atom in _walk_atoms_all(require_expr):
				subj_name = _subject_name(atom.subject)
				if subj_name != "Self" and subj_name not in trait_type_params:
					world.diagnostics.append(
						diag(
							"trait require clause must use 'Self' or a trait type parameter",
							getattr(atom, "loc", None),
							code="E-REQUIRE-UNKNOWN-SUBJECT",
						)
					)
					continue
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=module_packages,
				)
				if not _is_known_local_constraint(trait_key):
					world.diagnostics.append(
						diag(
							f"unknown trait '{_trait_key_str(trait_key)}' in require clause",
							getattr(atom, "loc", None),
						)
					)
		for m in getattr(tr, "methods", []) or []:
			mkey = (key, m.name)
			if mkey in method_seen:
				world.diagnostics.append(
					diag(
						f"duplicate method '{m.name}' in trait '{_trait_key_str(key)}'",
						getattr(m, "loc", None),
					)
				)
			else:
				method_seen[mkey] = getattr(m, "loc", None)

	# Collect require clauses for structs and functions.
	for s in getattr(prog, "structs", []) or []:
		if getattr(s, "require", None) is None:
			continue
		type_key = TypeKey(package_id=local_pkg, module=module_id, name=s.name, args=())
		req_expr = s.require.expr
		world.requires_by_struct[type_key] = req_expr
		type_param_names = set(getattr(s, "type_params", []) or [])
		for atom in _walk_atoms_all(req_expr):
			subj = atom.subject
			subj_name = _subject_name(subj)
			if subj_name == "Self" or (subj_name is not None and subj_name in type_param_names):
				# Same accept-struct-type-param-as-trait rule as the
				# function path below: `struct Holder<T, I> require
				# T is I` names the method-/struct-level type
				# parameter `I` as the required trait; it's bound to
				# the caller's actual trait/interface at instantiation
				# time, so it's valid at the declaration site.
				trait_expr_name = getattr(atom.trait, "name", None)
				trait_expr_has_args = bool(getattr(atom.trait, "args", None))
				trait_expr_module = getattr(atom.trait, "module_id", None) or getattr(atom.trait, "module_alias", None)
				trait_is_struct_type_param = (
					trait_expr_name is not None
					and not trait_expr_has_args
					and trait_expr_module is None
					and trait_expr_name in type_param_names
				)
				if trait_is_struct_type_param:
					continue
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=module_packages,
				)
				if not _is_known_local_constraint(trait_key):
					world.diagnostics.append(
						diag(
							f"unknown trait '{_trait_key_str(trait_key)}' in require clause",
							getattr(atom, "loc", None),
						)
					)
			else:
				world.diagnostics.append(
					diag(
						"require clause on struct must use a type parameter or 'Self'",
						getattr(atom, "loc", None),
						code="E-REQUIRE-UNKNOWN-SUBJECT",
					)
				)

	name_ord: Dict[str, int] = {}
	for fn in getattr(prog, "functions", []) or []:
		ordinal = name_ord.get(fn.name, 0)
		name_ord[fn.name] = ordinal + 1
		if getattr(fn, "require", None) is None:
			continue
		req_expr = fn.require.expr
		fn_id = FunctionId(module=module_id, name=fn.name, ordinal=ordinal)
		world.requires_by_fn[fn_id] = req_expr
		type_param_names = set(getattr(fn, "type_params", []) or [])
		for atom in _walk_atoms_all(req_expr):
			subj_name = _subject_name(atom.subject)
			if subj_name == "Self":
				world.diagnostics.append(
					diag(
						"function require clause cannot use 'Self'",
						getattr(atom, "loc", None),
						code="E-REQUIRE-UNKNOWN-SUBJECT",
					)
				)
				continue
			if subj_name is None or subj_name not in type_param_names:
				world.diagnostics.append(
					diag(
						"function require clause must use a type parameter",
						getattr(atom, "loc", None),
						code="E-REQUIRE-UNKNOWN-SUBJECT",
					)
				)
				continue
			# The trait side of a `require` atom may name a function-
			# level type parameter — e.g. `fn check<T, I>(x: T)
			# require T is I`, where `I` is bound to the caller-
			# supplied interface at call sites.  Such references are
			# neither locally-declared traits nor locally-declared
			# interfaces, so the standard `_is_known_local_constraint`
			# check would reject them.  Accept them here; actual
			# proof against the substituted trait happens at the call
			# site (see `trait_key_from_expr(..., type_param_subst=)`).
			trait_expr_name = getattr(atom.trait, "name", None)
			trait_expr_has_args = bool(getattr(atom.trait, "args", None))
			trait_expr_module = getattr(atom.trait, "module_id", None) or getattr(atom.trait, "module_alias", None)
			trait_is_fn_type_param = (
				trait_expr_name is not None
				and not trait_expr_has_args
				and trait_expr_module is None
				and trait_expr_name in type_param_names
			)
			if trait_is_fn_type_param:
				continue
			trait_key = trait_key_from_expr(
				atom.trait,
				default_module=module_id,
				default_package=package_id,
				module_packages=module_packages,
			)
			if not _is_known_local_constraint(trait_key):
				world.diagnostics.append(
					diag(
						f"unknown trait '{_trait_key_str(trait_key)}' in require clause",
						getattr(atom, "loc", None),
					)
				)

	# Collect impls (trait impls + interface impls).
	#
	# Interface impls are kept out of the trait-impl registry
	# (`world.impls_by_trait_target`) because downstream trait machinery
	# assumes pure trait semantics there.  Instead, we register them in
	# `world.interface_impls_by_iface_target` — a parallel index the
	# solver consults when proving `require T is InterfaceName`.
	interface_names = {i.name for i in getattr(prog, "interfaces", []) or []}
	for impl in getattr(prog, "implements", []) or []:
		if getattr(impl, "trait", None) is None:
			continue
		trait_key = trait_key_from_expr(
			impl.trait,
			default_module=module_id,
			default_package=package_id,
			module_packages=module_packages,
		)
		# Sealed `Frozen` trait: in v1, `Frozen` is compiler-derived
		# (auto-derived for structs/variants whose every owned field
		# is Frozen, via the prover-level structural shortcut at
		# `traits/solver.py`).  User code MUST NOT write
		# `implement std.core.shareable.Frozen for X { }` blocks —
		# that would let a user claim Frozen for a type whose
		# `&Self` API exposes mutation (Mutex / Atomic / etc.),
		# breaking the soundness contract.
		#
		# Trust gate (v1 — trusted-stdlib escape hatch, NOT a general
		# rule): the impl is accepted iff BOTH (a) the source file's
		# module id starts with `std.`, AND (b) the host module's
		# package id (`local_pkg`, looked up in `module_packages`)
		# equals the package id of the `Frozen` trait declaration
		# (`trait_key.package_id`, looked up in the same map).
		#
		# The actual trust boundary is the path-based classification
		# in `parser/__init__.py::_is_stdlib_module`: only source
		# files physically under `--stdlib-root` get assigned to the
		# canonical `"std"` package; everything else is the user's
		# `local_pkg`.  Therefore:
		#
		#   - genuine stdlib build: host module under stdlib-root →
		#     local_pkg = "std" = trait_key.package_id → allowed.
		#   - third-party declaring `module std.evil;` in a file NOT
		#     under stdlib-root: host module → local_pkg = user pkg
		#     or "__local__"; stdlib is loaded, so trait_key.package_id
		#     = "std" → mismatch → rejected.
		#   - user replacing stdlib by shipping their own
		#     `std.core.shareable` source under their own root: the
		#     trait identity becomes `(user_pkg, std.core.shareable,
		#     Frozen)` — a different trait from stdlib's; their impl
		#     satisfies their own trait, not stdlib's.  This is
		#     bootstrap trust, not a runtime spoof.
		#
		# See `work/constshare-substrate/phase1a-dispositions.md` §3a.
		if (
			trait_key.module == "std.core.shareable"
			and trait_key.name == "Frozen"
		):
			host_is_stdlib_module = (
				module_id is not None and module_id.startswith("std.")
			)
			host_owns_trait_pkg = (local_pkg == trait_key.package_id)
			if not (host_is_stdlib_module and host_owns_trait_pkg):
				world.diagnostics.append(
					diag(
						(
							"cannot implement Frozen for user types: in v1, "
							"Frozen is compiler-derived (stdlib-baked impls "
							"for primitives + auto-derive for structs / "
							"variants whose every owned field is Frozen); "
							"user-written `implement Frozen for X { }` "
							"blocks are not accepted.  Define your type with "
							"all-Frozen fields and the compiler will derive "
							"Frozen automatically.  See `std.core.shareable` "
							"for the contract."
						),
						getattr(impl, "loc", None),
						code="E_FROZEN_USER_IMPL_REJECTED",
					)
				)
				continue
		# Sealed `ConstShare` trait: same trust gate as `Frozen`.
		# In v1, the only direct stdlib-baked `ConstShare` impl is
		# on `core.ConstArc<T>`.  User struct / variant types acquire
		# `ConstShare` only via the structural composition rule
		# (every owned field is `ConstArc<U>`, another `ConstShare`
		# type, or `Copy + Frozen`), proven by the prover-level
		# shortcut at `traits/solver.py`.  Allowing user-written
		# `implement ConstShare for X` would let user code claim the
		# immutable-shared capability for a type that silently
		# mutates through `&Self`, breaking the soundness contract
		# that `ConstArc` rests on.  Same path-vetted package gate
		# as `Frozen` — see the comment block above for the
		# bootstrap-trust / spoof-resistance reasoning.
		if (
			trait_key.module == "std.core.shareable"
			and trait_key.name == "ConstShare"
		):
			host_is_stdlib_module = (
				module_id is not None and module_id.startswith("std.")
			)
			host_owns_trait_pkg = (local_pkg == trait_key.package_id)
			if not (host_is_stdlib_module and host_owns_trait_pkg):
				world.diagnostics.append(
					diag(
						(
							"cannot implement ConstShare for user types: "
							"in v1, ConstShare is compiler-derived / "
							"stdlib-backed.  The only stdlib-baked impl "
							"is on `core.ConstArc<T>`; user struct / "
							"variant types acquire ConstShare via the "
							"structural composition rule (every owned "
							"field must be `core.ConstArc<U>`, another "
							"ConstShare type, or a `Copy + Frozen` "
							"scalar).  Wrap mutable / unique state in "
							"`core.ConstArc<U>` and the compiler will "
							"derive ConstShare for your struct "
							"automatically.  See "
							"`std.core.shareable.ConstShare` for the "
							"contract."
						),
						getattr(impl, "loc", None),
						code="E_CONST_SHARE_USER_IMPL_REJECTED",
					)
				)
				continue
		if trait_key.module == module_id and trait_key.name in interface_names:
			iface_target_key = type_key_from_expr(
				impl.target,
				default_module=module_id,
				default_package=package_id,
				module_packages=module_packages,
			)
			iface_head_key = iface_target_key.head()
			world.interface_impls_by_iface_target.setdefault(
				(trait_key, iface_head_key), []
			).append(
				InterfaceImplRef(
					iface=trait_key,
					target=iface_target_key,
					target_head=iface_head_key,
					type_params=tuple(getattr(impl, "type_params", []) or ()),
					require_expr=(impl.require.expr if getattr(impl, "require", None) is not None else None),
					loc=getattr(impl, "loc", None),
				)
			)
			continue
		trait_args = tuple(
			type_key_from_expr(
				a,
				default_module=module_id,
				default_package=package_id,
				module_packages=module_packages,
			)
			for a in (getattr(impl.trait, "args", []) or [])
		)
		if trait_key not in local_trait_keys and trait_key.module == module_id:
			world.diagnostics.append(diag(f"unknown trait '{_trait_key_str(trait_key)}' in implement block", getattr(impl, "loc", None)))
		target_key = type_key_from_expr(
			impl.target,
			default_module=module_id,
			default_package=package_id,
			module_packages=module_packages,
		)
		head_key = target_key.head()
		trait_pkg = trait_key.package_id
		target_pkg = head_key.package_id
		if local_pkg is not None:
			if trait_pkg is None and trait_key.module == module_id:
				trait_pkg = local_pkg
			if target_pkg is None and head_key.module == module_id:
				target_pkg = local_pkg
		def _is_local(pkg: Optional[str]) -> bool:
			if local_pkg is None:
				return pkg is None
			return pkg == local_pkg
		if local_pkg is not None:
			missing_pkg = False
			if trait_pkg is None and trait_key.module is not None and trait_key.module != module_id:
				missing_pkg = True
			if target_pkg is None and head_key.module is not None and head_key.module != module_id:
				missing_pkg = True
			if missing_pkg:
				world.diagnostics.append(
					diag(
						"internal: missing package id for trait impl resolution",
						getattr(impl, "loc", None),
					)
				)
		if not _is_local(trait_pkg) and not _is_local(target_pkg):
			world.diagnostics.append(
				diag(
					(
						"orphan trait impl is not allowed: "
						f"trait '{_trait_key_str(trait_key)}' and "
						f"type '{_type_key_str(head_key)}' are outside the current package"
					),
					getattr(impl, "loc", None),
					code="E-IMPL-ORPHAN",
				)
			)
			continue
		req_expr = impl.require.expr if getattr(impl, "require", None) is not None else None
		impl_id = len(world.impls)
		world.impls.append(
			ImplDef(
				trait=trait_key,
				trait_args=trait_args,
				target=target_key,
				target_head=head_key,
				methods=list(getattr(impl, "methods", []) or []),
				require=req_expr,
				type_params=list(getattr(impl, "type_params", []) or []),
				loc=getattr(impl, "loc", None),
			)
		)
		world.impls_by_trait.setdefault(trait_key, []).append(impl_id)
		world.impls_by_target_head.setdefault(head_key, []).append(impl_id)
		world.impls_by_trait_target.setdefault((trait_key, head_key), []).append(impl_id)
		if req_expr is not None:
			for atom in _walk_atoms_all(req_expr):
				trait_dep = trait_key_from_expr(
					atom.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=module_packages,
				)
				# Bug 1 trait-canonicalization extension (2026-04-24):
				# bake the resolved `module_id` back into the require
				# clause's `atom.trait` AST node so the prove-time path
				# (`solver.prove_expr` → `trait_key_from_expr`, which
				# uses the env's `default_module=None`) sees the
				# already-resolved key.  Without this, the prove path
				# computes `Copy` with `module=None`, fails the
				# `world.traits` lookup keyed by `std.core::Copy`, and
				# REFUTES the require — causing every conditional impl
				# (e.g. `implement<T> Copy for Optional<T> require T is
				# Copy`) to silently fail to apply in raw-stdlib builds.
				# Pkg-stdlib's .dmp serialization already bakes the
				# resolved module_id into the trait AST; this matches
				# that behavior at impl-build time.  See
				# `work/ownership-ledger/whole-scrutinee-investigation.md`
				# (Vector-4 root cause).
				if trait_dep.module is not None and getattr(atom.trait, "module_id", None) is None:
					try:
						atom.trait.module_id = trait_dep.module
					except Exception:
						pass  # frozen AST — fall back to env propagation
				if not _is_known_local_constraint(trait_dep):
					world.diagnostics.append(
						diag(
							f"unknown trait '{_trait_key_str(trait_dep)}' in require clause",
							getattr(atom, "loc", None),
						)
					)

	# Coherence/overlap checks (stable ordering).
	def _impl_sort_key(impl_id: int) -> tuple[int, int, int]:
		impl = world.impls[impl_id]
		loc = getattr(impl, "loc", None)
		line = getattr(loc, "line", 0) or 0
		col = getattr(loc, "column", 0) or 0
		return (line, col, impl_id)

	def _coherence_key(item: tuple[TraitKey, TypeHeadKey]) -> tuple[str, str]:
		trait_key, head_key = item
		return (_trait_key_str(trait_key), _type_key_str(head_key))

	for key in sorted(world.impls_by_trait_target.keys(), key=_coherence_key):
		trait_key, head_key = key
		impl_ids = sorted(world.impls_by_trait_target.get(key, []), key=_impl_sort_key)
		if len(impl_ids) <= 1:
			continue
		first = world.impls[impl_ids[0]]
		# DISTINCT TRAIT/INTERFACE INSTANCES are NOT duplicates:
		# `implement Sink<Int> for Box` and `implement Sink<String> for
		# Box` coexist — each instance dispatches through its own impl
		# (the codegen impl index and the checker implements relation are
		# keyed on the exact canonical instance). Coherence is per
		# instance, not per base: a CONCRETE impl collides iff ANY
		# previous concrete impl in the group has the same
		# (target, trait_args) — tracked in `seen_concrete`, NOT
		# compared only against `first` (review finding: with
		# arg-sensitivity, a duplicate pair that is not in first
		# position would otherwise slip through, and codegen's
		# first-wins method merge inside the exact key would recreate
		# ambiguous dispatch for the invalid program). Generic /
		# type-parametric impls keep the legacy conservative
		# vs-first behavior. (Imported interfaces are classified as
		# trait impls in per-module worlds and reclassified after
		# linking, so this check cannot rely on `world.interfaces` —
		# arg-sensitivity is the module-local truth either way.)
		seen_concrete: set = set()
		first_concrete = not getattr(first, "type_params", None)
		if first_concrete:
			seen_concrete.add((first.target, tuple(first.trait_args)))
		for other_id in impl_ids[1:]:
			other = world.impls[other_id]
			other_concrete = not getattr(other, "type_params", None)
			if other_concrete:
				concrete_key = (other.target, tuple(other.trait_args))
				if concrete_key in seen_concrete:
					world.diagnostics.append(diag(
						f"duplicate impl for trait '{_trait_key_str(trait_key)}' on '{_type_key_str(head_key)}'",
						other.loc,
						code="E-IMPL-DUPLICATE",
					))
					continue
				seen_concrete.add(concrete_key)
			if other.target == first.target:
				if other_concrete and first_concrete:
					# Concrete pair, same target: same trait_args was
					# caught by `seen_concrete` above; different
					# trait_args = distinct instances, allowed.
					continue
				msg = f"duplicate impl for trait '{_trait_key_str(trait_key)}' on '{_type_key_str(head_key)}'"
				code = "E-IMPL-DUPLICATE"
			else:
				msg = f"overlapping impls for trait '{_trait_key_str(trait_key)}' on '{_type_key_str(head_key)}'"
				code = "E-IMPL-OVERLAP"
			world.diagnostics.append(diag(msg, other.loc, code=code))

	return world


def _resolve_trait_subjects(
	expr: parser_ast.TraitExpr,
	type_param_map: Dict[str, TypeParamId],
) -> parser_ast.TraitExpr:
	if isinstance(expr, parser_ast.TraitIs):
		subj = expr.subject
		subj_name = None
		if isinstance(subj, parser_ast.TypeNameRef):
			subj_name = subj.name
		elif isinstance(subj, str):
			subj_name = subj
		if subj_name is not None and subj_name in type_param_map:
			return parser_ast.TraitIs(loc=expr.loc, subject=type_param_map[subj_name], trait=expr.trait)
		return expr
	if isinstance(expr, parser_ast.TraitAnd):
		return parser_ast.TraitAnd(
			loc=expr.loc,
			left=_resolve_trait_subjects(expr.left, type_param_map),
			right=_resolve_trait_subjects(expr.right, type_param_map),
		)
	if isinstance(expr, parser_ast.TraitOr):
		return parser_ast.TraitOr(
			loc=expr.loc,
			left=_resolve_trait_subjects(expr.left, type_param_map),
			right=_resolve_trait_subjects(expr.right, type_param_map),
		)
	if isinstance(expr, parser_ast.TraitNot):
		return parser_ast.TraitNot(
			loc=expr.loc,
			expr=_resolve_trait_subjects(expr.expr, type_param_map),
		)
	return expr


def resolve_trait_subjects(
	expr: parser_ast.TraitExpr,
	type_param_map: Dict[str, TypeParamId],
) -> parser_ast.TraitExpr:
	"""Lower trait subjects using a name -> TypeParamId map."""
	return _resolve_trait_subjects(expr, type_param_map)


def resolve_struct_require_subjects(
	world: TraitWorld,
	struct_param_maps: Dict[TypeKey, Dict[str, TypeParamId]],
) -> None:
	"""Lower struct-require subjects from names to TypeParamIds."""
	for ty_key, req in list(world.requires_by_struct.items()):
		type_param_map = struct_param_maps.get(ty_key)
		if not type_param_map:
			continue
		world.requires_by_struct[ty_key] = _resolve_trait_subjects(req, type_param_map)


def resolve_fn_require_subjects(
	world: TraitWorld,
	signatures: Dict[FunctionId, object],
) -> None:
	"""Lower function-require subjects from names to TypeParamIds."""
	for fn_id, req in list(world.requires_by_fn.items()):
		sig = signatures.get(fn_id)
		if sig is None:
			continue
		type_params = getattr(sig, "type_params", []) or []
		if not type_params:
			continue
		type_param_map = {p.name: p.id for p in type_params if hasattr(p, "name") and hasattr(p, "id")}
		if not type_param_map:
			continue
		world.requires_by_fn[fn_id] = _resolve_trait_subjects(req, type_param_map)


__all__ = [
	"TraitWorld",
	"TraitKey",
	"TypeKey",
	"TypeHeadKey",
	"ImplKey",
	"ImplDef",
	"TraitDef",
	"FnKey",
	"build_trait_world",
	"resolve_struct_require_subjects",
	"resolve_trait_subjects",
	"type_key_from_typeid",
	"normalize_type_key",
	"type_key_str",
]
