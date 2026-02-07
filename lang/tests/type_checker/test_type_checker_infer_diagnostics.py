#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-

from __future__ import annotations

from pathlib import Path

from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId, function_symbol
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


def _check_main(src: str, tmp_path: Path):
	src_path = tmp_path / "infer_diagnostics.drift"
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
	return result


def test_infer_underdetermined_return_only(tmp_path: Path) -> None:
	result = _check_main(
		"""
fn make<T>() -> T { return 0; }

fn main() -> Int {
	val x = make();
	return 0;
}
""",
		tmp_path,
	)
	assert any("cannot infer type arguments for 'make'" in d.message for d in result.diagnostics)
	assert any("T" in d.message for d in result.diagnostics)


def test_infer_conflicting_args(tmp_path: Path) -> None:
	result = _check_main(
		"""
fn pair<T>(a: T, b: T) -> Int { return 0; }

fn main() -> Int {
	return pair(1, "s");
}
""",
		tmp_path,
	)
	assert any("conflicting constraints" in d.message for d in result.diagnostics)
	assert any("pair" in d.message for d in result.diagnostics)


def test_infer_expected_return_succeeds(tmp_path: Path) -> None:
	result = _check_main(
		"""
struct Box<T> { value: T }

fn make<T>() -> Box<T> { return Box<type T>(0); }

fn main() -> Int {
	val x: Box<Int> = make();
	return 0;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []


def test_explicit_type_args_resolve_underdetermined(tmp_path: Path) -> None:
	result = _check_main(
		"""
fn make<T>() -> T { return 0; }

fn main() -> Int {
	val x = make<type Int>();
	return 0;
}
""",
		tmp_path,
	)
	assert result.diagnostics == []


def test_infer_method_missing_param(tmp_path: Path) -> None:
	result = _check_main(
		"""
struct Box<T> { value: T }

implement<T> Box<T> {
	pub fn convert<U>(self: Box<T>) -> Box<U> { return self; }
}

fn main() -> Int {
	val b: Box<Int> = Box<type Int>(1);
	b.convert();
	return 0;
}
""",
		tmp_path,
	)
	assert any("cannot infer type arguments for method 'convert'" in d.message for d in result.diagnostics)


def test_infer_ctor_field_conflict_notes(tmp_path: Path) -> None:
	result = _check_main(
		"""
struct Pair<T> { a: T, b: T }

fn main() -> Int {
	val p = Pair(a = 1, b = "s");
	return 0;
}
""",
		tmp_path,
	)
	assert any("cannot infer type arguments for struct 'Pair'" in d.message for d in result.diagnostics)
	assert any("field" in note for d in result.diagnostics for note in (d.notes or []))
