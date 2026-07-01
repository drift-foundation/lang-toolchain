# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	root = stdlib_root()
	args = list(argv)
	if root:
		args += ["--stdlib-root", str(root)]
	args += ["--dev"]
	args += ["--json"]
	rc = driftc_main(args)
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_interface_value_dispatch_compiles(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

interface Action {
	fn run(self: &Self, x: Int) nothrow -> Int;
}

struct Impl {
}

implement Action for Impl {
	pub fn run(self: &Impl, x: Int) nothrow -> Int {
		val _ = self;
		return x + 1;
	}
}

pub fn main() nothrow -> Int {
	val a: Action = Impl();
	return a.run(41);
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_interface_ufcs_dispatch_reports_explicit_unsupported_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

interface Action {
	fn run(self: &Self, x: Int) nothrow -> Int;
}

struct Impl {
}

implement Action for Impl {
	pub fn run(self: &Impl, x: Int) nothrow -> Int {
		val _ = self;
		return x + 1;
	}
}

pub fn main() nothrow -> Int {
	var v = Impl();
	return Action::run(&mut v, 41);
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 1
	diags = payload.get("diagnostics", [])
	msgs = [d.get("message", "") for d in diags]
	assert any("UFCS interface dispatch is not supported yet" in m for m in msgs)
	assert not any("missing CallInfo" in m for m in msgs)
