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
	_modules, _table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		paths,
		module_paths=[mod_root],
		stdlib_root=stdlib_root(),
	)
	return diagnostics


def test_require_rejects_value_subject_under_or(tmp_path: Path) -> None:
	diags = _parse_workspace(
		tmp_path,
		"""
module main;

trait A { fn a(self: Self) -> Int }
trait B { fn b(self: Self) -> Int }

fn f<T>(x: T) -> Int require (T is A or x is B) {
	return 0;
}
""".lstrip(),
	)
	assert diags
	assert any(d.code == "E-REQUIRE-UNKNOWN-SUBJECT" for d in diags)


def test_require_rejects_self_under_not(tmp_path: Path) -> None:
	diags = _parse_workspace(
		tmp_path,
		"""
module main;

trait A { fn a(self: Self) -> Int }

fn f<T>(x: T) -> Int require not (Self is A) {
	return 0;
}
""".lstrip(),
	)
	assert diags
	assert any(d.code == "E-REQUIRE-UNKNOWN-SUBJECT" for d in diags)
