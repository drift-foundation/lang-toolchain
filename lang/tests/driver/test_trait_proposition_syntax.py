# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _parse_workspace(tmp_path: Path, source: str):
	mod_root = tmp_path / "mods"
	src = mod_root / "main.drift"
	_write_file(src, source)
	paths = sorted(mod_root.rglob("*.drift"))
	_modules, _type_table, _exc_catalog, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	return diagnostics


def test_trait_proposition_rejected_in_value_position(tmp_path: Path) -> None:
	diags = _parse_workspace(
		tmp_path,
		"""
module main

pub trait Debug { fn debug(self: &Self) -> Int; }

fn foo<T>() nothrow -> Int{
	val x = (T is Debug);
	return 0;
}
""".lstrip(),
	)
	assert diags
	assert any(d.phase == "parser" and d.code == "E-TRAIT-PROP-VALUE-POS" for d in diags)


def test_trait_proposition_allowed_in_if_guard(tmp_path: Path) -> None:
	diags = _parse_workspace(
		tmp_path,
		"""
module main

pub trait Debug { fn debug(self: &Self) -> Int; }

fn foo<T>() nothrow -> Int{
	if (T is Debug) {
		return 1;
	}
	return 0;
}
""".lstrip(),
	)
	assert diags == []


def test_trait_proposition_allowed_in_require_clause(tmp_path: Path) -> None:
	diags = _parse_workspace(
		tmp_path,
		"""
module main

pub trait Debug { fn debug(self: &Self) -> Int; }

fn foo<T>() nothrow -> Int require T is Debug {
	return 0;
}
""".lstrip(),
	)
	assert diags == []
