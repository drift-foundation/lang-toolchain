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


def test_std_text_tokenizer_api_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.text as text;

struct S {
	pub count: Int,
}

implement text.TokenConsumer<String, Int> for S {
	pub fn on_token(self: &mut S, tok: String, span: text.TokenSpan) nothrow -> text.TokenizeAction {
		val _ = tok;
		val _ = span;
		self.count = self.count + 1;
		return text.TokenizeAction::Continue();
	}

	pub fn on_error(self: &mut S, err: Int, at: Int) nothrow -> text.TokenizeAction {
		val _ = err;
		val _ = at;
		return text.TokenizeAction::Stop();
	}
}

pub fn main() nothrow -> Int {
	var s = S(count = 0);
	val c: text.TokenConsumer<String, Int> = s;
	val sp = text.TokenSpan(start = 0, end = 1);
	val _ = c.on_token("x", sp);
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(["-M", str(mod_root), *map(str, paths)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
