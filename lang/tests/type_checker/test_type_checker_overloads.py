#!/usr/bin/env python3
# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Overload resolution coverage: arity, param type, and require ranking."""

from __future__ import annotations

from pathlib import Path

from lang.driftc import stage1 as H
from lang.driftc.checker import FnSignature
from lang.driftc.core.function_id import FunctionId
from lang.driftc.core.types_core import TypeKind
from lang.driftc.method_registry import CallableRegistry, CallableSignature, Visibility
from lang.driftc.parser import ast as parser_ast
from lang.driftc.parser import parse_drift_to_hir
from lang.driftc.type_checker import TypeChecker
from lang.driftc.test_helpers import build_linked_world


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
			is_generic=False,
		)
		next_id += 1
	return registry, module_ids


def _first_call(block: H.HBlock) -> H.HCall:
	for stmt in block.statements:
		if isinstance(stmt, H.HReturn) and isinstance(stmt.value, H.HCall):
			return stmt.value
		if isinstance(stmt, H.HExprStmt) and isinstance(stmt.expr, H.HCall):
			return stmt.expr
	raise AssertionError("no call expression found in block")


def _type_id_for_name(type_table, name: str) -> int:
	if name == "Int":
		return type_table.ensure_int()
	if name == "String":
		return type_table.ensure_string()
	if name == "Bool":
		return type_table.ensure_bool()
	if name == "Uint":
		return type_table.ensure_uint()
	if name == "Float":
		return type_table.ensure_float()
	return type_table.require_nominal(kind=TypeKind.STRUCT, module_id="main", name=name)


def _resolve_main_call(
	tmp_path: Path,
	src: str,
) -> tuple[FunctionId, dict[FunctionId, FnSignature], object]:
	src_path = tmp_path / "overload.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	registry, module_ids = _build_registry(module.signatures_by_id)
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	call_expr = _first_call(main_block)
	tc = TypeChecker(type_table=type_table)
	main_sig = module.signatures_by_id.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	linked_world, require_env = build_linked_world(type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=registry,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert result.diagnostics == []
	decl = result.typed_fn.call_resolutions.get(call_expr.node_id)
	assert decl is not None
	assert getattr(decl, "fn_id", None) is not None
	return decl.fn_id, module.signatures_by_id, type_table


def test_overload_by_arity_picks_matching_signature(tmp_path: Path) -> None:
	resolved_id, sigs, type_table = _resolve_main_call(
		tmp_path,
		"""
fn f() -> Int { return 1; }
fn f(x: Int) -> Int { return x; }
fn main() -> Int { return f(1); }
""",
	)
	expected_param_types = [_type_id_for_name(type_table, "Int")]
	expected = next(
		fn_id
		for fn_id, sig in sigs.items()
		if sig.name == "f" and list(sig.param_type_ids or []) == expected_param_types
	)
	assert resolved_id == expected


def test_overload_by_param_type_picks_matching_signature(tmp_path: Path) -> None:
	resolved_id, sigs, type_table = _resolve_main_call(
		tmp_path,
		"""
fn g(x: Int) -> Int { return x; }
fn g(x: String) -> Int { return 1; }
fn main() -> Int { return g("hi"); }
""",
	)
	expected_param_types = [_type_id_for_name(type_table, "String")]
	expected = next(
		fn_id
		for fn_id, sig in sigs.items()
		if sig.name == "g" and list(sig.param_type_ids or []) == expected_param_types
	)
	assert resolved_id == expected


def test_overload_require_rejects_when_unmet(tmp_path: Path) -> None:
	src = """
struct S { }
trait A { fn a(self: S) -> Int }
fn h<T>(x: T) -> Int require T is A { return 1; }
fn h(x: Int) -> Int { return x; }
fn main(x: S) -> Int { return h(x); }
"""
	src_path = tmp_path / "overload_require_reject.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	registry, module_ids = _build_registry(module.signatures_by_id)
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	tc = TypeChecker(type_table=type_table)
	main_sig = module.signatures_by_id.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	linked_world, require_env = build_linked_world(type_table)
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=registry,
		linked_world=linked_world,
		require_env=require_env,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert any("requirement not satisfied" in d.message for d in result.diagnostics)


def test_overload_type_arg_count_selects_matching_candidate(tmp_path: Path) -> None:
	resolved_id, sigs, _type_table = _resolve_main_call(
		tmp_path,
		"""
fn f<T>(x: T) -> T { return x; }
fn f<T, U>(x: T, y: U) -> T { return x; }
fn main() -> Int { return f<type Int, String>(1, "s"); }
""",
	)
	expected = next(
		fn_id
		for fn_id, sig in sigs.items()
		if sig.name == "f" and len(list(sig.param_names or [])) == 2
	)
	assert resolved_id == expected


def test_overload_require_prefers_trait_dependency(tmp_path: Path) -> None:
	resolved_id, _sigs, type_table = _resolve_main_call(
		tmp_path,
		"""
struct S { }

trait Debug { fn debug(self: &Self) -> String; }
trait Printable require Self is Debug { fn show(self: &Self) -> String; }

implement Debug for S { fn debug(self: &S) -> String { return "d"; } }
implement Printable for S { fn show(self: &S) -> String { return "p"; } }

fn f<T>(x: T) -> Int require T is Debug { return 1; }
fn f<T>(x: T) -> Int require T is Printable { return 2; }

fn main(x: S) -> Int { return f(x); }
""",
	)
	linked_world, _require_env = build_linked_world(type_table)
	world = linked_world.global_world
	req = world.requires_by_fn.get(resolved_id)
	assert isinstance(req, parser_ast.TraitIs)
	assert req.trait.name == "Printable"


def test_overload_require_incomparable_is_ambiguous(tmp_path: Path) -> None:
	src = """
struct S { }

trait A { fn a(self: &Self) -> Int; }
trait B { fn b(self: &Self) -> Int; }

implement A for S { fn a(self: &S) -> Int { return 1; } }
implement B for S { fn b(self: &S) -> Int { return 2; } }

fn f<T>(x: T) -> Int require T is A { return 1; }
fn f<T>(x: T) -> Int require T is B { return 2; }

fn main(x: S) -> Int { return f(x); }
"""
	src_path = tmp_path / "overload_require_incomparable.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	registry, module_ids = _build_registry(module.signatures_by_id)
	fn_ids = module.fn_ids_by_name.get("main") or []
	assert len(fn_ids) == 1
	main_id = fn_ids[0]
	main_block = module.func_hirs[main_id]
	call_expr = _first_call(main_block)
	tc = TypeChecker(type_table=type_table)
	main_sig = module.signatures_by_id.get(main_id)
	param_types = {}
	if main_sig and main_sig.param_names and main_sig.param_type_ids:
		param_types = {pname: pty for pname, pty in zip(main_sig.param_names, main_sig.param_type_ids)}
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	result = tc.check_function(
		main_id,
		main_block,
		param_types=param_types,
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=registry,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert any("ambiguous call to function 'f'" in d.message for d in result.diagnostics)


def test_overload_dedupes_duplicate_candidates(tmp_path: Path) -> None:
	src = """
fn f(x: Int) -> Int { return x; }
fn main() -> Int { return f(1); }
"""
	src_path = tmp_path / "overload_dedupe.drift"
	src_path.write_text(src)
	module, type_table, _exc_catalog, diagnostics = parse_drift_to_hir(src_path)
	assert diagnostics == []
	registry, module_ids = _build_registry(module.signatures_by_id)
	fn_ids = module.fn_ids_by_name.get("f") or []
	assert len(fn_ids) == 1
	f_id = fn_ids[0]
	f_sig = module.signatures_by_id.get(f_id)
	assert f_sig is not None
	assert f_sig.param_type_ids is not None
	assert f_sig.return_type_id is not None
	module_id = module_ids.setdefault(f_sig.module, len(module_ids))
	registry.register_free_function(
		callable_id=999,
		name=_callable_name(f_id),
		module_id=module_id,
		visibility=Visibility.public(),
		signature=CallableSignature(
			param_types=tuple(f_sig.param_type_ids),
			result_type=f_sig.return_type_id,
		),
		fn_id=f_id,
		is_generic=False,
	)
	main_ids = module.fn_ids_by_name.get("main") or []
	assert len(main_ids) == 1
	main_id = main_ids[0]
	main_block = module.func_hirs[main_id]
	call_expr = _first_call(main_block)
	tc = TypeChecker(type_table=type_table)
	main_sig = module.signatures_by_id.get(main_id)
	current_mod = module_ids.setdefault(main_sig.module, len(module_ids))
	result = tc.check_function(
		main_id,
		main_block,
		param_types={},
		return_type=main_sig.return_type_id if main_sig is not None else None,
		signatures_by_id=module.signatures_by_id,
		callable_registry=registry,
		visible_modules=(current_mod,),
		current_module=current_mod,
	)
	assert result.diagnostics == []
	resolved = result.typed_fn.call_resolutions.get(call_expr.node_id)
	assert resolved is not None
	assert getattr(resolved, "fn_id", None) == f_id
