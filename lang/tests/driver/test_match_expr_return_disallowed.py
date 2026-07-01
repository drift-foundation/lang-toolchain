# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_return_disallowed_in_match_expr_value_arm_reports_targeted_diagnostic(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	src = tmp_path / "match_expr_return_disallowed.drift"
	_write_file(
		src,
		"""
module main;

import std.core as core;

fn f(r: core.Result<Int, Int>) -> core.Result<Int, Int> {
	val x = match r {
		Ok(v) => { v },
		Err(e) => { return core.Result::Err(e); }
	};
	return core.Result::Ok(x);
}

pub fn main() nothrow -> Int { return 0; }
""".lstrip(),
	)
	rc, payload = _run_driftc_json(["-M", str(tmp_path), str(src)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", []) if isinstance(payload, dict) else []
	assert any(d.get("code") == "E_EXPR_BLOCK_MISSING_VALUE" for d in diags), diags
	# The message must clearly attribute the failure to a disallowed `return`
	# in an expression-form block and point at the statement-form remedy.
	assert any(
		"is not allowed in an expression-form block" in str(d.get("message", ""))
		and "statement form" in str(d.get("message", ""))
		for d in diags
	), diags
