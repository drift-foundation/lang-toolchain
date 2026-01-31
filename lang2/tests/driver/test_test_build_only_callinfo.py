# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc import stage1 as H
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.impl_index import GlobalImplIndex, find_impl_method_conflicts
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang2.driftc.module_lowered import flatten_modules
from lang2.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang2.driftc.test_helpers import build_linked_world
from lang2.driftc.trait_index import GlobalTraitImplIndex, GlobalTraitIndex
from lang2.driftc.type_checker import TypeChecker
from lang2.driftc.stage1.call_info import CallTargetKind


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")


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
				name=fn_id.name,
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


def _resolve_main_block(tmp_path: Path, sources: dict[Path, str]) -> tuple[H.HBlock, object, dict[FunctionId, object]]:
	for rel_path, content in sources.items():
		_write_file(tmp_path / rel_path, content)
	paths = sorted(tmp_path.rglob("*.drift"))
	modules, type_table, _exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
		test_build_only=True,
	)
	assert diagnostics == []
	func_hirs, signatures, fn_ids_by_name = flatten_modules(modules)
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
	main_id = None
	for fn_id in signatures.keys():
		if fn_id.name == "main" and fn_id.module == "m_main":
			main_id = fn_id
			break
	assert isinstance(main_id, FunctionId)
	main_block = func_hirs[main_id]
	main_sig = signatures.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	visible_mods = tuple(sorted(module_ids.values()))
	visibility_provenance = {
		mid: (name if name is not None else "<unknown>",)
		for name, mid in module_ids.items()
	}
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
	return main_block, result, signatures


def test_test_build_only_method_callinfo_is_direct(tmp_path: Path) -> None:
	sources = {
		Path("m_lib/lib.drift"): """
module m_lib

export { Foo, make };

pub struct Foo { x: Int }

implement Foo {
	@test_build_only
	pub fn test_inc(self: &Foo) -> Int { return self.x + 1; }
}

pub fn make() nothrow -> Foo {
	return Foo(x = 1);
}
""",
		Path("m_main/main.drift"): """
module m_main

import m_lib;

fn main() nothrow -> Int {
	val f = m_lib.make();
	return f.test_inc();
}
""",
	}
	main_block, result, signatures = _resolve_main_block(tmp_path, sources)
	test_sig = None
	for _fn_id, sig in signatures.items():
		if getattr(sig, "module", None) == "m_lib" and getattr(sig, "method_name", None) == "test_inc":
			test_sig = sig
			break
	assert test_sig is not None
	assert getattr(test_sig, "is_pub", False) is True
	assert result.diagnostics == []
	calls = _collect_method_calls(main_block)
	assert len(calls) == 1
	call = calls[0]
	csid = getattr(call, "callsite_id", None)
	assert isinstance(csid, int)
	info = result.typed_fn.call_info_by_callsite_id.get(csid)
	assert info is not None
	assert info.target.kind is CallTargetKind.DIRECT
	assert info.target.symbol is not None
