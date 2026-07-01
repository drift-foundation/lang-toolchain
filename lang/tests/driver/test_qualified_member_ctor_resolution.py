# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main
from lang.driftc.parser import parse_drift_workspace_to_hir, stdlib_root
from lang.tests.driver.driver_cli_helpers import with_target_word_bits


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	if "--dev" not in argv:
		argv = [*argv, "--dev"]
	rc = driftc_main(with_target_word_bits(argv + ["--json"]))
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def test_qualified_member_ctor_resolves_core_variant(tmp_path: Path) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"pub fn main() nothrow -> Void {",
				"	val a: Optional<Int> = Optional::None();",
				"	val b: Optional<Int> = Optional::Some(1);",
				"	_ = a;",
				"	_ = b;",
				"}",
				"",
			]
		)
	)

	_modules, _table, _exc, _exports, _deps, diagnostics = parse_drift_workspace_to_hir(
		[src],
		stdlib_root=stdlib_root(),
		package_id="app",
	)

	assert diagnostics == []


def test_qualified_member_ctor_requires_expected_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"pub fn main() nothrow -> Void {",
				"	val x = Optional::None();",
				"	_ = x;",
				"}",
				"",
			]
		)
	)

	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("E-QMEM-CANNOT-INFER" in (d.get("message") or "") for d in diags)


def test_qualified_member_ctor_rejects_missing_ctor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	src = tmp_path / "main.drift"
	src.write_text(
		"\n".join(
			[
				"module main;",
				"pub fn main() nothrow -> Void {",
				"	val x: Optional<Int> = Optional::Bogus();",
				"	_ = x;",
				"}",
				"",
			]
		)
	)

	rc, payload = _run_driftc_json(["-M", str(tmp_path), "--stdlib-root", str(stdlib_root()), str(src)], capsys)
	assert rc != 0
	diags = payload.get("diagnostics", [])
	assert any("E-QMEM-NO-CTOR" in (d.get("message") or "") for d in diags)
