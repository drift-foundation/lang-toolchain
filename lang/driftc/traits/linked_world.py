# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.span import Span
from lang.driftc.core.types_core import TypeParamId
from lang.driftc.parser import ast as parser_ast
from lang.driftc.traits.world import (
	BUILTIN_TYPE_NAMES,
	TraitKey,
	TraitWorld,
	TypeKey,
	normalize_type_key,
	trait_key_from_expr,
	type_key_from_expr,
)

BoolExpr = tuple[str, object]
BOOL_TRUE: BoolExpr = ("true", None)
BOOL_FALSE: BoolExpr = ("false", None)


# Linked-world diagnostics are package-phase.
def _link_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "package"
	return Diagnostic(*args, **kwargs)


@dataclass
class AtomInterner:
	"""Map canonical atom keys to compact ids."""

	atom_to_id: dict[tuple[object, object], int] = field(default_factory=dict)
	id_to_atom: list[tuple[object, object]] = field(default_factory=list)

	def intern(self, atom: tuple[object, object]) -> int:
		existing = self.atom_to_id.get(atom)
		if existing is not None:
			return existing
		new_id = len(self.id_to_atom)
		self.atom_to_id[atom] = new_id
		self.id_to_atom.append(atom)
		return new_id

	def lookup(self, atom_id: int) -> tuple[object, object]:
		return self.id_to_atom[atom_id]


@dataclass(frozen=True)
class LinkedWorld:
	"""Immutable merge of per-module trait worlds."""

	trait_worlds: Mapping[str, TraitWorld]
	global_world: TraitWorld

	def visible_world(self, module_names: Iterable[str]) -> TraitWorld:
		names = set(module_names)
		return merge_trait_worlds(world for name, world in self.trait_worlds.items() if name in names)


@dataclass(frozen=True)
class RequireEnv:
	"""Normalized require metadata for proving/ordering."""

	requires_by_fn: Mapping[FunctionId, parser_ast.TraitExpr]
	requires_by_struct: Mapping[TypeKey, parser_ast.TraitExpr]
	trait_requires: Mapping[TraitKey, parser_ast.TraitExpr] = field(default_factory=dict)
	default_package: str | None = None
	module_packages: Mapping[str, str] = field(default_factory=dict)
	atom_interner: AtomInterner = field(default_factory=AtomInterner)
	normalized_cache: dict[tuple[object, ...], BoolExpr] = field(default_factory=dict)
	axiom_cache: dict[int, BoolExpr] = field(default_factory=dict)

	def require_key(self, expr: parser_ast.TraitExpr | None, *, default_module: str | None = None) -> object | None:
		return _trait_expr_key(
			expr,
			default_module=default_module,
			default_package=self.default_package,
			module_packages=self.module_packages,
		)

	def normalized(
		self,
		expr: parser_ast.TraitExpr | None,
		*,
		subst: Mapping[object, object] | None,
		default_module: str | None,
		param_scope_map: Mapping[TypeParamId, tuple[str, int]] | None = None,
	) -> BoolExpr:
		if expr is None:
			return BOOL_TRUE
		subst = subst or {}
		param_scope_map = param_scope_map or {}
		cache_key = (
			_trait_expr_key(
				expr,
				default_module=default_module,
				default_package=self.default_package,
				module_packages=self.module_packages,
			),
			default_module,
			self.default_package,
			_param_scope_key(param_scope_map),
			_subst_key(
				subst,
				default_module=default_module,
				default_package=self.default_package,
				module_packages=self.module_packages,
				param_scope_map=param_scope_map,
			),
		)
		cached = self.normalized_cache.get(cache_key)
		if cached is not None:
			return cached
		formula = self._expr_to_bool(
			expr,
			subst=subst,
			default_module=default_module,
			default_package=self.default_package,
			module_packages=self.module_packages,
			param_scope_map=param_scope_map,
		)
		self.normalized_cache[cache_key] = formula
		return formula

	def implies(self, a_expr: BoolExpr, b_expr: BoolExpr) -> bool:
		expr = _bool_and(a_expr, _bool_not(b_expr))
		atom_set: set[int] = set()
		_collect_atoms(expr, atom_set)
		axioms = self._axioms_for_atoms(atom_set)
		if axioms != BOOL_TRUE:
			expr = _bool_and(expr, axioms)
			_collect_atoms(axioms, atom_set)
		atoms = sorted(atom_set)
		return not _is_satisfiable(expr, atoms)

	def _expr_to_bool(
		self,
		expr: parser_ast.TraitExpr,
		*,
		subst: Mapping[object, object],
		default_module: str | None,
		default_package: str | None,
		module_packages: Mapping[str, str],
		param_scope_map: Mapping[TypeParamId, tuple[str, int]],
	) -> BoolExpr:
		if isinstance(expr, parser_ast.TraitIs):
			trait_key = trait_key_from_expr(
				expr.trait,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			)
			subj_key = _subject_key(
				expr.subject,
				subst,
				default_module,
				default_package,
				module_packages,
				param_scope_map,
			)
			atom_id = self.atom_interner.intern((trait_key, subj_key))
			return _bool_atom(atom_id)
		if isinstance(expr, parser_ast.TraitAnd):
			return _bool_and(
				self._expr_to_bool(
					expr.left,
					subst=subst,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
				self._expr_to_bool(
					expr.right,
					subst=subst,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
			)
		if isinstance(expr, parser_ast.TraitOr):
			return _bool_or(
				self._expr_to_bool(
					expr.left,
					subst=subst,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
				self._expr_to_bool(
					expr.right,
					subst=subst,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
			)
		if isinstance(expr, parser_ast.TraitNot):
			return _bool_not(
				self._expr_to_bool(
					expr.expr,
					subst=subst,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				)
			)
		return BOOL_FALSE

	def _axioms_for_atoms(self, atom_ids: set[int]) -> BoolExpr:
		if not atom_ids:
			return BOOL_TRUE
		combined: BoolExpr = BOOL_TRUE
		seen: set[int] = set()
		queue: list[int] = list(atom_ids)
		while queue:
			atom_id = queue.pop()
			if atom_id in seen:
				continue
			seen.add(atom_id)
			axiom = self._axiom_for_atom(atom_id)
			if axiom is None:
				continue
			if combined == BOOL_TRUE:
				combined = axiom
			else:
				combined = _bool_and(combined, axiom)
			new_atoms: set[int] = set()
			_collect_atoms(axiom, new_atoms)
			for new_atom in new_atoms:
				if new_atom not in seen:
					queue.append(new_atom)
		return combined

	def _axiom_for_atom(self, atom_id: int) -> BoolExpr | None:
		axiom = self.axiom_cache.get(atom_id)
		if axiom is not None:
			return axiom
		trait_key, subject_key = self.atom_interner.lookup(atom_id)
		if not isinstance(trait_key, TraitKey):
			return None
		req_expr = self.trait_requires.get(trait_key)
		if req_expr is None:
			return None
		substituted = _substitute_trait_subject(req_expr, subject_key)
		req_bool = self._expr_to_bool(
			substituted,
			subst={},
			default_module=trait_key.module,
			default_package=trait_key.package_id,
			module_packages=self.module_packages,
			param_scope_map={},
		)
		axiom = _bool_or(_bool_not(_bool_atom(atom_id)), req_bool)
		self.axiom_cache[atom_id] = axiom
		return axiom


def merge_trait_worlds(worlds: Iterable[TraitWorld]) -> TraitWorld:
	merged = TraitWorld()
	world_list = list(worlds)
	for world in world_list:
		merged.diagnostics.extend(getattr(world, "diagnostics", []) or [])
	seen_impls: dict[tuple[object, object, object], int] = {}
	for world in world_list:
		for key, tr in world.traits.items():
			existing = merged.traits.get(key)
			if existing is None:
				merged.traits[key] = tr
				continue
			if _trait_def_key(existing) != _trait_def_key(tr):
				merged.diagnostics.append(
					_link_diag(
						message=f"conflicting trait definition for '{_trait_key_str(key)}'",
						severity="error",
						span=Span.from_loc(getattr(tr, "loc", None)),
					)
				)
	for world in world_list:
		for fn_key, req in world.requires_by_fn.items():
			existing = merged.requires_by_fn.get(fn_key)
			if existing is None:
				merged.requires_by_fn[fn_key] = req
				continue
			if _trait_expr_key(existing) != _trait_expr_key(req):
				merged.diagnostics.append(
					_link_diag(
						message=f"conflicting require clause for '{fn_key}'",
						severity="error",
						span=Span.from_loc(getattr(req, "loc", None)),
					)
				)
		for ty_key, req in world.requires_by_struct.items():
			existing = merged.requires_by_struct.get(ty_key)
			if existing is None:
				merged.requires_by_struct[ty_key] = req
				continue
			if _trait_expr_key(existing) != _trait_expr_key(req):
				merged.diagnostics.append(
					_link_diag(
						message=f"conflicting require clause for '{_type_key_str(ty_key)}'",
						severity="error",
						span=Span.from_loc(getattr(req, "loc", None)),
					)
				)
		for impl in world.impls:
			impl_key = (impl.trait, impl.target, _trait_expr_key(impl.require))
			existing_id = seen_impls.get(impl_key)
			if existing_id is not None:
				existing_impl = merged.impls[existing_id]
				if _impl_method_keys(existing_impl.methods) != _impl_method_keys(impl.methods):
					merged.diagnostics.append(
						_link_diag(
							message=(
								f"conflicting impl for trait '{_trait_key_str(impl.trait)}' "
								f"on '{_type_key_str(impl.target)}'"
							),
							severity="error",
							span=Span.from_loc(getattr(impl, "loc", None)),
						)
					)
				continue
			impl_id = len(merged.impls)
			seen_impls[impl_key] = impl_id
			merged.impls.append(impl)
			merged.impls_by_trait.setdefault(impl.trait, []).append(impl_id)
			merged.impls_by_target_head.setdefault(impl.target_head, []).append(impl_id)
			merged.impls_by_trait_target.setdefault((impl.trait, impl.target_head), []).append(impl_id)
	return merged


def link_trait_worlds(trait_worlds: Mapping[str, TraitWorld]) -> LinkedWorld:
	return LinkedWorld(
		trait_worlds=dict(trait_worlds),
		global_world=merge_trait_worlds(trait_worlds.values()),
	)


def build_require_env(
	linked_world: LinkedWorld,
	*,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> RequireEnv:
	world = linked_world.global_world
	return RequireEnv(
		requires_by_fn=world.requires_by_fn,
		requires_by_struct=world.requires_by_struct,
		trait_requires={key: tr.require for key, tr in world.traits.items() if tr.require is not None},
		default_package=default_package,
		module_packages=module_packages or {},
	)


def _type_expr_key(
	expr: parser_ast.TypeExpr,
	*,
	default_module: str | None = None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> tuple[object, ...]:
	module = expr.module_id if expr.module_id is not None else expr.module_alias
	if module is None and expr.name not in BUILTIN_TYPE_NAMES and expr.name != "fn":
		module = default_module
	package_id = None
	if module is not None:
		package_id = (module_packages or {}).get(module, default_package)
	elif expr.name not in BUILTIN_TYPE_NAMES and expr.name != "fn":
		package_id = default_package
	args = tuple(
		_type_expr_key(
			arg,
			default_module=default_module,
			default_package=default_package,
			module_packages=module_packages,
		)
		for arg in getattr(expr, "args", []) or []
	)
	return (package_id, module, expr.name, args, bool(getattr(expr, "fn_throws", False)))


def _trait_subject_key(
	subject: object,
	*,
	default_module: str | None = None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> tuple[object, object]:
	if isinstance(subject, parser_ast.SelfRef):
		return ("self",)
	if isinstance(subject, parser_ast.TypeNameRef):
		return ("name", subject.name)
	if isinstance(subject, TypeParamId):
		return ("type_param", (subject.owner, subject.index))
	if isinstance(subject, TypeKey):
		return (
			"type_key",
			normalize_type_key(
				subject,
				module_name=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(subject, parser_ast.TypeExpr):
		return (
			"type_expr",
			_type_expr_key(
				subject,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	return ("name", subject)


def _trait_expr_key(
	expr: parser_ast.TraitExpr | None,
	*,
	default_module: str | None = None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> object | None:
	if expr is None:
		return None
	if isinstance(expr, parser_ast.TraitIs):
		return (
			"is",
			_trait_subject_key(
				expr.subject,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
			_type_expr_key(
				expr.trait,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(expr, parser_ast.TraitAnd):
		return (
			"and",
			_trait_expr_key(
				expr.left,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
			_trait_expr_key(
				expr.right,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(expr, parser_ast.TraitOr):
		return (
			"or",
			_trait_expr_key(
				expr.left,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
			_trait_expr_key(
				expr.right,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(expr, parser_ast.TraitNot):
		return (
			"not",
			_trait_expr_key(
				expr.expr,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	return ("unknown", type(expr).__name__)


def _bool_atom(atom_id: int) -> BoolExpr:
	return ("atom", atom_id)


def _bool_not(expr: BoolExpr) -> BoolExpr:
	if expr == BOOL_TRUE:
		return BOOL_FALSE
	if expr == BOOL_FALSE:
		return BOOL_TRUE
	if expr[0] == "not":
		return expr[1]
	return ("not", expr)


def _bool_and(left: BoolExpr, right: BoolExpr) -> BoolExpr:
	if left == BOOL_FALSE or right == BOOL_FALSE:
		return BOOL_FALSE
	if left == BOOL_TRUE:
		return right
	if right == BOOL_TRUE:
		return left
	if left == right:
		return left
	return ("and", left, right)


def _bool_or(left: BoolExpr, right: BoolExpr) -> BoolExpr:
	if left == BOOL_TRUE or right == BOOL_TRUE:
		return BOOL_TRUE
	if left == BOOL_FALSE:
		return right
	if right == BOOL_FALSE:
		return left
	if left == right:
		return left
	return ("or", left, right)


def _collect_atoms(expr: BoolExpr, out: set[int]) -> None:
	tag = expr[0]
	if tag == "atom":
		out.add(expr[1])
		return
	if tag in ("and", "or"):
		_collect_atoms(expr[1], out)
		_collect_atoms(expr[2], out)
		return
	if tag == "not":
		_collect_atoms(expr[1], out)


def _eval_bool(expr: BoolExpr, assignment: dict[int, bool]) -> bool | None:
	tag = expr[0]
	if tag == "true":
		return True
	if tag == "false":
		return False
	if tag == "atom":
		return assignment.get(expr[1])
	if tag == "not":
		val = _eval_bool(expr[1], assignment)
		return None if val is None else (not val)
	if tag == "and":
		left = _eval_bool(expr[1], assignment)
		if left is False:
			return False
		right = _eval_bool(expr[2], assignment)
		if right is False:
			return False
		if left is True and right is True:
			return True
		if left is True:
			return right
		if right is True:
			return left
		return None
	if tag == "or":
		left = _eval_bool(expr[1], assignment)
		if left is True:
			return True
		right = _eval_bool(expr[2], assignment)
		if right is True:
			return True
		if left is False and right is False:
			return False
		if left is False:
			return right
		if right is False:
			return left
		return None
	return None


def _is_satisfiable(expr: BoolExpr, atoms: list[int]) -> bool:
	assignment: dict[int, bool] = {}

	def _search(idx: int) -> bool:
		val = _eval_bool(expr, assignment)
		if val is True:
			return True
		if val is False:
			return False
		while idx < len(atoms) and atoms[idx] in assignment:
			idx += 1
		if idx >= len(atoms):
			return False
		atom = atoms[idx]
		assignment[atom] = True
		if _search(idx + 1):
			return True
		assignment[atom] = False
		if _search(idx + 1):
			return True
		assignment.pop(atom, None)
		return False

	return _search(0)


def _subject_key(
	subject: object,
	subst: Mapping[object, object],
	default_module: str | None,
	default_package: str | None,
	module_packages: Mapping[str, str],
	param_scope_map: Mapping[TypeParamId, tuple[str, int]],
) -> object:
	subj_name: str | None = None
	if isinstance(subject, parser_ast.SelfRef):
		subj_name = "Self"
	elif isinstance(subject, parser_ast.TypeNameRef):
		subj_name = subject.name
	elif isinstance(subject, str):
		subj_name = subject
	if subj_name is not None and subj_name in subst:
		subject = subst[subj_name]
	elif subject in subst:
		subject = subst[subject]
	if isinstance(subject, tuple) and subject and subject[0] == "tyvar":
		return subject
	if isinstance(subject, TypeParamId):
		scope, index = param_scope_map.get(subject, ("unknown", subject.index))
		return ("tyvar", scope, index, subject.owner)
	if isinstance(subject, TypeKey):
		return normalize_type_key(
			subject,
			module_name=default_module,
			default_package=default_package,
			module_packages=module_packages,
		)
	if isinstance(subject, parser_ast.TypeExpr):
		return normalize_type_key(
			type_key_from_expr(
				subject,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
			module_name=default_module,
			default_package=default_package,
			module_packages=module_packages,
		)
	return subject


def _substitute_trait_subject(expr: parser_ast.TraitExpr, subject: object) -> parser_ast.TraitExpr:
	if isinstance(expr, parser_ast.TraitIs):
		subj = expr.subject
		if subj == "Self" or isinstance(subj, parser_ast.SelfRef):
			subj = subject
		return parser_ast.TraitIs(loc=expr.loc, subject=subj, trait=expr.trait)
	if isinstance(expr, parser_ast.TraitAnd):
		return parser_ast.TraitAnd(
			loc=expr.loc,
			left=_substitute_trait_subject(expr.left, subject),
			right=_substitute_trait_subject(expr.right, subject),
		)
	if isinstance(expr, parser_ast.TraitOr):
		return parser_ast.TraitOr(
			loc=expr.loc,
			left=_substitute_trait_subject(expr.left, subject),
			right=_substitute_trait_subject(expr.right, subject),
		)
	if isinstance(expr, parser_ast.TraitNot):
		return parser_ast.TraitNot(
			loc=expr.loc,
			expr=_substitute_trait_subject(expr.expr, subject),
		)
	return expr


def _param_scope_key(param_scope_map: Mapping[TypeParamId, tuple[str, int]]) -> tuple[tuple[object, ...], ...]:
	items: list[tuple[object, ...]] = []
	for key, val in param_scope_map.items():
		items.append((key.owner, key.index, val[0], val[1]))
	return tuple(sorted(items, key=lambda item: (str(item[0]), item[1], str(item[2]), item[3])))


def _subst_key(
	subst: Mapping[object, object],
	*,
	default_module: str | None,
	default_package: str | None,
	module_packages: Mapping[str, str],
	param_scope_map: Mapping[TypeParamId, tuple[str, int]],
) -> tuple[tuple[object, object], ...]:
	items: list[tuple[object, object]] = []
	for key, val in subst.items():
		items.append(
			(
				_subst_item_key(
					key,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
				_subst_item_key(
					val,
					default_module=default_module,
					default_package=default_package,
					module_packages=module_packages,
					param_scope_map=param_scope_map,
				),
			)
		)
	return tuple(sorted(items, key=lambda item: (str(item[0]), str(item[1]))))


def _subst_item_key(
	val: object,
	*,
	default_module: str | None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
	param_scope_map: Mapping[TypeParamId, tuple[str, int]],
) -> object:
	if isinstance(val, TypeParamId):
		scope, index = param_scope_map.get(val, ("unknown", val.index))
		return ("tyvar", scope, index, val.owner)
	if isinstance(val, TypeKey):
		return (
			"type_key",
			normalize_type_key(
				val,
				module_name=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(val, parser_ast.TypeExpr):
		return (
			"type_expr",
			_type_expr_key(
				val,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			),
		)
	if isinstance(val, tuple) and val and val[0] == "tyvar":
		return val
	return ("lit", val)


def _param_type_keys(
	params: Sequence[parser_ast.Param] | None,
	*,
	default_module: str | None = None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> tuple[tuple[object, ...], ...]:
	out: list[tuple[object, ...]] = []
	for param in params or []:
		out.append(
			_type_expr_key(
				param.type_expr,
				default_module=default_module,
				default_package=default_package,
				module_packages=module_packages,
			)
		)
	return tuple(out)


def _trait_method_sig_key(
	method: parser_ast.TraitMethodSig,
	*,
	default_module: str | None = None,
	default_package: str | None = None,
	module_packages: Mapping[str, str] | None = None,
) -> tuple[object, ...]:
	return (
		method.name,
		bool(getattr(method, "declared_nothrow", False)),
		bool(getattr(method, "is_unsafe", False)),
		len(getattr(method, "type_params", []) or []),
		_param_type_keys(
			getattr(method, "params", None),
			default_module=default_module,
			default_package=default_package,
			module_packages=module_packages,
		),
		_type_expr_key(
			method.return_type,
			default_module=default_module,
			default_package=default_package,
			module_packages=module_packages,
		),
	)


def _trait_def_key(trait_def: object) -> tuple[object, ...]:
	methods = getattr(trait_def, "methods", []) or []
	default_module = getattr(getattr(trait_def, "key", None), "module", None)
	default_package = getattr(getattr(trait_def, "key", None), "package_id", None)
	return (
		getattr(trait_def, "name", None),
		_trait_expr_key(
			getattr(trait_def, "require", None),
			default_module=default_module,
			default_package=default_package,
			module_packages=None,
		),
		tuple(
			sorted(
				(
					_trait_method_sig_key(
						m,
						default_module=default_module,
						default_package=default_package,
						module_packages=None,
					)
					for m in methods
				),
				key=lambda item: item,
			)
		),
	)


def _impl_method_key(method: parser_ast.FunctionDef) -> tuple[object, ...]:
	return (
		method.name,
		len(getattr(method, "type_params", []) or []),
		_param_type_keys(getattr(method, "params", None)),
		_type_expr_key(method.return_type),
	)


def _impl_method_keys(methods: Sequence[parser_ast.FunctionDef]) -> tuple[tuple[object, ...], ...]:
	return tuple(sorted((_impl_method_key(m) for m in methods), key=lambda item: item))


def _trait_key_str(key: object) -> str:
	module = getattr(key, "module", None)
	pkg = getattr(key, "package_id", None)
	name = getattr(key, "name", None)
	if name is None:
		return str(key)
	base = f"{module}.{name}" if module else str(name)
	if pkg:
		return f"{pkg}::{base}"
	return base


def _type_key_str(key: object) -> str:
	module = getattr(key, "module", None)
	pkg = getattr(key, "package_id", None)
	name = getattr(key, "name", None)
	if name is None:
		return str(key)
	base = f"{module}.{name}" if module else str(name)
	if pkg:
		base = f"{pkg}::{base}"
	args = getattr(key, "args", None) or ()
	if not args:
		return base
	return f"{base}<{', '.join(_type_key_str(a) for a in args)}>"
