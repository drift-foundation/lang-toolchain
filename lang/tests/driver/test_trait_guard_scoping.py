# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker
from lang.driftc.core.type_resolve_common import resolve_opaque_type


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build_registry(signatures: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		if sig.is_method:
			if sig.impl_target_type_id is None or sig.self_mode is None:
				continue
			self_mode = {
				"value": SelfMode.SELF_BY_VALUE,
				"ref": SelfMode.SELF_BY_REF,
				"ref_mut": SelfMode.SELF_BY_REF_MUT,
			}.get(sig.self_mode)
			if self_mode is None:
				continue
			registry.register_inherent_method(
				callable_id=next_id,
				name=sig.method_name or sig.name,
				module_id=module_id,
				visibility=Visibility.public() if sig.is_pub else Visibility.private(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				impl_id=next_id,
				impl_target_type_id=sig.impl_target_type_id,
				self_mode=self_mode,
				is_generic=bool(sig.type_params or getattr(sig, "impl_type_params", [])),
			)
		else:
			callable_name = fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"
			registry.register_free_function(
				callable_id=next_id,
				name=callable_name,
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	return registry, module_ids


def _visible_modules_for(module_name: str, module_deps: dict[str, set[str]], module_ids: dict[object, int]) -> tuple[int, ...]:
	visible = set(module_deps.get(module_name, set()))
	visible.add(module_name)
	return tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in visible))


def _typecheck_named_fn(
	tmp_path: Path,
	files: dict[Path, str],
	*,
	module_name: str,
	fn_name: str,
) -> object:
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	origin_by_fn_id: dict[FunctionId, Path] = {}
	for mod in modules.values():
		origin_by_fn_id.update(mod.origin_by_fn_id)
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_index = GlobalTraitIndex.from_trait_worlds(getattr(type_table, "trait_worlds", None))
	trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	trait_scope_by_module: dict[str, list[object]] = {}
	for _mod, exp in module_exports.items():
		if isinstance(exp, dict):
			scope = exp.get("trait_scope", [])
			if isinstance(scope, list):
				trait_scope_by_module[_mod] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	fn_key = f"{module_name}::{fn_name}"
	fn_ids = fn_ids_by_name.get(fn_key) or []
	if not fn_ids:
		fn_ids = fn_ids_by_name.get(fn_name) or []
	if not fn_ids:
		qualified = [name for name in fn_ids_by_name.keys() if name.endswith(f"::{fn_name}")]
		if len(qualified) == 1:
			fn_ids = fn_ids_by_name.get(qualified[0]) or []
	if not fn_ids:
		method_ids = [fid for fid, sig in signatures.items() if getattr(sig, "method_name", None) == fn_name]
		if len(method_ids) == 1:
			fn_ids = method_ids
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	fn_block = func_hirs[fn_id]
	fn_sig = signatures.get(fn_id)
	param_types = {}
	if fn_sig and fn_sig.param_names:
		if fn_sig.param_type_ids:
			param_types = {pname: pty for pname, pty in zip(fn_sig.param_names, fn_sig.param_type_ids)}
		elif fn_sig.param_types:
			local_type_params = {p.name: p.id for p in getattr(fn_sig, "type_params", []) or []}
			resolved = [
				resolve_opaque_type(p, type_table, module_id=fn_sig.module, type_params=local_type_params)
				for p in fn_sig.param_types
			]
			param_types = {pname: pty for pname, pty in zip(fn_sig.param_names, resolved)}
	visible_mods = _visible_modules_for(module_name, module_deps, module_ids)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		fn_id,
		fn_block,
		param_types=param_types,
		return_type=fn_sig.return_type_id if fn_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=module_ids.setdefault(module_name, len(module_ids)),
	)
	return result


def test_trait_guard_conjunction_adds_scope(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show, Debug };

pub trait Show { fn show(self: Self) -> Int }
pub trait Debug { fn debug(self: Self) -> Int }

implement Show for Int { pub fn show(self: Int) -> Int { return 1; } }
implement Debug for Int { pub fn debug(self: Int) -> Int { return 2; } }
""",
		Path("main.drift"): """
module main;

import m_trait;
use trait m_trait.Show;
use trait m_trait.Debug;

fn f<T>(x: T) -> Int {
	if (T is m_trait.Show and T is m_trait.Debug) {
		return x.show() + x.debug();
	}
	return 0;
}
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="f")
	assert result.diagnostics == []
	then_diags = [
		d.message
		for (guard_key, branch), diags in (result.deferred_guard_diags or {}).items()
		if branch == "then"
		for d in diags
	]
	assert not then_diags


def test_trait_guard_or_does_not_add_scope(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show, Debug };

pub trait Show { fn show(self: Self) -> Int }
pub trait Debug { fn debug(self: Self) -> Int }

implement Show for Int { pub fn show(self: Int) -> Int { return 1; } }
""",
		Path("main.drift"): """
module main;

import m_trait;
use trait m_trait.Show;
use trait m_trait.Debug;

fn f<T>(x: T) -> Int {
	if (T is m_trait.Show or T is m_trait.Debug) {
		return x.show();
	}
	return 0;
}
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="f")
	then_diags = [
		d.message
		for (guard_key, branch), diags in (result.deferred_guard_diags or {}).items()
		if branch == "then"
		for d in diags
	]
	assert any("requirement not satisfied" in msg for msg in then_diags)


def test_trait_guard_not_does_not_add_scope(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show };

pub trait Show { fn show(self: Self) -> Int }

implement Show for Int { pub fn show(self: Int) -> Int { return 1; } }
""",
		Path("main.drift"): """
module main;

import m_trait;
use trait m_trait.Show;

fn f<T>(x: T) -> Int {
	if not (T is m_trait.Show) {
		return x.show();
	}
	return 0;
}
""",
	}
	result = _typecheck_named_fn(tmp_path, files, module_name="main", fn_name="f")
	then_diags = [
		d.message
		for (guard_key, branch), diags in (result.deferred_guard_diags or {}).items()
		if branch == "then"
		for d in diags
	]
	assert any("requirement not satisfied" in msg for msg in then_diags)
