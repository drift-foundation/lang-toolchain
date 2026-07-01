# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.core.function_id import FunctionId
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.type_checker import TypeChecker


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _parse_workspace(tmp_path: Path, files: dict[Path, str]):
	mod_root = tmp_path / "mods"
	for rel, content in files.items():
		_write_file(mod_root / rel, content)
	paths = sorted(mod_root.rglob("*.drift"))
	modules, type_table, exc_catalog, module_exports, module_deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	func_hirs, sigs, fn_ids_by_name = flatten_modules(modules)
	return func_hirs, sigs, fn_ids_by_name, type_table, exc_catalog, module_exports, module_deps, diagnostics


def test_fixed_width_rejected_in_user_module(tmp_path: Path) -> None:
	files = {
		Path("main.drift"): """
module main;

	pub fn main() nothrow -> Int{
		val x: Uint8 = cast<Uint8>(1);
		return 0;
	}
""".lstrip(),
	}
	func_hirs, _sigs, _fn_ids_by_name, type_table, _exc_catalog, _exports, _deps, diags = _parse_workspace(tmp_path, files)
	assert diags == []
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	block = func_hirs[fn_id]
	res = TypeChecker(type_table).check_function(fn_id, block, current_module=0, visible_modules=(0,))
	matches = [d for d in res.diagnostics if d.code == "E_FIXED_WIDTH_RESERVED"]
	assert matches
	assert all(d.phase == "typecheck" for d in matches)
	assert all(d.span.line is not None and d.span.column is not None for d in matches)


def test_fixed_width_allowed_in_lang_abi(tmp_path: Path) -> None:
	files = {
		Path("lang/abi/net/lib.drift"): """
module lang.abi.net;

fn takes(x: Uint8) nothrow -> Int{
	return 0;
}
""".lstrip(),
	}
	func_hirs, _sigs, _fn_ids_by_name, type_table, _exc_catalog, _exports, _deps, diags = _parse_workspace(tmp_path, files)
	assert diags == []
	fn_id = FunctionId(module="lang.abi.net", name="takes", ordinal=0)
	block = func_hirs[fn_id]
	res = TypeChecker(type_table).check_function(fn_id, block, current_module=0, visible_modules=(0,))
	assert not res.diagnostics


def test_byte_allowed_in_user_module(tmp_path: Path) -> None:
	files = {
		Path("main.drift"): """
module main;

pub fn main() nothrow -> Int{
	val b: Byte = 0;
	return 0;
}
""".lstrip(),
	}
	func_hirs, _sigs, _fn_ids_by_name, type_table, _exc_catalog, _exports, _deps, diags = _parse_workspace(tmp_path, files)
	assert diags == []
	fn_id = FunctionId(module="main", name="main", ordinal=0)
	block = func_hirs[fn_id]
	res = TypeChecker(type_table).check_function(fn_id, block, current_module=0, visible_modules=(0,))
	assert not res.diagnostics
