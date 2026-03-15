# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(with_target_word_bits(argv + ["--json"]))
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_callinfo_collected_inside_try_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"struct S { v: Int }",
				"implement S {",
				"	fn get(self: &S) nothrow -> Int {",
				"		return self.v;",
				"	}",
				"}",
				"fn f() nothrow -> Int {",
				"	val s = S(v = 1);",
				"	try {",
				"		val _ = s.get();",
				"	} catch {",
				"	}",
				"	return 0;",
				"}",
				"fn main() nothrow -> Int {",
				"	return f();",
				"}",
				"",
			]
		)
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_callinfo_for_inline_lambda_callsite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"fn main() nothrow -> Int {",
				"	val v = (|x| => x + 1)(1);",
				"	return v;",
				"}",
				"",
			]
		)
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc == 0
	assert payload.get("diagnostics", []) == []
