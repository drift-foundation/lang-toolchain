# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, parse_drift_files_to_hir, stdlib_root
from lang.driftc.module_lowered import flatten_modules
from lang.driftc.traits.world import TraitKey


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


def test_use_trait_resolves_and_records_scope(tmp_path: Path) -> None:
	files = {
		Path("m_traits/lib.drift"): """
module m_traits;

export { Show };

pub trait Show { fn show(self: Int) -> Int }
""",
		Path("m_main/main.drift"): """
module m_main;

import m_traits as t;

use trait t.Show;

fn main() nothrow -> Int{ return 0; }
""",
	}
	_func_hirs, _sigs, _fn_ids_by_name, _type_table, _exc_catalog, module_exports, _deps, diagnostics = _parse_workspace(
		tmp_path, files
	)
	assert diagnostics == []
	scope = module_exports.get("m_main", {}).get("trait_scope", [])
	assert TraitKey(package_id="__local__", module="m_traits", name="Show") in scope


def test_use_trait_unknown_trait_is_error(tmp_path: Path) -> None:
	files = {
		Path("m_traits/lib.drift"): """
module m_traits;

export { Show };

pub trait Show { fn show(self: Int) -> Int }
""",
		Path("m_main/main.drift"): """
module m_main;

import m_traits;

use trait m_traits.Missing;

fn main() nothrow -> Int{ return 0; }
""",
	}
	*_rest, diagnostics = _parse_workspace(tmp_path, files)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("module 'm_traits' does not export trait 'Missing'" in m for m in msgs)


def test_use_trait_requires_export(tmp_path: Path) -> None:
	files = {
		Path("m_traits/lib.drift"): """
module m_traits;

pub trait Show { fn show(self: Int) -> Int }
""",
		Path("m_main/main.drift"): """
module m_main;

import m_traits;

use trait m_traits.Show;

fn main() nothrow -> Int{ return 0; }
""",
	}
	*_rest, diagnostics = _parse_workspace(tmp_path, files)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("module 'm_traits' does not export trait 'Show'" in m for m in msgs)
	notes = [note for d in diagnostics for note in (d.notes or [])]
	assert any("exports no traits (private by default)" in n for n in notes)


def test_use_trait_requires_import_alias(tmp_path: Path) -> None:
	files = {
		Path("m_traits/lib.drift"): """
module m_traits;

export { Show };

pub trait Show { fn show(self: Int) -> Int }
""",
		Path("m_main/main.drift"): """
module m_main;

use trait missing.Show;

fn main() nothrow -> Int{ return 0; }
""",
	}
	*_rest, diagnostics = _parse_workspace(tmp_path, files)
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("unknown module alias 'missing' in trait reference 'missing.Show'" in m for m in msgs)


def test_use_trait_requires_workspace_pipeline_in_single_module_loader(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	file_a = mod_root / "a.drift"
	file_b = mod_root / "b.drift"
	_write_file(
		file_a,
		"""
module m_main;

import m_traits;

use trait m_traits.Show;

fn main() nothrow -> Int{ return 0; }
""",
	)
	_write_file(
		file_b,
		"""
module m_main;

fn other() nothrow -> Int{ return 1; }
""",
	)
	_module, _table, _exc, diagnostics = parse_drift_files_to_hir([file_a, file_b], package_id="test")
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("multiple source files declare one module" in m for m in msgs)


def test_multi_file_single_module_loader_is_error(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	file_a = mod_root / "a.drift"
	file_b = mod_root / "b.drift"
	_write_file(
		file_a,
		"""
module m_main;

fn a() nothrow -> Int{ return 1; }
""",
	)
	_write_file(
		file_b,
		"""
module m_main;

fn b() nothrow -> Int{ return 2; }
""",
	)
	_module, _table, _exc, diagnostics = parse_drift_files_to_hir([file_a, file_b], package_id="test")
	assert diagnostics
	msgs = [d.message for d in diagnostics]
	assert any("multiple source files declare one module" in m for m in msgs)
