#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
# author: Sławomir Liszniański; created: 2025-12-09
"""End-to-end check: method resolution failure produces diagnostics and nonzero exit."""

import json
from pathlib import Path

import pytest

from lang.driftc import driftc, stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.impl_index import GlobalImplIndex, ImplMeta, find_impl_method_conflicts
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.parser import ast as parser_ast, parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc.main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name


def _build_registry(signatures: dict[FunctionId, object]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in signatures.items():
		if getattr(sig, "is_wrapper", False):
			continue
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


def _resolve_main_block(tmp_path: Path, source: str) -> tuple[H.HBlock, dict[int, object], dict[FunctionId, object]]:
	src = tmp_path / "main.drift"
	_write_file(src, source)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
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
	trait_scope_by_module: dict[str, list] = {}
	if isinstance(module_exports, dict):
		for _mod_name, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[_mod_name] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	conflicts = find_impl_method_conflicts(
		module_exports=module_exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name={mod: set(deps) | {mod} for mod, deps in module_deps.items()},
	)
	assert conflicts == []
	main_ids = fn_ids_by_name.get("main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = tuple(sorted(module_ids.values()))
	visibility_provenance = {mid: (name,) for name, mid in module_ids.items() if name is not None}
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
		visibility_provenance=visibility_provenance,
	)
	return main_block, result.typed_fn.call_resolutions, signatures


def _resolve_main_with_meta(
	tmp_path: Path, source: str
) -> tuple[H.HBlock, object, dict[FunctionId, object], dict[str, dict[str, object]], object]:
	src = tmp_path / "main.drift"
	_write_file(src, source)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
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
	trait_scope_by_module: dict[str, list] = {}
	if isinstance(module_exports, dict):
		for _mod_name, exp in module_exports.items():
			if isinstance(exp, dict):
				scope = exp.get("trait_scope", [])
				if isinstance(scope, list):
					trait_scope_by_module[_mod_name] = list(scope)
	linked_world, require_env = build_linked_world(type_table)
	conflicts = find_impl_method_conflicts(
		module_exports=module_exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name={mod: set(deps) | {mod} for mod, deps in module_deps.items()},
	)
	assert conflicts == []
	main_ids = fn_ids_by_name.get("main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in module_deps.get("main", {"main"})))
	visibility_provenance = {mid: (name,) for name, mid in module_ids.items() if name is not None}
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		trait_index=trait_index,
		trait_impl_index=trait_impl_index,
		trait_scope_by_module=trait_scope_by_module,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
		visibility_provenance=visibility_provenance,
	)
	return main_block, result, signatures, module_exports, type_table


def _impl_require_for_method(
	module_exports: dict[str, dict[str, object]], fn_id: FunctionId
) -> parser_ast.TraitExpr | None:
	for exp in module_exports.values():
		if not isinstance(exp, dict):
			continue
		impls = exp.get("impls")
		if not isinstance(impls, list):
			continue
		for impl in impls:
			if not isinstance(impl, ImplMeta):
				continue
			for method in impl.methods:
				if method.fn_id == fn_id:
					return impl.require_expr
	return None


def test_method_resolution_failure_reports_diagnostic(tmp_path, capsys: pytest.CaptureFixture[str]):
	src = tmp_path / "bad_method.drift"
	src.write_text(
		"""
module main

struct Point { }

implement Point {
    fn m(self: &Point) -> Int { return 1; }
}

fn main() nothrow -> Int{
    val x = 1;
    return x.m(); // no such method on Int;
}
""".lstrip(),
	)
	rc, payload = _run_driftc_json([str(src)], capsys)
	assert rc == 1
	diags = payload.get("diagnostics", [])
	assert diags
	assert any("no matching method 'm'" in (d.get("message") or "") for d in diags)


def test_cross_module_trait_dot_call_e2e(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_box" / "lib.drift",
		"""
module m_box

export { Box };

pub struct Box { pub value: Int }
""",
	)
	_write_file(
		mod_root / "m_trait" / "lib.drift",
		"""
module m_trait

import m_box;

export { Show };

pub trait Show { fn show(self: m_box.Box) -> Int }

implement Show for m_box.Box {
	pub fn show(self: m_box.Box) -> Int { return self.value; }
}
""",
	)
	_write_file(
		mod_root / "m_main" / "main.drift",
		"""
module m_main

import m_box;
import m_trait as t;

use trait t.Show;

fn main() nothrow -> Int{
	val b: m_box.Box = m_box.Box(value = 1);
	try {
		return b.show();
	} catch {
		return 0;
	}
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	exit_code = driftc.main(["-M", str(mod_root), *[str(p) for p in paths]])
	assert exit_code == 0


def test_cross_module_trait_ufcs_e2e(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_box" / "lib.drift",
		"""
module m_box

export { Box };

pub struct Box { pub value: Int }
""",
	)
	_write_file(
		mod_root / "m_trait" / "lib.drift",
		"""
module m_trait

import m_box;

export { Show };

pub trait Show { fn show(self: m_box.Box) -> Int }

implement Show for m_box.Box {
	pub fn show(self: m_box.Box) -> Int { return self.value + 1; }
}
""",
	)
	_write_file(
		mod_root / "m_main" / "main.drift",
		"""
module m_main

import m_box;
import m_trait;

fn main() nothrow -> Int{
	val b: m_box.Box = m_box.Box(value = 1);
	return try m_trait.Show::show(b) catch { 0 };
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	exit_code = driftc.main(["-M", str(mod_root), *[str(p) for p in paths]])
	assert exit_code == 0


def test_receiver_prefers_ref_mut_over_ref(tmp_path: Path) -> None:
	main_block, call_resolutions, _signatures = _resolve_main_block(
		tmp_path,
		"""
module main

pub struct Box { pub value: Int }

implement Box {
	pub fn f(self: &Box) -> Int { return 1; }
	pub fn f(self: &mut Box) -> Int { return 2; }
}

fn main() nothrow -> Int{
	var b: Box = Box(value = 1);
	return b.f();
}
""".lstrip(),
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None
	assert res.decl.self_mode == SelfMode.SELF_BY_REF_MUT


def test_receiver_prefers_ref_mut_over_value(tmp_path: Path) -> None:
	main_block, call_resolutions, _signatures = _resolve_main_block(
		tmp_path,
		"""
module main

pub struct Box { pub value: Int }

implement Box {
	pub fn g(self: &mut Box) -> Int { return 1; }
	pub fn g(self: Box) -> Int { return 2; }
}

fn main() nothrow -> Int{
	var b: Box = Box(value = 1);
	return b.g();
}
""".lstrip(),
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None
	assert res.decl.self_mode == SelfMode.SELF_BY_REF_MUT


def test_receiver_rvalue_prefers_value(tmp_path: Path) -> None:
	main_block, call_resolutions, _signatures = _resolve_main_block(
		tmp_path,
		"""
module main

pub struct Box { pub value: Int }

pub fn make() -> Box { return Box(value = 1); }

implement Box {
	pub fn h(self: &Box) -> Int { return 1; }
	pub fn h(self: Box) -> Int { return 2; }
}

fn main() nothrow -> Int{
	return make().h();
}
""".lstrip(),
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None
	assert res.decl.self_mode == SelfMode.SELF_BY_VALUE


def test_inherent_require_prefers_trait_dependency(tmp_path: Path) -> None:
	main_block, result, _signatures, _module_exports, type_table = _resolve_main_with_meta(
		tmp_path,
		"""
module main

import std.core as core;

pub struct Box { pub value: Int }
pub struct Item { pub value: Int }

pub trait Debug { fn debug(self: &Self) -> String; }
pub trait Printable require Self is Debug { fn show(self: &Self) -> String; }

implement Debug for Item { fn debug(self: &Item) -> String { return "d"; } }
implement Printable for Item { fn show(self: &Item) -> String { return "p"; } }
implement core.Copy for Item { }

implement Box {
	pub fn f<T>(self: Box, x: T) -> Int require T is Debug { return 1; }
	pub fn f<T>(self: Box, x: T) -> Int require T is Printable { return 2; }
}

fn main() nothrow -> Int{
	val b: Box = Box(value = 1);
	val it: Item = Item(value = 1);
	return b.f(it);
}
""".lstrip(),
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = result.typed_fn.call_resolutions.get(calls[0].node_id)
	assert res is not None
	linked_world, _require_env = build_linked_world(type_table)
	world = linked_world.global_world
	req = world.requires_by_fn.get(res.decl.fn_id)
	assert isinstance(req, parser_ast.TraitIs)
	assert req.trait.name == "Printable"


def test_inherent_require_incomparable_is_ambiguous(tmp_path: Path) -> None:
	_main_block, result, _signatures, _module_exports, _type_table = _resolve_main_with_meta(
		tmp_path,
		"""
module main

import std.core as core;

pub struct Box { pub value: Int }
pub struct Item { pub value: Int }

pub trait A { fn a(self: &Self) -> Int; }
pub trait B { fn b(self: &Self) -> Int; }

implement A for Item { fn a(self: &Item) -> Int { return 1; } }
implement B for Item { fn b(self: &Item) -> Int { return 2; } }
implement core.Copy for Item { }

implement Box {
	pub fn f<T>(self: Box, x: T) -> Int require T is A { return 1; }
	pub fn f<T>(self: Box, x: T) -> Int require T is B { return 2; }
}

fn main() nothrow -> Int{
	val b: Box = Box(value = 1);
	val it: Item = Item(value = 1);
	return b.f(it);
}
""".lstrip(),
	)
	assert any("ambiguous method 'f'" in d.message for d in result.diagnostics)


def test_trait_impl_require_prefers_trait_dependency(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "m_lib" / "lib.drift",
		"""
module m_lib

import std.core as core;

export { Box, Item, Debug, Printable, Show };

pub struct Box<T> { pub value: T }
pub struct Item { pub value: Int }

pub trait Debug { fn debug(self: &Self) -> String; }
pub trait Printable require Self is Debug { fn show(self: &Self) -> String; }
pub trait Show { fn show(self: &Self) -> Int; }

implement Debug for Item { fn debug(self: &Item) -> String { return "d"; } }
implement Printable for Item { fn show(self: &Item) -> String { return "p"; } }
implement core.Copy for Item { }
""",
	)
	_write_file(
		tmp_path / "m_impl_a" / "lib.drift",
		"""
module m_impl_a

import m_lib;

implement<T> m_lib.Show for m_lib.Box<T> require T is m_lib.Debug {
	pub fn show(self: &m_lib.Box<T>) -> Int { return 1; }
}
""",
	)
	_write_file(
		tmp_path / "m_impl_b" / "lib.drift",
		"""
module m_impl_b

import m_lib;

implement<T> m_lib.Show for m_lib.Box<T> require T is m_lib.Printable {
	pub fn show(self: &m_lib.Box<T>) -> Int { return 2; }
}
""",
	)
	main_block, result, _signatures, module_exports, _type_table = _resolve_main_with_meta(
		tmp_path,
		"""
module main

import m_lib;
import m_impl_a;
import m_impl_b;

use trait m_lib.Show;

fn main() nothrow -> Int{
	val it: m_lib.Item = m_lib.Item(value = 1);
	val b: m_lib.Box<m_lib.Item> = m_lib.Box(value = it);
	return b.show();
}
""".lstrip(),
	)
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = result.typed_fn.call_resolutions.get(calls[0].node_id)
	assert res is not None
	req = _impl_require_for_method(module_exports, res.decl.fn_id)
	assert isinstance(req, parser_ast.TraitIs)
	assert req.trait.name == "Printable"
