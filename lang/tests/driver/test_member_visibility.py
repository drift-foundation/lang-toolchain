# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import compile_stubbed_funcs
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _compile_workspace(tmp_path: Path, files: dict[Path, str]):
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	assert diagnostics == []
	func_hirs, sigs, _fn_ids = flatten_modules(modules)
	_, checked = compile_stubbed_funcs(
		func_hirs=func_hirs,
		signatures=sigs,
		exc_env=exc_catalog,
		type_table=type_table,
		module_exports=module_exports,
		module_deps=module_deps,
		return_checked=True,
	)
	return checked.diagnostics


def test_private_field_access_is_error(tmp_path: Path) -> None:
	diagnostics = _compile_workspace(
		tmp_path,
		{
			Path("m_lib/lib.drift"): """
module m_lib;

export { Point, make };

pub struct Point {
	x: Int,
	pub y: Int
}

pub fn make() nothrow -> Point {
	return Point(x = 1, y = 2);
}
""",
			Path("m_main/main.drift"): """
module m_main;

import m_lib as lib;

pub fn main() nothrow -> Int {
	val p = lib.make();
	val ok = p.y;
	val bad = p.x;
	return ok + bad;
}
""",
		},
	)
	assert any(d.code == "E-PRIVATE-FIELD" for d in diagnostics)


def test_private_field_access_through_borrow_is_error(tmp_path: Path) -> None:
	diagnostics = _compile_workspace(
		tmp_path,
		{
			Path("m_lib/lib.drift"): """
module m_lib;

export { Point, make };

pub struct Point {
	x: Int,
	pub y: Int
}

pub fn make() nothrow -> Point {
	return Point(x = 1, y = 2);
}
""",
			Path("m_main/main.drift"): """
module m_main;

import m_lib as lib;

fn read_x(p: &lib.Point) nothrow -> Int {
	return p.x;
}

pub fn main() nothrow -> Int {
	val p = lib.make();
	return read_x(&p);
}
""",
		},
	)
	assert any(d.code == "E-PRIVATE-FIELD" for d in diagnostics)


def test_public_field_access_is_ok(tmp_path: Path) -> None:
	diagnostics = _compile_workspace(
		tmp_path,
		{
			Path("m_lib/lib.drift"): """
module m_lib;

export { Point, make };

pub struct Point {
	pub x: Int,
	pub y: Int
}

pub fn make() nothrow -> Point {
	return Point(x = 1, y = 2);
}
""",
			Path("m_main/main.drift"): """
module m_main;

import m_lib as lib;

pub fn main() -> Int {
	val p = lib.make();
	return p.x + p.y;
}
""",
		},
	)
	assert diagnostics == []


def test_private_field_in_constructor_is_error(tmp_path: Path) -> None:
	diagnostics = _compile_workspace(
		tmp_path,
		{
			Path("m_lib/lib.drift"): """
module m_lib;

export { Point };

pub struct Point {
	x: Int,
	pub y: Int
}
""",
			Path("m_main/main.drift"): """
module m_main;

import m_lib as lib;

pub fn main() nothrow -> Int {
	val p = lib.Point(x = 1, y = 2);
	return p.y;
}
""",
		},
	)
	assert any(d.code == "E-PRIVATE-FIELD" for d in diagnostics)


def test_exported_value_using_private_type_is_error(tmp_path: Path) -> None:
	diagnostics = _compile_workspace(
		tmp_path,
		{
			Path("m_lib/lib.drift"): """
module m_lib;

export { make };

struct Hidden(x: Int);

pub fn make() nothrow -> Hidden {
	return Hidden(x = 1);
}
""",
			Path("m_main/main.drift"): """
module m_main;

import m_lib as lib;

pub fn main() nothrow -> Int {
	val _h = lib.make();
	return 0;
}
""",
		},
	)
	assert any(d.code == "E-PRIVATE-TYPE" for d in diagnostics)
