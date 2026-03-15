# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_workspace_parse_smoke(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	_write_file(
		src,
		"""
module main;

fn main() nothrow -> Int { return 0; }
""".lstrip(),
	)
	paths = sorted(tmp_path.rglob("*.drift"))
	_modules, _table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir(
		paths,
		module_paths=[tmp_path],
		stdlib_root=stdlib_root(),
	)
	assert diags == []


def test_workspace_parse_order_is_deterministic(tmp_path: Path) -> None:
	a = tmp_path / "a.drift"
	b = tmp_path / "b.drift"
	_write_file(
		a,
		"""
module main;

fn dup() nothrow -> Int { return 1; }
""".lstrip(),
	)
	_write_file(
		b,
		"""
module main;

fn dup() nothrow -> Int { return 2; }
""".lstrip(),
	)
	paths_a = [a, b]
	paths_b = [b, a]
	*_rest, diags_a = parse_drift_workspace_to_hir(paths_a, module_paths=[tmp_path], stdlib_root=stdlib_root())
	*_rest, diags_b = parse_drift_workspace_to_hir(paths_b, module_paths=[tmp_path], stdlib_root=stdlib_root())

	def _diag_key(d):
		span = getattr(d, "span", None)
		return (
			d.code,
			d.message,
			getattr(span, "file", None),
			getattr(span, "line", None),
			getattr(span, "column", None),
		)

	assert [_diag_key(d) for d in diags_a] == [_diag_key(d) for d in diags_b]
