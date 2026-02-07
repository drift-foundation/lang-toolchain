# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.generic_type_expr import GenericTypeExpr
from lang.driftc.parser import parse_drift_to_hir


def _has_error(diags: list[object], match: str) -> bool:
	return any(getattr(d, "severity", None) == "error" and match in str(getattr(d, "message", "")) for d in diags)


def _parse(tmp_path: Path, src: str):
	path = tmp_path / "main.drift"
	path.write_text(src)
	return parse_drift_to_hir(path)


def test_interface_method_schema_is_recorded(tmp_path: Path) -> None:
	src = """
interface Callback1<A, R> {
	fn call(x: A) -> R
}
"""
	_module, type_table, _exc_catalog, diagnostics = _parse(tmp_path, src)
	assert not [d for d in diagnostics if getattr(d, "severity", None) == "error"]
	base_id = type_table.get_interface_base(module_id="main", name="Callback1")
	assert base_id is not None
	schema = type_table.get_interface_schema(base_id)
	assert schema is not None
	assert schema.name == "Callback1"
	assert schema.type_params == ["A", "R"]
	assert len(schema.methods) == 1
	method = schema.methods[0]
	assert method.name == "call"
	assert method.type_params == []
	assert len(method.params) == 1
	assert method.params[0].name == "x"
	assert method.params[0].type_expr == GenericTypeExpr.param(0)
	assert method.return_type == GenericTypeExpr.param(1)


def test_interface_duplicate_method_names_error(tmp_path: Path) -> None:
	src = """
interface I {
	fn f() -> Int
	fn f() -> Int
}
"""
	_module, _type_table, _exc_catalog, diagnostics = _parse(tmp_path, src)
	assert _has_error(diagnostics, "duplicate method 'f' in interface 'I'")


def test_interface_method_type_param_duplicate_error(tmp_path: Path) -> None:
	src = """
interface I {
	fn f<T, T>(x: T) -> T
}
"""
	_module, _type_table, _exc_catalog, diagnostics = _parse(tmp_path, src)
	assert _has_error(diagnostics, "duplicate type parameter 'T' in interface method 'f'")


def test_interface_method_type_param_shadow_error(tmp_path: Path) -> None:
	src = """
interface I<T> {
	fn f<T>(x: T) -> T
}
"""
	_module, _type_table, _exc_catalog, diagnostics = _parse(tmp_path, src)
	assert _has_error(diagnostics, "interface method 'f' shadows interface type parameter 'T'")


def test_interface_duplicate_method_from_parent_error(tmp_path: Path) -> None:
	src = """
interface A {
	fn f() -> Int
}
interface B: A {
	fn f() -> Int
}
"""
	_module, _type_table, _exc_catalog, diagnostics = _parse(tmp_path, src)
	assert _has_error(diagnostics, "inherits duplicate method 'f'")
