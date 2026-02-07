# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.impl_index import GlobalImplIndex
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.stage1 import hir_nodes as H
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _build_registry(signatures: dict[object, object]) -> tuple[CallableRegistry, dict[object, int]]:
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
				name=fn_id.name,
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


def test_callback_dynamic_dispatch(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb = core.callback1(add1);
	return cb.call(41);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	registry, module_ids = _build_registry(signatures)
	impl_index = GlobalImplIndex.from_module_exports(
		module_exports=dict(module_exports),
		type_table=type_table,
		module_ids=module_ids,
	)
	main_ids = _fn_ids_by_name.get("m_main::main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures[main_id]
	param_types = {}
	if main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	visible_mods = _visible_modules_for(main_sig.module, module_deps, module_ids)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id,
		signatures_by_id=signatures,
		callable_registry=registry,
		impl_index=impl_index,
		visible_modules=visible_mods,
		current_module=module_ids.setdefault(main_sig.module, len(module_ids)),
	)
	assert not result.diagnostics
	calls = _collect_method_calls(result.typed_fn.body)
	assert len(calls) == 1
	call = calls[0]
	info = result.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
	assert info is not None
	assert info.sig.can_throw is False


def test_callback_requires_nothrow_function(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn might_throw(x: Int) -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb = core.callback1(might_throw);
	return cb.call(0);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert any("requires a nothrow function" in d.message for d in checked.diagnostics)


def test_callback_arity_mismatch(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn one_arg(x: Int) nothrow -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb = core.callback2(one_arg);
	return cb.call(1, 2);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert any("expects a function with 2 argument(s)" in d.message for d in checked.diagnostics)


def test_callback_rejects_non_function_value(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn main() nothrow -> Int {
	var cb = core.callback1(123);
	return cb.call(0);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert any("expects a function value" in d.message for d in checked.diagnostics)


def test_callback_rejects_type_args(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn zero() nothrow -> Int {
	return 0;
}

fn main() nothrow -> Int {
	var cb = core.callback0<type Int>(zero);
	return cb.call();
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	msgs = [d.message for d in checked.diagnostics]
	assert any("does not accept type arguments" in m for m in msgs)


def test_callback_call_is_nothrow(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb = core.callback1(add1);
	return cb.call(1);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	assert checked.diagnostics == []


def test_callback_call_rejects_kwargs(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb = core.callback1(add1);
	return cb.call(v = 1);
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	msgs = [d.message for d in checked.diagnostics]
	assert any("keyword arguments are not supported for method calls in MVP" in m for m in msgs)


def test_callback_type_mismatch_rejected(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module m_main

import std.core as core;

fn add1(x: Int) nothrow -> Int {
	return x + 1;
}

fn main() nothrow -> Int {
	var cb: core.Callback1<Int, Bool> = core.callback1(add1);
	return 0;
}
""",
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, signatures, _fn_ids_by_name = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=signatures,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	msgs = [d.message for d in checked.diagnostics]
	assert any("does not match declared type" in m for m in msgs)
