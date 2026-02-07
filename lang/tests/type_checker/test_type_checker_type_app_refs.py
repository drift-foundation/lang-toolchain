#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name


def _build_registry(sigs: dict[FunctionId, FnSignature]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in sigs.items():
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
			is_generic=bool(sig.type_params),
		)
		next_id += 1
	return registry, module_ids


def _find_callable_ref(block: H.HBlock) -> H.HExpr:
	for stmt in block.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, (H.HTypeApp, H.HFnPtrConst, H.HQualifiedMember)):
			return stmt.value
	raise AssertionError("no callable reference expression found")


def _find_qualified_member(block: H.HBlock) -> H.HQualifiedMember:
	for stmt in block.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HQualifiedMember):
			return stmt.value
	raise AssertionError("no qualified member expression found")


def _find_return_call(block: H.HBlock) -> H.HCall:
	for stmt in block.statements:
		if isinstance(stmt, H.HReturn) and isinstance(stmt.value, H.HCall):
			return stmt.value
	raise AssertionError("no call expression found")


def _find_call_in_let(block: H.HBlock) -> H.HCall:
	for stmt in block.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HCall):
			return stmt.value
	raise AssertionError("no call expression found in let")


def test_type_app_reference_produces_callable_type(tmp_path: Path) -> None:
	src = tmp_path / "type_app_ref.drift"
	src.write_text(
		"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	val f = id<type Int>;
	return f(1);
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	func_hirs = module.func_hirs
	sigs = module.signatures_by_id
	fn_ids_by_name = module.fn_ids_by_name
	registry, module_ids = _build_registry(sigs)
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=registry,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert result.diagnostics == []
	type_app = _find_callable_ref(main_block)
	call_expr = _find_return_call(main_block)
	type_app_ty = result.typed_fn.expr_types.get(type_app.node_id)
	call_ty = result.typed_fn.expr_types.get(call_expr.node_id)
	assert type_app_ty is not None
	assert call_ty is not None
	td_fn = type_table.get(type_app_ty)
	assert td_fn.kind is TypeKind.FUNCTION
	assert td_fn.param_types[-1] == type_table.ensure_int()
	assert td_fn.param_types[0] == type_table.ensure_int()
	assert call_ty == type_table.ensure_int()


def test_qualified_member_type_app_reference(tmp_path: Path) -> None:
	src = tmp_path / "type_app_qmem.drift"
	src.write_text(
		"""
fn main() -> Int {
	val f = Optional::None<type Int>;
	return 0;
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	func_hirs = module.func_hirs
	sigs = module.signatures_by_id
	fn_ids_by_name = module.fn_ids_by_name
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=None,
	)
	assert result.diagnostics == []
	type_app = _find_callable_ref(main_block)
	type_app_ty = result.typed_fn.expr_types.get(type_app.node_id)
	assert type_app_ty is not None
	td_fn = type_table.get(type_app_ty)
	assert td_fn.kind is TypeKind.FUNCTION
	assert td_fn.param_types[-1] == type_table.ensure_instantiated(
		type_table.get_variant_base(module_id="lang.core", name="Optional"),
		[type_table.ensure_int()],
	)


def test_prequalified_member_reference_with_base_args(tmp_path: Path) -> None:
	src = tmp_path / "type_app_qmem_pre.drift"
	src.write_text(
		"""
fn main() -> Int {
	val f = Optional<Int>::None;
	val x: Optional<Int> = f();
	return 0;
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	func_hirs = module.func_hirs
	sigs = module.signatures_by_id
	fn_ids_by_name = module.fn_ids_by_name
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=None,
	)
	assert result.diagnostics == []
	type_app = _find_qualified_member(main_block)
	call_expr = _find_call_in_let(main_block)
	type_app_ty = result.typed_fn.expr_types.get(type_app.node_id)
	call_ty = result.typed_fn.expr_types.get(call_expr.node_id)
	assert type_app_ty is not None
	assert call_ty is not None
	td_fn = type_table.get(type_app_ty)
	assert td_fn.kind is TypeKind.FUNCTION
	assert td_fn.param_types[-1] == type_table.ensure_instantiated(
		type_table.get_variant_base(module_id="lang.core", name="Optional"),
		[type_table.ensure_int()],
	)
	assert call_ty == type_table.ensure_instantiated(
		type_table.get_variant_base(module_id="lang.core", name="Optional"),
		[type_table.ensure_int()],
	)


def test_qualified_member_without_args_is_not_value(tmp_path: Path) -> None:
	src = tmp_path / "type_app_qmem_bare.drift"
	src.write_text(
		"""
fn main() -> Int {
	val f = Optional::None;
	return 0;
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	func_hirs = module.func_hirs
	sigs = module.signatures_by_id
	fn_ids_by_name = module.fn_ids_by_name
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=None,
	)
	assert any("E-QMEM-NOT-CALLABLE" in d.message for d in result.diagnostics)


def test_function_value_call_rejects_type_args(tmp_path: Path) -> None:
	src = tmp_path / "type_app_fn_val.drift"
	src.write_text(
		"""
fn id<T>(value: T) -> T { return value; }
fn main() -> Int {
	val f = id<type Int>;
	return f<type Int>(1);
}
"""
	)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src)
	assert diagnostics == []
	func_hirs = module.func_hirs
	sigs = module.signatures_by_id
	fn_ids_by_name = module.fn_ids_by_name
	registry, module_ids = _build_registry(sigs)
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=sigs,
		callable_registry=registry,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert any("type arguments are not supported on function values" in d.message for d in result.diagnostics)
