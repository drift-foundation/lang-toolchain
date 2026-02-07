# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_stdlib_exports_include_array_borrow_iter(tmp_path: Path) -> None:
	root = stdlib_root()
	assert root is not None
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_main/main.drift",
		"""
module m_main

fn main() nothrow -> Int { return 0; }
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _type_table, _exc_catalog, module_exports, _module_deps, diagnostics = (
		parse_drift_workspace_to_hir(
			paths,
			module_paths=[mod_root],
			stdlib_root=root,
		)
	)
	assert diagnostics == []
	std_exports = module_exports.get("std.containers")
	assert std_exports is not None
	structs = std_exports.get("types", {}).get("structs", [])
	aliases = std_exports.get("types", {}).get("aliases", [])
	assert "ArrayBorrowIter" in structs
	assert "HashMap" in structs or "HashMap" in aliases
	assert "HashSet" in structs or "HashSet" in aliases


def test_stdlib_exports_include_hash_traits(tmp_path: Path) -> None:
	root = stdlib_root()
	assert root is not None
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_main/main.drift",
		"""
module m_main

fn main() nothrow -> Int { return 0; }
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _type_table, _exc_catalog, module_exports, _module_deps, diagnostics = (
		parse_drift_workspace_to_hir(
			paths,
			module_paths=[mod_root],
			stdlib_root=root,
		)
	)
	assert diagnostics == []
	hash_exports = module_exports.get("std.core.hash")
	assert hash_exports is not None
	traits = hash_exports.get("traits", [])
	assert "Hash" in traits
	assert "Hasher" in traits
	assert "BuildHasher" in traits


def test_stdlib_exports_include_callback_traits_and_interfaces(tmp_path: Path) -> None:
	root = stdlib_root()
	assert root is not None
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "m_main/main.drift",
		"""
module m_main

fn main() nothrow -> Int { return 0; }
""",
	)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _type_table, _exc_catalog, module_exports, _module_deps, diagnostics = (
		parse_drift_workspace_to_hir(
			paths,
			module_paths=[mod_root],
			stdlib_root=root,
		)
	)
	assert diagnostics == []
	core_exports = module_exports.get("std.core")
	assert core_exports is not None
	traits = core_exports.get("traits", [])
	interfaces = core_exports.get("types", {}).get("interfaces", [])
	assert "Fn0" in traits
	assert "Fn1" in traits
	assert "Fn2" in traits
	assert "Callback0" in interfaces
	assert "Callback1" in interfaces
	assert "Callback2" in interfaces
