# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.stage1.call_info import CallTargetKind
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.type_checker import TypeChecker
from lang.driftc.test_helpers import build_linked_world


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
		if sig.is_method:
			continue
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		registry.register_free_function(
			callable_id=next_id,
			name=_callable_name(fn_id),
			module_id=module_id,
			visibility=Visibility.public(),
			signature=CallableSignature(param_types=tuple(sig.param_type_ids), result_type=sig.return_type_id),
			fn_id=fn_id,
			is_generic=False,
		)
		next_id += 1
	return registry, module_ids


def _collect_calls(block: H.HBlock) -> list[H.HCall]:
	calls: list[H.HCall] = []

	def walk_expr(expr: H.HExpr) -> None:
		if isinstance(expr, H.HCall):
			calls.append(expr)
			walk_expr(expr.fn)
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


def _find_fn_id_by_param_type(signatures: dict[FunctionId, object], *, name: str, param_type_id: int) -> FunctionId:
	for fn_id, sig in signatures.items():
		if fn_id.name != name:
			continue
		if sig.param_type_ids and list(sig.param_type_ids) == [param_type_id]:
			return fn_id
	raise AssertionError(f"missing overload for {name}({param_type_id})")


def test_overloads_in_module(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m" / "lib.drift",
		"""
module m

export { f };

pub fn f(x: Int) -> Int { return x + 1; }
pub fn f(x: String) -> Int { return 2; }
""",
	)
	_write_file(
		mod_root / "main.drift",
		"""
module main

import m as m;

fn main() nothrow -> Int{
	val a: Int = m.f(1);
	val b: Int = m.f("hi");
	return a + b;
}
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, _exc_catalog, _exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert not diagnostics
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
	registry, module_ids = _build_registry(signatures)

	main_ids = fn_ids_by_name.get("main::main") or fn_ids_by_name.get("main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = func_hirs[main_id]
	main_sig = signatures[main_id]
	param_types = {}
	if main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	linked_world, require_env = build_linked_world(type_table)
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id,
		signatures_by_id=signatures,
		callable_registry=registry,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=tuple(module_ids.values()),
		current_module=current_mod,
	)
	assert not result.diagnostics
	calls = _collect_calls(result.typed_fn.body)
	assert len(calls) == 2
	int_fn = _find_fn_id_by_param_type(signatures, name="f", param_type_id=type_table.ensure_int())
	str_fn = _find_fn_id_by_param_type(signatures, name="f", param_type_id=type_table.ensure_string())
	targets = []
	for call in calls:
		info = result.typed_fn.call_info_by_callsite_id.get(call.callsite_id)
		assert info is not None
		assert info.target.kind is CallTargetKind.DIRECT
		targets.append(info.target.symbol)
	assert int_fn in targets
	assert str_fn in targets
