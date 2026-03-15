# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId, method_wrapper_id
from lang.driftc.driftc import _inject_method_boundary_wrappers
from lang.driftc.method_registry import CallableDecl, CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.method_resolver import MethodResolution
from lang.driftc.impl_index import GlobalImplIndex, find_impl_method_conflicts
from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.stage1.call_info import CallTargetKind
from lang.driftc.test_helpers import build_linked_world
from lang.driftc.type_checker import TypeChecker
from lang.tests.support.module_packages import mk_module


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
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


def _visible_modules_for(
	module_name: str, module_deps: dict[str, set[str]], module_ids: dict[object, int]
) -> tuple[int, ...]:
	visible = set(module_deps.get(module_name, set()))
	visible.add(module_name)
	return tuple(sorted(module_ids.setdefault(mod, len(module_ids)) for mod in visible))


def _visible_modules_by_name(module_deps: dict[str, set[str]]) -> dict[str, set[str]]:
	return {mod: set(deps) | {mod} for mod, deps in module_deps.items()}


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


def _collect_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HCall):
			calls.append(expr)
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
		for stmt in b.statements:
			if isinstance(stmt, H.HExprStmt):
				walk_expr(stmt.expr)
			elif isinstance(stmt, H.HLet):
				walk_expr(stmt.value)
			elif isinstance(stmt, H.HReturn) and stmt.value is not None:
				walk_expr(stmt.value)
			elif isinstance(stmt, H.HIf):
				walk_expr(stmt.cond)
				walk_block(stmt.then_block)
				if stmt.else_block is not None:
					walk_block(stmt.else_block)
			elif isinstance(stmt, H.HLoop):
				walk_block(stmt.body)
			elif isinstance(stmt, H.HTry):
				walk_block(stmt.body)
				for arm in stmt.catches:
					walk_block(arm.block)
			elif isinstance(stmt, H.HMatch):
				walk_expr(stmt.scrutinee)
				for arm in stmt.arms:
					walk_block(arm.block)
					if arm.result is not None:
						walk_expr(arm.result)

	walk_block(block)
	return calls


def _resolve_main_block(
	tmp_path: Path, files: dict[Path, str], *, main_module: str
) -> tuple[H.HBlock, dict[int, object], dict[FunctionId, object], dict[str, set[str]], dict[object, int]]:
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
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	conflicts = find_impl_method_conflicts(
		module_exports=module_exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name=_visible_modules_by_name(module_deps),
	)
	assert conflicts == []
	main_ids = fn_ids_by_name.get(f"{main_module}::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = _visible_modules_for(main_module, module_deps, module_ids)
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	return main_block, result.typed_fn.call_resolutions, signatures, module_deps, module_ids


def test_method_resolution_cross_module_success(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }

implement<T> Box<T> {
	pub fn tag(self: Box<T>) -> Int { return 1; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_box;

fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.tag();
}
""",
	}
	main_block, call_resolutions, signatures, _deps, _module_ids = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_box"
	assert signatures[res.decl.fn_id].is_method


def test_module_alias_call_resolves_as_free_call(tmp_path: Path) -> None:
	files = {
		Path("m_lib/lib.drift"): """
module m_lib;

export { spawn };

pub fn spawn() nothrow -> Int { return 1; }
""",
		Path("m_main/main.drift"): """
module m_main;

import m_lib as conc;

fn main() nothrow -> Int {
	return conc.spawn();
}
""",
	}
	main_block, call_resolutions, _signatures, _deps, _module_ids = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	method_calls = _collect_method_calls(main_block)
	free_calls = _collect_calls(main_block)
	assert len(method_calls) == 0
	assert len(free_calls) == 1
	res = call_resolutions.get(free_calls[0].node_id)
	assert isinstance(res, CallableDecl)
	assert not isinstance(res, MethodResolution)
	assert res.fn_id is not None and res.fn_id.module == "m_lib"


def test_method_resolution_ambiguity_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_a/lib.drift"): """
module m_a;

import m_types;

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_b/lib.drift"): """
module m_b;

import m_types;

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_types;
import m_a;
import m_b;

fn main() nothrow -> Int{
	val b: m_types.Box<Int> = m_types.Box<type Int>(1);
	return b.tag();
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	assert diagnostics == []
	conflicts = find_impl_method_conflicts(
		module_exports=_exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name=_visible_modules_by_name(module_deps),
	)
	assert conflicts
	msg = conflicts[0].message
	assert "duplicate inherent method" in msg
	assert "m_a" in msg and "m_b" in msg
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	main_ids = fn_ids_by_name.get("m_main::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = _visible_modules_for("m_main", module_deps, module_ids)
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	assert result.diagnostics
	msg = result.diagnostics[0].message
	assert "ambiguous method" in msg
	assert "m_a" in msg and "m_b" in msg


def test_method_visibility_controls_candidates(tmp_path: Path) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_a/lib.drift"): """
module m_a;

import m_types;

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_b/lib.drift"): """
module m_b;

import m_types;

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_types;
import m_a;

fn main() nothrow -> Int{
	val b: m_types.Box<Int> = m_types.Box<type Int>(1);
	return b.tag();
}
""",
	}
	main_block, call_resolutions, signatures, _deps, _module_ids = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_a"
	assert signatures[res.decl.fn_id].is_method


def test_local_impl_wins_over_unimported_impl(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_impl/lib.drift"): """
module m_impl;

import m_box;

implement m_box.Box<Int> {
	pub fn tag(self: m_box.Box<Int>) -> Int { return 2; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_box;

implement m_box.Box<Int> {
	pub fn tag(self: m_box.Box<Int>) -> Int { return 1; }
}

fn main() nothrow -> Int{
	val b: m_box.Box<Int> = m_box.Box<type Int>(1);
	return b.tag();
}
""",
	}
	main_block, call_resolutions, signatures, _deps, _module_ids = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_main"
	assert signatures[res.decl.fn_id].is_method


def test_private_method_not_visible_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_impl/lib.drift"): """
module m_impl;

import m_types;

implement m_types.Box<Int> {
	fn tag(self: m_types.Box<Int>) -> Int { return 1; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_types;
import m_impl;

fn main() nothrow -> Int{
	val b: m_types.Box<Int> = m_types.Box<type Int>(1);
	return b.tag();
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	assert diagnostics == []
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	main_ids = fn_ids_by_name.get("m_main::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = _visible_modules_for("m_main", module_deps, module_ids)
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	assert result.diagnostics
	msg = result.diagnostics[0].message
	assert "exists but is not visible" in msg
	assert "m_impl" in msg


def test_method_resolution_generic_impl_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }
""",
		Path("m_impl/lib.drift"): """
module m_impl;

import m_box;

implement<T> m_box.Box<Array<T>> {
	pub fn inner(self: m_box.Box<Array<T>>) -> T { return self.value[0]; }
}
""",
		Path("m_main/main.drift"): """
module m_main;

import m_box;
import m_impl;

fn main() nothrow -> Int{
	val b: m_box.Box<Array<Int>> = m_box.Box<type Array<Int>>([1, 2]);
	return b.inner();
}
""",
	}
	main_block, call_resolutions, signatures, _deps, _module_ids = _resolve_main_block(
		tmp_path, files, main_module="m_main"
	)
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	res = call_resolutions.get(calls[0].node_id)
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_impl"
	assert signatures[res.decl.fn_id].is_method


def test_duplicate_method_signature_in_single_module_is_error(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }

implement Box<Int> {
	pub fn tag(self: Box<Int>) -> Int { return 1; }
}

implement Box<Int> {
	pub fn tag(self: Box<Int>) -> Int { return 2; }
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, type_table, _exc_catalog, exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	_func_hirs, signatures, _fn_ids_by_name = flatten_modules(_modules)
	assert diagnostics == []
	conflicts = find_impl_method_conflicts(
		module_exports=exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name=_visible_modules_by_name(module_deps),
	)
	assert conflicts
	msg = conflicts[0].message
	assert "duplicate inherent method" in msg
	assert "m_box" in msg


def test_ambiguity_notes_include_visibility_chain(tmp_path: Path, capsys) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types;

export { Box };

pub struct Box { pub value: Int }
""",
		Path("m_impl_a/lib.drift"): """
module m_impl_a;

import m_types;

pub const MARK_A: Int = 1;

export { MARK_A };

implement m_types.Box {
	pub fn tag(self: m_types.Box) -> Int { return 1; }
}
""",
		Path("m_impl_b/lib.drift"): """
module m_impl_b;

import m_types;

pub const MARK_B: Int = 2;

export { MARK_B };

implement m_types.Box {
	pub fn tag(self: m_types.Box) -> Int { return 2; }
}
""",
		Path("m_api/lib.drift"): """
module m_api;

export { m_impl_a.*, m_impl_b.* };
""",
		Path("m_main/main.drift"): """
module m_main;

import m_api;
import m_types;

fn main() nothrow -> Int{
	val b: m_types.Box = m_types.Box(value = 1);
	return b.tag();
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *[str(p) for p in paths]], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	ambiguous = [d for d in diags if "ambiguous method 'tag'" in d.get("message", "")]
	assert ambiguous
	notes = ambiguous[0].get("notes", [])
	assert notes == [
		"m_impl_a visible via: m_main import-> m_api reexport-> m_impl_a",
		"m_impl_b visible via: m_main import-> m_api reexport-> m_impl_b",
	]


def test_cross_module_method_call_uses_boundary_wrapper(tmp_path: Path) -> None:
	files = {
		Path("mod_a/lib.drift"): """
module mod_a;

export { Point };

pub struct Point { pub x: Int }

implement Point {
\tpub fn bump(self: Point) nothrow -> Int { return self.x + 1; }
}
""",
		Path("mod_b/main.drift"): """
module mod_b;

import mod_a as A;

fn main() nothrow -> Int{
\tval p = A.Point(x = 1);
\treturn try p.bump() catch { 0 };
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	module_packages = {}
	mk_module(module_packages, "mod_b", "app")
	mk_module(module_packages, "mod_a", "acme")
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		external_module_packages=module_packages,
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)

	derived_signatures: dict[FunctionId, FnSignature] = {}

	def _register_derived(fn_id: FunctionId, sig: FnSignature) -> None:
		existing = derived_signatures.get(fn_id) or signatures.get(fn_id)
		if existing is not None:
			if existing != sig:
				raise AssertionError(f"signature collision for '{fn_id}'")
			return
		derived_signatures[fn_id] = sig

	_, wrap_errors = _inject_method_boundary_wrappers(
		signatures_by_id=signatures,
		existing_ids=set(signatures.keys()) | set(derived_signatures.keys()),
		register_derived=_register_derived,
		type_table=type_table,
	)
	signatures = {**signatures, **derived_signatures}
	assert wrap_errors == []

	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=module_exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	conflicts = find_impl_method_conflicts(
		module_exports=module_exports,
		signatures_by_id=signatures,
		type_table=type_table,
		visible_modules_by_name=_visible_modules_by_name(module_deps),
	)
	assert conflicts == []

	main_ids = fn_ids_by_name.get("mod_b::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures[main_id]
	param_types = {}
	if main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = _visible_modules_for("mod_b", module_deps, module_ids)
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	assert not result.diagnostics

	calls = _collect_method_calls(result.typed_fn.body)
	assert len(calls) == 1
	call = calls[0]
	info = result.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
	assert info is not None
	assert info.target.kind is CallTargetKind.DIRECT

	impl_id = next(
		fn_id
		for fn_id, sig in signatures.items()
		if sig.is_method and sig.method_name == "bump" and not getattr(sig, "is_wrapper", False)
	)
	assert info.target.symbol == method_wrapper_id(impl_id)
	assert info.sig.can_throw is True


def test_impl_index_keeps_overloaded_methods(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box;

export { Box };

pub struct Box<T> { pub value: T }

implement Box<Int> {
	pub fn tag(self: Box<Int>) -> Int { return 1; }
	pub fn tag(self: Box<Int>, label: String) -> Int { return 2; }
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, type_table, _exc_catalog, exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	module_ids: dict[object, int] = {None: 0}
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=exports,
		type_table=type_table,
		module_ids=module_ids,
	)
	base_id = type_table.get_struct_base(module_id="m_box", name="Box")
	assert base_id is not None
	cands = impl_index.get_candidates(base_id, "tag")
	assert len(cands) == 2
	assert len({cand.fn_id for cand in cands}) == 2
