# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc import stage1 as H
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang2.driftc.impl_index import GlobalImplIndex, find_impl_method_conflicts
from lang2.driftc.parser import parse_drift_workspace_to_hir
from lang2.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"


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
	return tuple(sorted(module_ids.get(mod, 0) for mod in visible))


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


def _resolve_main_block(
	tmp_path: Path, files: dict[Path, str], *, main_module: str
) -> tuple[H.HBlock, dict[int, object], dict[FunctionId, object], dict[str, set[str]], dict[object, int]]:
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	func_hirs, signatures, fn_ids_by_name, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
	)
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
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		visible_modules=visible_mods,
		current_module=current_mod,
	)
	return main_block, result.typed_fn.call_resolutions, signatures, module_deps, module_ids


def test_method_resolution_cross_module_success(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box

export { Box }

pub struct Box<T> { value: T }

implement<T> Box<T> {
	pub fn tag(self: Box<T>) returns Int { return 1; }
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_box

fn main() returns Int {
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
	res = call_resolutions.get(id(calls[0]))
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_box"
	assert signatures[res.decl.fn_id].is_method


def test_method_resolution_ambiguity_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types

export { Box }

pub struct Box<T> { value: T }
""",
		Path("m_a/lib.drift"): """
module m_a

import m_types

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) returns Int { return 1; }
}
""",
		Path("m_b/lib.drift"): """
module m_b

import m_types

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) returns Int { return 2; }
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_types
import m_a
import m_b

fn main() returns Int {
	val b: m_types.Box<Int> = m_types.Box<type Int>(1);
	return b.tag();
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	func_hirs, signatures, fn_ids_by_name, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
	)
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
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
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
module m_types

export { Box }

pub struct Box<T> { value: T }
""",
		Path("m_a/lib.drift"): """
module m_a

import m_types

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) returns Int { return 1; }
}
""",
		Path("m_b/lib.drift"): """
module m_b

import m_types

implement m_types.Box<Int> {
	pub fn tag(self: m_types.Box<Int>) returns Int { return 2; }
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_types
import m_a

fn main() returns Int {
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
	res = call_resolutions.get(id(calls[0]))
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_a"
	assert signatures[res.decl.fn_id].is_method


def test_private_method_not_visible_across_modules(tmp_path: Path) -> None:
	files = {
		Path("m_types/lib.drift"): """
module m_types

export { Box }

pub struct Box<T> { value: T }
""",
		Path("m_impl/lib.drift"): """
module m_impl

import m_types

implement m_types.Box<Int> {
	fn tag(self: m_types.Box<Int>) returns Int { return 1; }
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_types
import m_impl

fn main() returns Int {
	val b: m_types.Box<Int> = m_types.Box<type Int>(1);
	return b.tag();
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	func_hirs, signatures, fn_ids_by_name, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
	)
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
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
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
module m_box

export { Box }

pub struct Box<T> { value: T }
""",
		Path("m_impl/lib.drift"): """
module m_impl

import m_box

implement<T> m_box.Box<Array<T>> {
	pub fn inner(self: m_box.Box<Array<T>>) returns T { return self.value[0]; }
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_box
import m_impl

fn main() returns Int {
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
	res = call_resolutions.get(id(calls[0]))
	assert res is not None and res.decl.fn_id is not None
	assert res.decl.fn_id.module == "m_impl"
	assert signatures[res.decl.fn_id].is_method


def test_duplicate_method_signature_in_single_module_is_error(tmp_path: Path) -> None:
	files = {
		Path("m_box/lib.drift"): """
module m_box

export { Box }

pub struct Box<T> { value: T }

implement Box<Int> {
	pub fn tag(self: Box<Int>) returns Int { return 1; }
}

implement Box<Int> {
	pub fn tag(self: Box<Int>) returns Int { return 2; }
}
""",
	}
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	_func_hirs, signatures, _fn_ids_by_name, type_table, _exc_catalog, exports, module_deps, diagnostics = (
		parse_drift_workspace_to_hir(
			paths,
			module_paths=[mod_root],
		)
	)
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
