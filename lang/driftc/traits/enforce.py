# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from lang.driftc.core.diagnostics import Diagnostic
from lang.driftc.core.span import Span
from lang.driftc import stage1 as H
from lang.driftc.parser import ast as parser_ast
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.traits.solver import Env, ProofStatus, prove_expr
from lang.driftc.core.function_id import FunctionId, function_symbol
from lang.driftc.core.types_core import TypeKind, TypeParamId
from lang.driftc.traits.linked_world import LinkedWorld, RequireEnv
from lang.driftc.traits.world import TypeKey, TraitKey, normalize_type_key, trait_key_from_expr, type_key_from_typeid
from lang.driftc.core.type_resolve_common import resolve_opaque_type


# Trait enforcement diagnostics are typecheck-phase.
def _enforce_diag(*args, **kwargs):
	if "phase" not in kwargs or kwargs.get("phase") is None:
		kwargs["phase"] = "typecheck"
	return Diagnostic(*args, **kwargs)


@dataclass
class TraitEnforceResult:
	diagnostics: List[Diagnostic]


def _collect_exprs(expr: H.HExpr, out: List[H.HExpr]) -> None:
	out.append(expr)
	for field in getattr(expr, "__dataclass_fields__", {}) or {}:
		val = getattr(expr, field, None)
		if isinstance(val, H.HExpr):
			_collect_exprs(val, out)
		elif isinstance(val, list):
			for item in val:
				if isinstance(item, H.HExpr):
					_collect_exprs(item, out)


def _walk_block(block: H.HBlock, out: List[H.HExpr]) -> None:
	for stmt in block.statements:
		if isinstance(stmt, H.HExprStmt):
			_collect_exprs(stmt.expr, out)
		elif isinstance(stmt, H.HLet):
			_collect_exprs(stmt.value, out)
		elif isinstance(stmt, H.HAssign):
			_collect_exprs(stmt.value, out)
		elif isinstance(stmt, H.HAugAssign):
			_collect_exprs(stmt.value, out)
		elif isinstance(stmt, H.HIf):
			if isinstance(stmt.cond, H.HExpr):
				_collect_exprs(stmt.cond, out)
			_walk_block(stmt.then_block, out)
			if stmt.else_block:
				_walk_block(stmt.else_block, out)
		elif isinstance(stmt, H.HLoop):
			_walk_block(stmt.body, out)
		elif isinstance(stmt, H.HTry):
			_walk_block(stmt.body, out)
			for arm in stmt.catches:
				_walk_block(arm.block, out)
		elif isinstance(stmt, H.HReturn) and stmt.value is not None:
			_collect_exprs(stmt.value, out)


def collect_used_type_keys(
	typed_fns: Dict[FunctionId, object],
	type_table: object,
	signatures: Dict[FunctionId, object],
) -> Set[TypeKey]:
	used: Set[TypeKey] = set()
	for sig in signatures.values():
		for tid in getattr(sig, "param_type_ids", []) or []:
			if tid is None:
				continue
			used.add(type_key_from_typeid(type_table, tid))
		ret = getattr(sig, "return_type_id", None)
		if ret is not None:
			used.add(type_key_from_typeid(type_table, ret))
	for typed_fn in typed_fns.values():
		for tid in getattr(typed_fn, "binding_types", {}).values():
			if tid is None:
				continue
			used.add(type_key_from_typeid(type_table, tid))
		for tid in getattr(typed_fn, "expr_types", {}).values():
			if tid is None:
				continue
			used.add(type_key_from_typeid(type_table, tid))
	return used


def _subject_key(subject: object) -> object:
	if isinstance(subject, parser_ast.SelfRef):
		return "Self"
	if isinstance(subject, parser_ast.TypeNameRef):
		return subject.name
	return subject


def _collect_trait_subjects(expr: parser_ast.TraitExpr, out: Set[object]) -> None:
	if isinstance(expr, parser_ast.TraitIs):
		out.add(_subject_key(expr.subject))
	elif isinstance(expr, (parser_ast.TraitAnd, parser_ast.TraitOr)):
		_collect_trait_subjects(expr.left, out)
		_collect_trait_subjects(expr.right, out)
	elif isinstance(expr, parser_ast.TraitNot):
		_collect_trait_subjects(expr.expr, out)


def _infer_lambda_fn_type(
	lam: H.HLambda,
	*,
	expr_types: dict[int, object],
	type_table: object,
	module_name: str | None,
) -> object | None:
	if getattr(lam, "explicit_captures", None):
		return None
	captures = getattr(lam, "captures", []) or []
	if captures:
		return None
	param_types: list[object] = []
	for p in getattr(lam, "params", []) or []:
		p_ty = None
		if getattr(p, "type", None) is not None:
			try:
				p_ty = resolve_opaque_type(p.type, type_table, module_id=module_name)
			except Exception:
				p_ty = None
		param_types.append(p_ty if p_ty is not None else type_table.ensure_unknown())
	ret_ty = None
	if getattr(lam, "ret_type", None) is not None:
		try:
			ret_ty = resolve_opaque_type(lam.ret_type, type_table, module_id=module_name)
		except Exception:
			ret_ty = None
	if ret_ty is None and lam.body_expr is not None:
		ret_ty = expr_types.get(lam.body_expr.node_id)
	if ret_ty is None and lam.body_block is not None:
		for st in lam.body_block.statements:
			if isinstance(st, H.HReturn) and st.value is not None:
				ret_ty = expr_types.get(st.value.node_id)
				if ret_ty is not None:
					break
	if ret_ty is None:
		ret_ty = type_table.ensure_void()
	can_throw = getattr(lam, "can_throw_effective", None)
	if can_throw is None:
		can_throw = True
	return type_table.ensure_function(list(param_types), ret_ty, can_throw=bool(can_throw))


def _extract_conjunctive_facts(expr: parser_ast.TraitExpr) -> List[parser_ast.TraitIs]:
	if isinstance(expr, parser_ast.TraitIs):
		return [expr]
	if isinstance(expr, parser_ast.TraitAnd):
		return _extract_conjunctive_facts(expr.left) + _extract_conjunctive_facts(expr.right)
	return []


def _trait_label(key: TraitKey) -> str:
	base = f"{key.module}.{key.name}" if key.module else key.name
	if key.package_id and not base.startswith(f"{key.package_id}."):
		return f"{key.package_id}::{base}"
	return base


def _trait_expr_label(expr: parser_ast.TraitExpr, require_env: RequireEnv, *, default_module: str | None) -> str:
	if isinstance(expr, parser_ast.TraitIs):
		trait_key = trait_key_from_expr(
			expr.trait,
			default_module=default_module,
			default_package=require_env.default_package,
			module_packages=require_env.module_packages,
		)
		subj = _subject_key(expr.subject)
		return f"{subj} is {_trait_label(trait_key)}"
	if isinstance(expr, parser_ast.TraitAnd):
		return f"{_trait_expr_label(expr.left, require_env, default_module=default_module)} and {_trait_expr_label(expr.right, require_env, default_module=default_module)}"
	if isinstance(expr, parser_ast.TraitOr):
		return f"{_trait_expr_label(expr.left, require_env, default_module=default_module)} or {_trait_expr_label(expr.right, require_env, default_module=default_module)}"
	if isinstance(expr, parser_ast.TraitNot):
		return f"not ({_trait_expr_label(expr.expr, require_env, default_module=default_module)})"
	return "<unknown>"


def enforce_struct_requires(
	linked_world: LinkedWorld,
	require_env: RequireEnv,
	used_types: Iterable[TypeKey],
	*,
	module_name: str,
	visible_modules: Iterable[str] | None = None,
) -> TraitEnforceResult:
	diags: List[Diagnostic] = []
	env = Env(
		default_module=module_name,
		default_package=require_env.default_package,
		module_packages=require_env.module_packages,
	)
	base_visible = set(visible_modules) if visible_modules is not None else None
	world = linked_world.global_world if base_visible is None else linked_world.visible_world(base_visible)
	for ty in used_types:
		ty_norm = normalize_type_key(
			ty,
			module_name=module_name,
			default_package=require_env.default_package,
			module_packages=require_env.module_packages,
		)
		req = require_env.requires_by_struct.get(ty_norm)
		if req is None:
			continue
		subst = {"Self": ty_norm}
		res = prove_expr(world, env, subst, req)
		if res.status is not ProofStatus.PROVED:
			diags.append(
				_enforce_diag(
					message=f"trait requirements not met for struct '{ty.name}'",
					code="E_REQUIREMENT_NOT_SATISFIED",
					severity="error",
					span=Span.from_loc(getattr(req, "loc", None)),
					notes=[f"requirement_expr={_trait_expr_label(req, require_env, default_module=module_name)}"],
				)
			)
	return TraitEnforceResult(diags)


def enforce_fn_requires(
	linked_world: LinkedWorld,
	require_env: RequireEnv,
	typed_fn: object,
	type_table: object,
	*,
	module_name: str,
	signatures: Dict[FunctionId, object],
	visible_modules: Iterable[str] | None = None,
) -> TraitEnforceResult:
	diags: List[Diagnostic] = []
	exprs: List[H.HExpr] = []
	_walk_block(getattr(typed_fn, "body"), exprs)
	expr_types = getattr(typed_fn, "expr_types", {})
	call_resolutions = getattr(typed_fn, "call_resolutions", {}) or {}
	instantiations_by_callsite_id = getattr(typed_fn, "instantiations_by_callsite_id", {}) or {}
	caller_sig = signatures.get(getattr(typed_fn, "fn_id", None))
	caller_type_params: Dict[str, TypeParamId] = {}
	if caller_sig is not None:
		for tp in getattr(caller_sig, "type_params", []) or []:
			if isinstance(getattr(tp, "name", None), str):
				caller_type_params[tp.name] = tp.id
	caller_req = require_env.requires_by_fn.get(getattr(typed_fn, "fn_id", None))
	assumed_true: Set[Tuple[object, TraitKey]] = set()
	if caller_req is not None:
		name_by_id = {tp_id: name for name, tp_id in caller_type_params.items()}
		for atom in _extract_conjunctive_facts(caller_req):
			subj = _subject_key(atom.subject)
			trait_key = trait_key_from_expr(
				atom.trait,
				default_module=module_name,
				default_package=require_env.default_package,
				module_packages=require_env.module_packages,
			)
			assumed_true.add((subj, trait_key))
			if isinstance(subj, TypeParamId):
				tp_name = name_by_id.get(subj)
				ty_id = type_table.ensure_typevar(subj, name=tp_name)
				key = normalize_type_key(
					type_key_from_typeid(type_table, ty_id),
					module_name=module_name,
					default_package=require_env.default_package,
					module_packages=require_env.module_packages,
				)
				assumed_true.add((key, trait_key))
	seen: Set[Tuple[FunctionId, Tuple[TypeKey, ...], Tuple[TypeKey, ...]]] = set()
	symbol_to_id = {function_symbol(fid): fid for fid in signatures.keys()}
	base_visible = set(visible_modules) if visible_modules is not None else None
	world = linked_world.global_world if base_visible is None else linked_world.visible_world(base_visible)

	def _infer_type_args_from_call(sig: object, arg_type_ids: List[object]) -> Dict[TypeParamId, object] | None:
		type_params = list(getattr(sig, "type_params", []) or [])
		if not type_params:
			return {}
		param_types = list(getattr(sig, "param_type_ids", []) or [])
		if not param_types or len(param_types) != len(arg_type_ids):
			return None
		owner = type_params[0].id.owner
		bindings: Dict[TypeParamId, object] = {}

		def _bind(tp_id: TypeParamId, actual: object) -> bool:
			if tp_id.owner != owner:
				return False
			cur = bindings.get(tp_id)
			if cur is None:
				bindings[tp_id] = actual
				return True
			return cur == actual

		def _unify(param: object, actual: object) -> bool:
			if param == actual:
				return True
			pdef = type_table.get(param)
			if pdef.kind is TypeKind.TYPEVAR and pdef.type_param_id is not None:
				return _bind(pdef.type_param_id, actual)
			adef = type_table.get(actual)
			if pdef.kind is not adef.kind:
				return False
			if pdef.kind is TypeKind.REF:
				if pdef.ref_mut != adef.ref_mut or not pdef.param_types or not adef.param_types:
					return False
				return _unify(pdef.param_types[0], adef.param_types[0])
			if pdef.kind in (TypeKind.ARRAY, TypeKind.FNRESULT, TypeKind.FUNCTION):
				if len(pdef.param_types) != len(adef.param_types):
					return False
				return all(_unify(p, a) for p, a in zip(pdef.param_types, adef.param_types))
			if pdef.kind is TypeKind.STRUCT:
				pinst = type_table.get_struct_instance(param)
				ainst = type_table.get_struct_instance(actual)
				if pinst is None and ainst is None:
					return param == actual
				if pinst is None or ainst is None or pinst.base_id != ainst.base_id:
					return False
				if len(pinst.type_args) != len(ainst.type_args):
					return False
				return all(_unify(p, a) for p, a in zip(pinst.type_args, ainst.type_args))
			if pdef.kind is TypeKind.VARIANT:
				pinst = type_table.get_variant_instance(param)
				ainst = type_table.get_variant_instance(actual)
				if pinst is None and ainst is None:
					return param == actual
				if pinst is None or ainst is None or pinst.base_id != ainst.base_id:
					return False
				if len(pinst.type_args) != len(ainst.type_args):
					return False
				return all(_unify(p, a) for p, a in zip(pinst.type_args, ainst.type_args))
			return False

		for p, a in zip(param_types, arg_type_ids):
			if not _unify(p, a):
				return None

		for tp in type_params:
			if tp.id not in bindings:
				return None
		return bindings

	for expr in exprs:
		if not isinstance(expr, H.HCall) or not isinstance(expr.fn, H.HVar):
			continue
		decl_name = expr.fn.name
		resolution = call_resolutions.get(expr.node_id)
		if isinstance(resolution, MethodResolution):
			# Trait requires are currently declared on free functions only.
			continue
		fn_id = getattr(resolution, "fn_id", None)
		if fn_id is None:
			fn_id = symbol_to_id.get(decl_name)
		if fn_id is None:
			continue
		callee_mod = getattr(fn_id, "module", None) or module_name
		req = require_env.requires_by_fn.get(fn_id)
		if req is None:
			continue
		if base_visible is not None:
			trait_mods: Set[str] = set()
			for atom in _extract_conjunctive_facts(req):
				trait_key = trait_key_from_expr(
					atom.trait,
					default_module=module_name,
					default_package=require_env.default_package,
					module_packages=require_env.module_packages,
				)
				if trait_key.module:
					trait_mods.add(trait_key.module)
			if trait_mods:
				world = linked_world.visible_world(base_visible | trait_mods)
		env = Env(
			assumed_true=set(assumed_true),
			default_module=callee_mod,
			default_package=require_env.default_package,
			module_packages=require_env.module_packages,
			type_table=type_table,
		)
		sig = signatures.get(fn_id)
		subst: Dict[object, TypeKey] = {}
		subjects: Set[object] = set()
		_collect_trait_subjects(req, subjects)
		arg_keys: List[TypeKey] = []
		type_arg_keys: List[TypeKey] = []
		arg_type_ids: List[object] = []
		for arg in expr.args:
			tid = expr_types.get(arg.node_id)
			if tid is None and isinstance(arg, H.HLambda):
				tid = _infer_lambda_fn_type(arg, expr_types=expr_types, type_table=type_table, module_name=module_name)
			if tid is None:
				continue
			arg_type_ids.append(tid)
			arg_keys.append(
				normalize_type_key(
					type_key_from_typeid(type_table, tid),
					module_name=module_name,
					default_package=require_env.default_package,
					module_packages=require_env.module_packages,
				)
			)
		if sig and getattr(sig, "type_params", None):
			type_params = list(getattr(sig, "type_params", []) or [])
			type_param_names = {
				tp.id: tp.name
				for tp in type_params
				if getattr(tp, "id", None) is not None and isinstance(getattr(tp, "name", None), str)
			}
			type_args = getattr(expr, "type_args", None) or []
			bindings: Dict[TypeParamId, object] | None = None
			callsite_id = getattr(expr, "callsite_id", None)
			inst = instantiations_by_callsite_id.get(callsite_id) if callsite_id is not None else None
			if inst is not None and len(inst.type_args) == len(type_params):
				bindings = {tp.id: inst.type_args[idx] for idx, tp in enumerate(type_params)}
			elif type_args and len(type_args) == len(type_params):
				bindings = {}
				for idx, tp in enumerate(type_params):
					te = type_args[idx]
					name = getattr(te, "name", None)
					args = getattr(te, "args", None)
					if isinstance(name, str) and name in caller_type_params and not args:
						ty_id = type_table.ensure_typevar(caller_type_params[name], name=name)
					else:
						try:
							ty_id = resolve_opaque_type(te, type_table, module_id=module_name)
						except Exception:
							bindings = None
							break
					bindings[tp.id] = ty_id
			else:
				bindings = _infer_type_args_from_call(sig, arg_type_ids)
			if bindings is None and req is not None and getattr(sig, "type_params", None) and arg_type_ids:
				type_params = list(getattr(sig, "type_params", []) or [])
				type_param_ids = {tp.id for tp in type_params}
				name_to_id = {tp.name: tp.id for tp in type_params if getattr(tp, "name", None)}
				bindings = {}
				if sig.param_type_ids and len(sig.param_type_ids) == len(arg_type_ids):
					for idx, pty in enumerate(sig.param_type_ids):
						pdef = type_table.get(pty)
						if pdef.kind is TypeKind.TYPEVAR and pdef.type_param_id in type_param_ids:
							if pdef.type_param_id not in bindings:
								bindings[pdef.type_param_id] = arg_type_ids[idx]
				for atom in _extract_conjunctive_facts(req):
					if not isinstance(atom, parser_ast.TraitIs):
						continue
					trait_name = getattr(atom.trait, "name", None)
					if trait_name not in ("Fn0", "Fn1", "Fn2"):
						continue
					subj_id = None
					if isinstance(atom.subject, TypeParamId):
						subj_id = atom.subject
					elif isinstance(atom.subject, parser_ast.TypeNameRef):
						subj_id = name_to_id.get(atom.subject.name)
					if subj_id is None:
						continue
					subj_ty = bindings.get(subj_id)
					if subj_ty is None:
						continue
					subj_def = type_table.get(subj_ty)
					if subj_def.kind is not TypeKind.FUNCTION or not subj_def.param_types:
						continue
					fn_parts = list(subj_def.param_types)
					trait_args = list(getattr(atom.trait, "args", []) or [])
					if len(trait_args) != len(fn_parts):
						continue
					for targ, farg in zip(trait_args, fn_parts):
						tp_id = None
						if isinstance(targ, parser_ast.TypeExpr):
							if isinstance(getattr(targ, "name", None), str) and not getattr(targ, "args", None):
								tp_id = name_to_id.get(targ.name)
						elif isinstance(targ, parser_ast.TypeNameRef):
							tp_id = name_to_id.get(targ.name)
						if tp_id is not None and tp_id not in bindings:
							bindings[tp_id] = farg
				if not bindings or any(tp.id not in bindings for tp in type_params):
					bindings = None
			if bindings:
				for tp_id, ty_id in bindings.items():
					tp_name = type_param_names.get(tp_id)
					if tp_id in subjects or tp_name in subjects:
						key = normalize_type_key(
							type_key_from_typeid(type_table, ty_id),
							module_name=module_name,
							default_package=require_env.default_package,
							module_packages=require_env.module_packages,
						)
						subst[tp_id] = key
						if tp_name:
							subst[tp_name] = key
				for tp in type_params:
					ty_id = bindings.get(tp.id)
					if ty_id is None:
						continue
					type_arg_keys.append(
						normalize_type_key(
							type_key_from_typeid(type_table, ty_id),
							module_name=module_name,
							default_package=require_env.default_package,
							module_packages=require_env.module_packages,
						)
					)
			if getattr(sig, "param_names", None):
				for idx, pname in enumerate(sig.param_names or []):
					if pname in subjects and idx < len(arg_keys):
						subst[pname] = arg_keys[idx]
		seen_key = (fn_id, tuple(arg_keys), tuple(type_arg_keys))
		if seen_key in seen:
			continue
		seen.add(seen_key)
		res = prove_expr(world, env, subst, req)
		if res.status is not ProofStatus.PROVED:
			diags.append(
				_enforce_diag(
					message=f"trait requirements not met for call to '{expr.fn.name}'",
					code="E_REQUIREMENT_NOT_SATISFIED",
					severity="error",
					span=getattr(expr, "loc", Span()),
					notes=[f"requirement_expr={_trait_expr_label(req, require_env, default_module=module_name)}"],
				)
			)
	return TraitEnforceResult(diags)
