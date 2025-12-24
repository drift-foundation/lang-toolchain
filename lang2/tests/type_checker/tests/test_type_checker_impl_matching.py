#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang2.driftc import stage1 as H
from lang2.driftc.checker import FnSignature
from lang2.driftc.core.function_id import FunctionId
from lang2.driftc.method_registry import CallableRegistry, CallableSignature, SelfMode, Visibility
from lang2.driftc.parser import parse_drift_to_hir
from lang2.driftc.type_checker import TypeChecker


def _callable_name(fn_id: FunctionId) -> str:
	return fn_id.name if fn_id.module == "main" else f"{fn_id.module}::{fn_id.name}"


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
	src_path = tmp_path / "impl_matching.drift"
	src_path.write_text(src)
	func_hirs, sigs, fn_ids_by_name, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	fn_ids = fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_sig = sigs.get(main_id)
	main_block = func_hirs[main_id]
	registry, module_ids = _build_registry(sigs)
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
	return result, type_table, sigs, main_block


def _find_impl_method_id(
	sigs: dict[FunctionId, FnSignature], *, method_name: str, impl_args: list[int]
) -> FunctionId:
	for fn_id, sig in sigs.items():
		if not sig.is_method:
			continue
		if (sig.method_name or sig.name) != method_name:
			continue
		if sig.impl_target_type_args == impl_args:
			return fn_id
	raise AssertionError("no matching method signature found")


def test_impl_target_concrete_args_match_correct_impl(tmp_path: Path) -> None:
	result, type_table, sigs, main_block = _check_main(
		"""
struct Box<T> { value: T }

implement Box<Int> {
	fn tag(self: Box<Int>) returns String { return "int"; }
}

implement Box<String> {
	fn tag(self: Box<String>) returns String { return "str"; }
}

fn main() returns String {
	val b: Box<Int> = Box<type Int>(1);
	val c: Box<String> = Box<type String>("s");
	val x = b.tag();
	val y = c.tag();
	return x;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_method_calls(main_block)
	assert len(calls) >= 2
	int_ty = type_table.ensure_int()
	str_ty = type_table.ensure_string()
	expected_int = _find_impl_method_id(sigs, method_name="tag", impl_args=[int_ty])
	expected_str = _find_impl_method_id(sigs, method_name="tag", impl_args=[str_ty])
	res_a = result.typed_fn.call_resolutions.get(id(calls[0]))
	res_b = result.typed_fn.call_resolutions.get(id(calls[1]))
	assert res_a is not None and res_b is not None
	assert res_a.decl.fn_id == expected_int
	assert res_b.decl.fn_id == expected_str


def test_impl_missing_for_concrete_args_reports_error(tmp_path: Path) -> None:
	result, _type_table, _sigs, _main_block = _check_main(
		"""
struct Box<T> { value: T }

implement Box<Int> {
	fn tag(self: Box<Int>) returns String { return "int"; }
}

fn main() returns String {
	val b: Box<Bool> = Box<type Bool>(false);
	return b.tag();
}
""",
		tmp_path,
	)
	assert any("no matching method 'tag'" in d.message for d in result.diagnostics)


def test_impl_ambiguity_is_error(tmp_path: Path) -> None:
	result, _type_table, _sigs, _main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	fn tag(self: Box<T>) returns String { return "gen"; }
}

implement Box<Int> {
	fn tag(self: Box<Int>) returns String { return "int"; }
}

fn main() returns String {
	val b: Box<Int> = Box<type Int>(1);
	return b.tag();
}
""",
		tmp_path,
	)
	assert any("ambiguous method 'tag'" in d.message for d in result.diagnostics)


def test_impls_without_method_name_do_not_ambiguous(tmp_path: Path) -> None:
	result, _type_table, _sigs, _main_block = _check_main(
		"""
struct Box<T> { value: T }

implement Box<Int> {
	fn a(self: Box<Int>) returns Int { return self.value; }
}

implement Box<Int> {
	fn b(self: Box<Int>) returns Int { return self.value; }
}

fn main() returns Int {
	val b: Box<Int> = Box<type Int>(1);
	return b.a();
}
""",
		tmp_path,
	)
	assert result.diagnostics == []


def test_impl_and_method_generics_layered(tmp_path: Path) -> None:
	result, type_table, _sigs, main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	fn wrap<U>(self: Box<T>, u: U) returns Box<T> { return self; }
}

fn main() returns Int {
	val b: Box<Int> = Box<type Int>(1);
	val r = b.wrap("s");
	return r.value;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_method_calls(main_block)
	assert len(calls) == 1
	box_base = type_table.get_struct_base(module_id="main", name="Box")
	assert box_base is not None
	expected = type_table.ensure_struct_instantiated(box_base, [type_table.ensure_int()])
	call_ty = result.typed_fn.expr_types.get(id(calls[0]))
	assert call_ty == expected


def test_impl_nested_pattern_matches_receiver(tmp_path: Path) -> None:
	result, type_table, _sigs, main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<Array<T>> {
	fn inner(self: Box<Array<T>>) returns T { return self.value[0]; }
}

fn main() returns Int {
	val b: Box<Array<Int>> = Box<type Array<Int>>([1, 2]);
	return b.inner();
}
""",
		tmp_path,
	)
	assert result.diagnostics == []
	calls = _find_method_calls(main_block)
	assert len(calls) == 1
	call_ty = result.typed_fn.expr_types.get(id(calls[0]))
	assert call_ty == type_table.ensure_int()


def test_impl_nested_pattern_no_match(tmp_path: Path) -> None:
	result, _type_table, _sigs, _main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<Array<T>> {
	fn inner(self: Box<Array<T>>) returns T { return self.value[0]; }
}

fn main() returns Int {
	val b: Box<Int> = Box<type Int>(1);
	return b.inner();
}
""",
		tmp_path,
	)
	assert any("no matching method 'inner'" in d.message for d in result.diagnostics)


def test_impl_nested_pattern_ambiguity(tmp_path: Path) -> None:
	result, _type_table, _sigs, _main_block = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<Array<T>> {
	fn inner(self: Box<Array<T>>) returns T { return self.value[0]; }
}

implement Box<Array<Int>> {
	fn inner(self: Box<Array<Int>>) returns Int { return self.value[0]; }
}

fn main() returns Int {
	val b: Box<Array<Int>> = Box<type Array<Int>>([1, 2]);
	return b.inner();
}
""",
		tmp_path,
	)
	assert any("ambiguous method 'inner'" in d.message for d in result.diagnostics)
