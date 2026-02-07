# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_module(root: Path, rel: str, src: str) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(src)


def _parse_workspace(root: Path) -> list[str]:
	paths = sorted(root.rglob("*.drift"))
	_modules, _tt, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[root],
		stdlib_root=stdlib_root(),
	)
	return [d.message for d in diagnostics]


def test_export_non_pub_rejected(tmp_path: Path) -> None:
	root = tmp_path / "mods"
	_write_module(
		root,
		"m/lib.drift",
		"""
module m

fn helper() -> Int { return 0; }
export { helper };
""",
	)
	messages = _parse_workspace(root)
	assert any("cannot export 'helper' from module 'm': symbol is not public" in m for m in messages)


def test_export_star_unknown_module_rejected(tmp_path: Path) -> None:
	root = tmp_path / "mods"
	_write_module(
		root,
		"m/lib.drift",
		"""
module m

export { missing.* };
""",
	)
	messages = _parse_workspace(root)
	assert any("re-exports unknown module 'missing'" in m for m in messages)


def test_export_star_collision_rejected(tmp_path: Path) -> None:
	root = tmp_path / "mods"
	_write_module(
		root,
		"a/lib.drift",
		"""
module a

pub fn x() -> Int { return 1; }
export { x };
""",
	)
	_write_module(
		root,
		"b/lib.drift",
		"""
module b

pub fn x() -> Int { return 2; }
export { x };
""",
	)
	_write_module(
		root,
		"m/lib.drift",
		"""
module m

export { a.*, b.* };
""",
	)
	messages = _parse_workspace(root)
	assert any("exported name 'x' is ambiguous due to re-exports" in m for m in messages)


def test_export_star_explicit_collision_rejected(tmp_path: Path) -> None:
	root = tmp_path / "mods"
	_write_module(
		root,
		"a/lib.drift",
		"""
module a

pub fn x() -> Int { return 1; }
export { x };
""",
	)
	_write_module(
		root,
		"m/lib.drift",
		"""
module m

pub fn x() -> Int { return 2; }
export { a.*, x };
""",
	)
	messages = _parse_workspace(root)
	assert any("exported name 'x' is ambiguous due to re-exports" in m for m in messages)
