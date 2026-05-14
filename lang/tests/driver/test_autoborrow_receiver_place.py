# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import stdlib_root
from lang.codegen.llvm.test_utils import sanitizer_timeout


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


def test_autoborrow_shared_receiver_allows_rvalue_place_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Inner { value: Int }

implement Inner {
	pub fn get(self: &Inner) nothrow -> Int { return self.value; }
}

struct Wrap { inner: Inner }

fn make() -> Wrap {
	return Wrap(inner = Inner(value = 1));
}

fn main() nothrow -> Int {
	return make().inner.get();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_shared_receiver_allows_ref_returning_rvalue_chain(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Shared receiver chain where the intermediate rvalue is *already
	a `&T`* (not an owned value): `node() -> &Inner` then
	`.get(&self: &Inner)`.  No autoborrow is needed — the
	intermediate ref already matches the method's `&self` — but
	the pre-fix check at type_checker.py:8489-8499 required an
	addressable place and rejected the rvalue ref-returning call
	with "borrow requires an addressable place; bind to a local
	first".

	The sibling test `..._allows_rvalue_place_chain` covers the
	`make() -> Wrap` (owned rvalue) → `.field` (place) → `.get()`
	shape; this test covers the distinct `f() -> &T` (rvalue ref)
	→ `.method()` shape, which surfaced against std.json's
	`payload.node().get_string_at_path(...)` idiom in the
	bookkeeper tree.
	"""
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Inner { value: Int }

implement Inner {
	pub fn get(self: &Inner) nothrow -> Int { return self.value; }
}

struct Outer { inner: Inner }

implement Outer {
	pub fn node(self: &Outer) nothrow -> &Inner { return &self.inner; }
}

fn main() nothrow -> Int {
	val o = Outer(inner = Inner(value = 42));
	return o.node().get();
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0, payload


def test_autoborrow_mut_rvalue_chain_terminates_without_resolver_recursion(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

struct Builder { x: Int }

implement Builder {
	pub fn step(self: &Builder) nothrow -> Builder {
		return Builder(x = self.x);
	}

	pub fn finish(self: &mut Builder) nothrow -> Int {
		self.x = self.x + 1;
		return self.x;
	}
}

fn make() nothrow -> Builder {
	return Builder(x = 0);
}

fn main() nothrow -> Int {
	val _ = make().step().step().finish();
	return 0;
}
""".lstrip(),
	)
	main_path = mod_root / "main" / "main.drift"
	cmd = [sys.executable, "-m", "lang.driftc", "-M", str(mod_root), str(main_path), "--dev", "--json"]
	root = stdlib_root()
	if root:
		cmd.insert(3, "--stdlib-root")
		cmd.insert(4, str(root))
	try:
		res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True, timeout=sanitizer_timeout(20))
	except subprocess.TimeoutExpired:
		pytest.fail("driftc compile timed out (possible resolver recursion on rvalue mut receiver chain)")
	payload = json.loads(res.stdout) if res.stdout.strip() else {}
	assert res.returncode != 0
	diags = payload.get("diagnostics", [])
	assert any("borrow requires an addressable place" in str(d.get("message", "")) for d in diags)