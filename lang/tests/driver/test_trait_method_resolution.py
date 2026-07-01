# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.impl_index import GlobalImplIndex, ImplMeta
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.type_checker import TypeChecker
from lang.driftc.traits.world import TraitKey


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name


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
			registry.register_free_function(
				callable_id=next_id,
				name=_callable_name(fn_id),
				module_id=module_id,
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	return registry, module_ids


def _visible_modules_for(
	module_name: str, module_deps: dict[str, set[str]], module_ids: dict[object, int]
) -> tuple[int, ...]:
	visible = set(module_deps.get(module_name, set()))
	visible.add(module_name)
	return tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in visible))


def _collect_method_calls(block: H.HBlock) -> list[H.HMethodCall]:
	calls: list[H.HMethodCall] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HMethodCall):
			calls.append(expr)
			walk_expr(expr.receiver)
			for a in expr.args:
				walk_expr(a)
			for kw in getattr(expr, "kwargs", []) or []:
				if getattr(kw, "value", None) is not None:
					walk_expr(kw.value)
			return
		for child in getattr(expr, "__dict__", {}).values():
			if isinstance(child, H.HExpr):
				walk_expr(child)
			elif isinstance(child, H.HBlock):
				walk_block(child)
			elif isinstance(child, list):
				for it in child:
					if isinstance(it, H.HExpr):
						walk_expr(it)
					elif isinstance(it, H.HBlock):
						walk_block(it)

	def walk_block(b: H.HBlock) -> None:
		for st in b.statements:
			if isinstance(st, H.HExprStmt):
				walk_expr(st.expr)
			elif isinstance(st, H.HReturn) and st.value is not None:
				walk_expr(st.value)
			else:
				for child in getattr(st, "__dict__", {}).values():
					if isinstance(child, H.HExpr):
						walk_expr(child)
					elif isinstance(child, H.HBlock):
						walk_block(child)
					elif isinstance(child, list):
						for it in child:
							if isinstance(it, H.HExpr):
								walk_expr(it)
							elif isinstance(it, H.HBlock):
								walk_block(it)

	walk_block(block)
	return calls


def _resolve_main_block(
	tmp_path: Path, files: dict[Path, str], *, main_module: str
) -> tuple[H.HBlock, object, dict[FunctionId, object], dict[str, set[str]], dict[object, int], object]:
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	origin_by_fn_id: dict[FunctionId, Path] = {}
	for mod in modules.values():
		origin_by_fn_id.update(mod.origin_by_fn_id)
	assert diagnostics == []
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
	main_key = f"{main_module}::main"
	main_ids = fn_ids_by_name.get(main_key) or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	visible_modules = _visible_modules_for(main_module, module_deps, module_ids)
	type_checker = TypeChecker(type_table=type_table)
	result = type_checker.check_function(
		main_id,
		main_block,
		callable_registry=registry,
		signatures_by_id=signatures,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_modules,
		current_module=module_ids.setdefault(main_module, len(module_ids)),
	)
	return (
		main_block,
		result,
		signatures,
		module_deps,
		module_ids,
		type_table,
	)


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
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	assert diagnostics == []
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
	key = f"{module_name}::{fn_name}"
	fn_ids = fn_ids_by_name.get(key) or []
	assert len(fn_ids) == 1
	fn_id = fn_ids[0]
	block = func_hirs[fn_id]
	sig = signatures.get(fn_id)
	param_types = {}
	if sig and sig.param_names and sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(sig.param_names, sig.param_type_ids)}
	current_mod = module_ids.setdefault(module_name, len(module_ids))
	visible_modules = _visible_modules_for(module_name, module_deps, module_ids)
	type_checker = TypeChecker(type_table=type_table)
	return type_checker.check_function(
		fn_id,
		block,
		param_types=param_types,
		return_type=sig.return_type_id if sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_modules,
		current_module=current_mod,
	)


def test_trait_dot_call_succeeds_with_use_trait(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

pub trait Show {
	fn show(self: Self) -> Int
}

export { Box, Show };

implement Show for Box<Int> {
	pub fn show(self: Box<Int>) -> Int { return self.value; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box as box;
use trait box.Show;

pub fn main() nothrow -> Int{
	val b: box.Box<Int> = box.Box<type Int>(1);
	return b.show();
}
""",
	}
	main_block, result, sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert calls
	res = result.typed_fn.call_resolutions[calls[0].node_id]
	assert res.decl.fn_id in sigs
	assert res.decl.fn_id.module == "m_box"


def test_trait_dot_call_requires_use_trait_same_module(tmp_path: Path) -> None:
	files = {
		Path("m_main.drift"): """
module m_main;

struct S(x: Int);

trait Show {
	fn show(self: S) -> Int
}

implement Show for S {
	fn show(self: S) -> Int { return self.x; }
}

pub fn main() nothrow -> Int{
	val s = S(1);
	return s.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("no matching method 'show'" in m for m in msgs)


def test_trait_dot_call_with_use_trait_same_module(tmp_path: Path) -> None:
	files = {
		Path("m_main.drift"): """
module m_main;

struct S(x: Int);

trait Show {
	fn show(self: S) -> Int
}

implement Show for S {
	fn show(self: S) -> Int { return self.x; }
}

use trait Show;

pub fn main() nothrow -> Int{
	val s = S(1);
	return s.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []


def test_use_trait_external_module_path_without_import_alias(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return self.value; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box as box;
import m_trait as t;
use trait m_trait.Show;

pub fn main() nothrow -> Int{
	val b: box.Box<Int> = box.Box<type Int>(1);
	return b.show();
}
""",
	}
	main_block, result, sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert calls
	res = result.typed_fn.call_resolutions[calls[0].node_id]
	assert res.decl.fn_id in sigs
	assert res.decl.fn_id.module == "m_trait"


def test_use_trait_unknown_trait_is_error(tmp_path: Path) -> None:
	files = {
		Path("m_main.drift"): """
module m_main;

use trait NotATrait;

pub fn main() nothrow -> Int{
	return 0;
}
	""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_mods, _tt, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("does not define trait 'NotATrait'" in m for m in msgs)


def test_use_trait_unknown_module_path_is_error(tmp_path: Path) -> None:
	files = {
		Path("m_main.drift"): """
module m_main;

use trait missing.mod.Show;

pub fn main() nothrow -> Int{
	return 0;
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_mods, _tt, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("unknown module" in m for m in msgs)


def test_trait_method_infers_method_type_params(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

pub trait MapOne {
	fn map<U>(self: &Self, x: U) -> U
}

export { Box, MapOne };

implement<T> MapOne for Box<T> {
pub fn map<U>(self: &Box<T>, var x: U) -> U { return move x; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box as box;
use trait box.MapOne;

pub fn main() nothrow -> Int{
	val b: box.Box<Int> = box.Box<type Int>(1);
	val a: Int = b.map(1);
	val s: String = b.map("x");
	return a;
}
""",
	}
	main_block, result, _sigs, _deps, _ids, type_table = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert len(calls) == 2
	impl_ids = [
		fn_id
		for fn_id, sig in _sigs.items()
		if getattr(sig, "is_method", False)
		and (sig.method_name or sig.name) == "map"
		and getattr(sig, "module", None) == "m_box"
		and "MapOne" in sig.name
	]
	assert len(impl_ids) == 1
	expected_fn_id = impl_ids[0]
	int_ty = type_table.ensure_int()
	str_ty = type_table.ensure_string()
	result_types = [result.typed_fn.expr_types.get(call.node_id) for call in calls]
	assert int_ty in result_types
	assert str_ty in result_types
	for call in calls:
		res = result.typed_fn.call_resolutions.get(call.node_id)
		assert res is not None
		assert res.decl.fn_id == expected_fn_id


def test_trait_not_in_scope_is_not_found(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

pub trait Show {
	fn show(self: Box<Int>) -> Int
}

export { Box, Show };

implement Show for Box<Int> {
	pub fn show(self: Box<Int>) -> Int { return self.value; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box as box;

	pub fn main() nothrow -> Int{
		val b: box.Box<Int> = box.Box<type Int>(1);
		return b.show();
	}
	""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("no matching method 'show'" in m for m in msgs)


def test_inherent_beats_trait_method(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };

implement Box<Int> {
	pub fn tag(self: Box<Int>) -> Int { return 1; }
}
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Tag {
	fn tag(self: m_box.Box<Int>) -> Int
}

export { Tag };

implement Tag for m_box.Box<Int> {
	pub fn tag(self: m_box.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
use trait m_trait.Tag;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.tag();
}
""",
	}
	main_block, result, sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	res = result.typed_fn.call_resolutions[calls[0].node_id]
	assert res.decl.fn_id in sigs
	assert res.decl.fn_id.module == "m_box"


def test_trait_ambiguity_reports_modules(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_a.drift"): """
module m_a;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_b.drift"): """
module m_b;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_a;
import m_b;
use trait m_a.Show;
use trait m_b.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("ambiguous method 'show'" in m for m in msgs)


def test_trait_require_blocks_candidate(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Hashable {
	fn hash(self: Int) -> Int
}

export { Hashable, Show };

implement Hashable for Int {
	pub fn hash(self: Int) -> Int { return self; }
}

pub trait Show {
	fn show(self: m_box.Box<String>) -> Int
}

implement Show for m_box.Box<String> require String is Hashable {
	pub fn show(self: m_box.Box<String>) -> Int { return 0; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
use trait m_trait.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<String> = m_box.Box<type String>("s");
	return b.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("requirement not satisfied" in m for m in msgs)
	assert any("Hashable" in m for m in msgs)


def test_trait_private_impl_not_visible_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };
""",
		Path("m_impl.drift"): """
module m_impl;

import m_box;
import m_trait;

implement m_trait.Show for m_box.Box<Int> {
	fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
import m_impl;
use trait m_trait.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("exists but is not visible" in m for m in msgs)
	assert any("m_trait.Show@m_impl" in m for m in msgs)


def test_trait_same_trait_multiple_impls_ambiguous(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };
""",
		Path("m_a.drift"): """
module m_a;

import m_box;
import m_trait;

implement m_trait.Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_b.drift"): """
module m_b;

import m_box;
import m_trait;

implement m_trait.Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
import m_a;
import m_b;
use trait m_trait.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("ambiguous method 'show'" in m for m in msgs)


def test_trait_bound_does_not_expand_scope(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show };

pub trait Show {
	fn show(self: Self) -> Int
}

implement Show for Int {
	pub fn show(self: Int) -> Int { return self; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_trait;

fn f<T>(x: T) -> Int require T is m_trait.Show { return x.show(); }

pub fn main() nothrow -> Int{ return f<type Int>(1); }
""",
	}
	result = _typecheck_named_fn(
		tmp_path, files, module_name="m_main", fn_name="f"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("no matching method 'show'" in m for m in msgs)


def test_trait_bound_with_use_trait_succeeds(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show };

pub trait Show {
	fn show(self: Self) -> Int
}

implement Show for Int {
	pub fn show(self: Int) -> Int { return self; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_trait;
use trait m_trait.Show;

fn f<T>(x: T) -> Int require T is m_trait.Show { return x.show(); }

pub fn main() nothrow -> Int{ return f<type Int>(1); }
""",
	}
	result = _typecheck_named_fn(
		tmp_path, files, module_name="m_main", fn_name="f"
	)
	assert result.diagnostics == []


def test_trait_bound_enforced_at_call_site(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

export { Show };

pub trait Show {
	fn show(self: Self) -> Int
}

implement Show for Int {
	pub fn show(self: Int) -> Int { return self; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_trait;
use trait m_trait.Show;

fn f<T>(x: T) -> Int require T is m_trait.Show { return x.show(); }

pub fn main() nothrow -> Int{ return f<type String>("s"); }
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("requirement not satisfied" in m for m in msgs)
	assert any("m_trait.Show" in m for m in msgs)


def test_ufcs_call_without_use_trait(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return self.value; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return m_trait.Show::show(b);
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []


def test_ufcs_disambiguates_traits(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_a.drift"): """
module m_a;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_b.drift"): """
module m_b;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };

implement Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_a;
import m_b;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return m_a.Show::show(b);
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics == []


def test_ufcs_respects_method_visibility(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
	fn show(self: m_box.Box<Int>) -> Int
}

export { Show };
""",
		Path("m_impl.drift"): """
module m_impl;

import m_box;
import m_trait;

implement m_trait.Show for m_box.Box<Int> {
	fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
import m_impl;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return m_trait.Show::show(b);
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("exists but is not visible" in m for m in msgs)


def test_visibility_filter_applies_to_inherent_and_trait_candidates(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box { pub value: Int }

export { Box };

implement Box {
\tfn bump(self: Box) -> Int { return self.value + 1; }
}
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Show {
\tfn show(self: m_box.Box) -> Int
}

export { Show };
""",
		Path("m_impl.drift"): """
module m_impl;

import m_box;
import m_trait;

implement m_trait.Show for m_box.Box {
\tfn show(self: m_box.Box) -> Int { return 1; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;
import m_impl;
use trait m_trait.Show;

pub fn main() nothrow -> Int{
\tval b: m_box.Box = m_box.Box(1);
\tval _ = b.bump();
\treturn b.show();
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert len([m for m in msgs if "exists but is not visible" in m]) == 2


def test_ufcs_respects_requirements(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

export { Box };
""",
		Path("m_trait.drift"): """
module m_trait;

import m_box;

pub trait Hashable {
	fn hash(self: Int) -> Int
}

export { Hashable, Show };

implement Hashable for Int {
	pub fn hash(self: Int) -> Int { return self; }
}

pub trait Show {
	fn show(self: m_box.Box<String>) -> Int
}

implement Show for m_box.Box<String> require String is Hashable {
	pub fn show(self: m_box.Box<String>) -> Int { return 0; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_box;
import m_trait;

pub fn main() nothrow -> Int{
	val b: m_box.Box<String> = m_box.Box<type String>("s");
	return m_trait.Show::show(b);
}
""",
	}
	_, result, _sigs, _deps, _ids, _types = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("requirement not satisfied" in m for m in msgs)
	assert any("Hashable" in m for m in msgs)


def test_ufcs_requires_exported_trait(tmp_path: Path) -> None:
	files = {
		Path("m_trait.drift"): """
module m_trait;

pub trait Show {
	fn show(self: Self) -> Int
}
""",
		Path("m_main.drift"): """
module m_main;

import m_trait;

pub fn main() nothrow -> Int{
	return m_trait.Show::show(1);
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _types, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("does not export trait 'Show'" in m for m in msgs)


def test_trait_impl_index_dedupes_duplicate_impls(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_trait/lib.drift"): """
module m_trait;

import m_box;

export { Show };

pub trait Show { fn show(self: m_box.Box<Int>) -> Int }
""",
		Path("m_impl/lib.drift"): """
module m_impl;

import m_box;
import m_trait as t;

implement t.Show for m_box.Box<Int> {
	pub fn show(self: m_box.Box<Int>) -> Int { return 1; }
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, type_table, _exc_catalog, module_exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	module_ids: dict[object, int] = {None: 0}
	trait_impl_index = GlobalTraitImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	impls = module_exports.get("m_impl", {}).get("impls", [])
	assert impls
	impl = impls[0]
	assert isinstance(impl, ImplMeta)
	trait_impl_index.add_impl(impl=impl, type_table=type_table, module_ids=module_ids)
	base_id = type_table.get_struct_base(module_id="m_box", name="Box")
	assert base_id is not None
	assert impl.trait_key is not None
	cands = trait_impl_index.get_candidates(impl.trait_key, base_id, "show")
	assert len(cands) == 1
	assert len({cand.fn_id for cand in cands}) == 1


def test_missing_trait_metadata_is_hard_error(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_traits/lib.drift"): """
module m_traits;

import m_box;

export { Show };

pub trait Show { fn show(self: m_box.Box<Int>) -> Int }
""",
		Path("m_main/main.drift"): """
module m_main;

import m_box;
import m_traits as t;

use trait t.Show;

pub fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
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
	trait_scope_by_module: dict[str, list[object]] = {}
	for _mod, exp in module_exports.items():
		if isinstance(exp, dict):
			scope = exp.get("trait_scope", [])
			if isinstance(scope, list):
				trait_scope_by_module[_mod] = list(scope)
	main_ids = fn_ids_by_name.get("m_main::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	visible_modules = _visible_modules_for("m_main", module_deps, module_ids)
	trait_index = GlobalTraitIndex()
	trait_index.mark_missing(TraitKey(package_id="__local__", module="m_traits", name="Show"))
	trait_impl_index = GlobalTraitImplIndex()
	linked_world, require_env = build_linked_world(type_table)
	type_checker = TypeChecker(type_table=type_table)
	result = type_checker.check_function(
		main_id,
		main_block,
		callable_registry=registry,
		signatures_by_id=signatures,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_modules,
		current_module=module_ids.setdefault("m_main", len(module_ids)),
	)
	assert any("missing trait metadata" in d.message for d in result.diagnostics)


def test_use_trait_is_module_scoped(tmp_path: Path) -> None:
	files = {
		Path("m_box.drift"): """
module m_box;

pub struct Box<T> { pub value: T }

pub trait Show {
	fn show(self: Self) -> Int
}

export { Box, Show };

implement Show for Box<Int> {
	pub fn show(self: Box<Int>) -> Int { return self.value; }
}
""",
		Path("m_main_ok.drift"): """
module m_main_ok;

import m_box;

use trait m_box.Show;

fn ok() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
		Path("m_main_fail.drift"): """
module m_main_fail;

import m_box;

fn fail() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.show();
}
""",
	}
	ok = _typecheck_named_fn(tmp_path, files, module_name="m_main_ok", fn_name="ok")
	assert ok.diagnostics == []
	fail = _typecheck_named_fn(tmp_path, files, module_name="m_main_fail", fn_name="fail")
	assert any("no matching method 'show'" in d.message for d in fail.diagnostics)


def test_use_trait_module_scope_applies_to_captureless_lambda(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

pub struct Point { pub value: Int }

pub trait Show { fn show(self: Point) -> Int }

export { Point, Show };

implement Show for Point {
	pub fn show(self: Point) -> Int { return self.value; }
}
""",
		Path("m_main_good.drift"): """
module m_main_good;

import m_box;

use trait m_box.Show;

fn good() nothrow -> Int {
	val b: m_box.Point = m_box.Point(value = 1);
	val f: Fn(m_box.Point) -> Int = |x| -> Int => x.show();
	return f(b);
}
""",
		Path("m_main_bad.drift"): """
module m_main_bad;

import m_box;

fn bad() nothrow -> Int {
	val b: m_box.Point = m_box.Point(value = 1);
	val f: Fn(m_box.Point) -> Int = |x| -> Int => x.show();
	return f(b);
}
""",
	}
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
	good_ids = fn_ids_by_name.get("m_main_good::good") or []
	bad_ids = fn_ids_by_name.get("m_main_bad::bad") or []
	assert len(good_ids) == 1
	assert len(bad_ids) == 1
	good_id = good_ids[0]
	bad_id = bad_ids[0]
	_good_mir, good_checked = compile_stubbed_funcs(
		func_hirs={good_id: func_hirs[good_id]},
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		origin_by_fn_id=origin_by_fn_id,
		return_checked=True,
	)
	assert all("method 'show'" not in d.message for d in good_checked.diagnostics)
	_bad_mir, bad_checked = compile_stubbed_funcs(
		func_hirs={bad_id: func_hirs[bad_id]},
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		origin_by_fn_id=origin_by_fn_id,
		return_checked=True,
	)
	assert any("method 'show'" in d.message for d in bad_checked.diagnostics)


def test_std_core_fn1_require_auto_resolves_call(tmp_path: Path) -> None:
	"""std.core.Fn1 in require clause auto-expands scope for .call() resolution."""
	files = {
		Path("m_main.drift"): """
module m_main;

import std.core as core;

fn apply<F>(f: F) nothrow -> Void require F is core.Fn1<Int, Void> {
	f.call(42);
}

pub fn main() nothrow -> Int { return 0; }
""",
	}
	result = _typecheck_named_fn(
		tmp_path, files, module_name="m_main", fn_name="apply"
	)
	call_errors = [d for d in result.diagnostics if "no matching method 'call'" in (d.message or "")]
	assert call_errors == [], f"std.core.Fn1 .call() must resolve via require-clause scope expansion: {call_errors}"


def test_user_defined_fn1_does_not_auto_expand_scope(tmp_path: Path) -> None:
	"""A user-defined trait named Fn1 must NOT be auto-injected into scope by the Fn* fallback."""
	files = {
		Path("m_fake.drift"): """
module m_fake;

export { Fn1 };

pub trait Fn1<A, R> {
	fn call(self: Self, a: A) -> R
}

implement Fn1<Int, Int> for Int {
	pub fn call(self: Int, a: Int) -> Int { return a; }
}
""",
		Path("m_main.drift"): """
module m_main;

import m_fake;

fn apply<F>(f: F) -> Int require F is m_fake.Fn1<Int, Int> { return f.call(42); }

pub fn main() nothrow -> Int { return apply<type Int>(1); }
""",
	}
	result = _typecheck_named_fn(
		tmp_path, files, module_name="m_main", fn_name="apply"
	)
	assert result.diagnostics
	msgs = [d.message for d in result.diagnostics]
	assert any("no matching method 'call'" in m for m in msgs), f"user-defined Fn1 must not auto-expand scope; expected 'no matching method' error, got: {msgs}"
