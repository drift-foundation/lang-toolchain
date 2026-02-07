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


def test_unqualified_call_prefers_free_function_over_method(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

fn write() -> Int { return 1; }

struct Foo { }

implement Foo {
	fn write(self: &Foo) -> String { return "x"; }
	fn f(self: &Foo) -> Int { return write(); }
}

fn main() -> Int {
	var foo = Foo();
	return foo.f();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_unqualified_call_does_not_resolve_to_method(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

struct Foo { }

implement Foo {
	fn write(self: &Foo) -> Int { return 2; }
	fn f(self: &Foo) -> Int { return write(self); }
}

fn main() -> Int {
	var foo = Foo();
	return foo.f();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("no matching overload for function 'write'" in d.get("message", "") for d in diags)


def test_local_binding_shadows_free_function(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

fn write() -> Int { return 1; }

struct Foo { }

implement Foo {
	fn f(self: &Foo) -> Int {
		val write: Int = 3;
		return write();
	}
}

fn main() -> Int {
	var foo = Foo();
	return foo.f();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("call target is not a function value" in d.get("message", "") for d in diags)
