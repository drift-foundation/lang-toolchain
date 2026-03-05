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


@dataclass(frozen=True)
class TypeKey:
	package_id: Optional[str]
	module: Optional[str]
	name: str
	args: Tuple["TypeKey", ...] = ()
	# Only meaningful when name == "fn"; None means non-function types.
	fn_throws: Optional[bool] = None

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
	diagnostics: List[Diagnostic] = field(default_factory=list)


def _qual_from_type_expr(typ: parser_ast.TypeExpr) -> Optional[str]:
	return getattr(typ, "module_id", None) or getattr(typ, "module_alias", None)


BUILTIN_TYPE_NAMES = {"Int", "Bool", "String", "Uint", "Uint64", "Byte", "Float", "Void", "Error", "DiagnosticValue"}
BUILTIN_TRAIT_NAMES: set[str] = set()


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
	else:
		mod = qual or default_module
	pkg = None
	if mod is not None:
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


def type_key_from_typeid(type_table: object, tid: int) -> TypeKey:
	td = type_table.get(tid)
	module_id = getattr(td, "module_id", None)
	module_packages = getattr(type_table, "module_packages", {}) or {}
	default_package = getattr(type_table, "package_id", None)
	package_id = None
	if module_id is not None:
		package_id = module_packages.get(module_id, default_package)
	if td.kind is TypeKind.STRUCT:
		inst = type_table.get_struct_instance(tid)
		if inst is not None:
			args = tuple(type_key_from_typeid(type_table, t) for t in inst.type_args)
			return TypeKey(package_id=package_id, module=module_id, name=getattr(td, "name", ""), args=args)
	if td.kind is TypeKind.VARIANT:
		inst = type_table.get_variant_instance(tid)
		if inst is not None:
			args = tuple(type_key_from_typeid(type_table, t) for t in inst.type_args)
			return TypeKey(package_id=package_id, module=module_id, name=getattr(td, "name", ""), args=args)
	args = tuple(type_key_from_typeid(type_table, t) for t in getattr(td, "param_types", []) or [])
	fn_throws = None
	if td.kind is TypeKind.FUNCTION:
		fn_throws = bool(getattr(td, "fn_throws", True))
	return TypeKey(package_id=package_id, module=module_id, name=getattr(td, "name", ""), args=args, fn_throws=fn_throws)


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
) -> TraitKey:
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
				if trait_key not in local_trait_keys and trait_key.module == module_id:
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
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=module_id,
					default_package=package_id,
					module_packages=module_packages,
				)
				if trait_key not in local_trait_keys and trait_key.module == module_id:
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
			trait_key = trait_key_from_expr(
				atom.trait,
				default_module=module_id,
				default_package=package_id,
				module_packages=module_packages,
			)
			if trait_key not in local_trait_keys and trait_key.module == module_id:
				world.diagnostics.append(
					diag(
						f"unknown trait '{_trait_key_str(trait_key)}' in require clause",
						getattr(atom, "loc", None),
					)
				)

	# Collect impls (trait impls only).
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
		if trait_key.module == module_id and trait_key.name in interface_names:
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
				if trait_dep not in local_trait_keys and trait_dep.module == module_id:
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
		for other_id in impl_ids[1:]:
			other = world.impls[other_id]
			if other.target == first.target:
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
