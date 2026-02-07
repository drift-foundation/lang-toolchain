#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name


def _build_registry(sigs: dict[FunctionId, FnSignature]) -> tuple[CallableRegistry, dict[object, int]]:
	registry = CallableRegistry()
	module_ids: dict[object, int] = {None: 0}
	next_id = 1
	for fn_id, sig in sigs.items():
		if sig.param_type_ids is None or sig.return_type_id is None:
			continue
		module_id = module_ids.setdefault(sig.module, len(module_ids))
		param_types_tuple = tuple(sig.param_type_ids)
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
				visibility=Visibility.public(),
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
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
				signature=CallableSignature(param_types=param_types_tuple, result_type=sig.return_type_id),
				fn_id=fn_id,
				is_generic=bool(sig.type_params),
			)
		next_id += 1
	return registry, module_ids


def _find_method_calls(block: H.HBlock) -> list[H.HMethodCall]:
	calls: list[H.HMethodCall] = []
	for stmt in block.statements:
		if isinstance(stmt, H.HLet) and isinstance(stmt.value, H.HMethodCall):
			calls.append(stmt.value)
		elif isinstance(stmt, H.HExprStmt) and isinstance(stmt.expr, H.HMethodCall):
			calls.append(stmt.expr)
		elif isinstance(stmt, H.HReturn) and isinstance(stmt.value, H.HMethodCall):
			calls.append(stmt.value)
	return calls


def _check_main(src: str, tmp_path: Path):
	src_path = tmp_path / "method_infer.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	main_sig = module.signatures_by_id.get(main_id)
	registry, module_ids = _build_registry(module.signatures_by_id)
	tc = TypeChecker(type_table=type_table)
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	result = tc.check_function(
		main_id,
		main_block,
		param_types=None,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=registry,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	return result, type_table, main_block


def test_method_infers_from_receiver_and_arg(tmp_path: Path) -> None:
	result, type_table, main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	fn apply<U>(self: Box<T>, u: U) -> U { return u; }
}

fn main() -> String {
	val b: Box<Int> = Box<type Int>(1);
	return b.apply("s");
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_method_calls(main_block)
	assert len(calls) == 1
	call_ty = result.typed_fn.expr_types.get(calls[0].node_id)
	assert call_ty == type_table.ensure_string()


def test_method_infers_from_expected_return_type(tmp_path: Path) -> None:
	result, type_table, main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	fn none<U>(self: Box<T>) -> Optional<U> { return Optional::None(); }
}

fn main() -> Optional<Int> {
	val b: Box<Int> = Box<type Int>(1);
	val x: Optional<Int> = b.none();
	return x;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_method_calls(main_block)
	assert len(calls) == 1
	call_ty = result.typed_fn.expr_types.get(calls[0].node_id)
	optional_base = type_table.get_variant_base(module_id="lang.core", name="Optional")
	assert optional_base is not None
	expected = type_table.ensure_instantiated(optional_base, [type_table.ensure_int()])
	assert call_ty == expected

