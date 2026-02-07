# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _write_require_modules(tmp_path: Path, *, import_impls: bool) -> list[Path]:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "lib" / "lib.drift",
		"""
module lib

export { Show, id };

pub trait Show {
	fn show(self: Self) -> Int
}

pub fn id<T>(var x: T) nothrow -> T require T is Show {
	return move x;
}
""".lstrip(),
	)
	_write_file(
		mod_root / "types" / "types.drift",
		"""
module types

export { Bad };

pub struct Bad { pub x: Int }
""".lstrip(),
	)
	_write_file(
		mod_root / "impls" / "impls.drift",
		"""
module impls

import lib;
import types;

implement lib.Show for types.Bad {
	pub fn show(self: types.Bad) -> Int { return 0; }
}
""".lstrip(),
	)
	import_impls_line = "import impls;\n" if import_impls else ""
	_write_file(
		mod_root / "main" / "main.drift",
		f"""
module main

import lib;
import types;
{import_impls_line}
fn main() nothrow -> Int {{
	try lib.id(types.Bad(x = 1)) catch {{
		val _ = types.Bad(x = 0);
	}}
	return 0;
}}
""".lstrip(),
	)
	return sorted(mod_root.rglob("*.drift"))


def test_require_usesite_import_allows_impl(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	paths = _write_require_modules(tmp_path, import_impls=True)
	rc, payload = _run_driftc_json(["-M", str(tmp_path / "mods"), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
	assert payload.get("exit_code", 0) == 0


def test_require_usesite_missing_import_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	paths = _write_require_modules(tmp_path, import_impls=False)
	rc, payload = _run_driftc_json(["-M", str(tmp_path / "mods"), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	codes = [d.get("code") for d in diags]
	assert "E_REQUIREMENT_NOT_SATISFIED" in codes
