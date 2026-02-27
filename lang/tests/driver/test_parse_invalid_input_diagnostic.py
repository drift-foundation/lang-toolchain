# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.parser import parse_drift_workspace_to_hir


def test_llvm_ir_input_reports_not_valid_drift(tmp_path: Path) -> None:
	f = tmp_path / "main.drift"
	f.write_text('%DriftString = type { ptr, i64 }\n', encoding="utf-8")
	_modules, _table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir([f], module_paths=[tmp_path])
	errors = [d for d in diags if d.severity == "error"]
	assert errors, diags
	msg = str(errors[0].message)
	assert "not valid Drift source" in msg, msg
	assert ".drift file" in msg, msg
	# raw parser detail is still present for debugging
	assert "parse detail" in msg, msg


def test_json_input_reports_not_valid_drift(tmp_path: Path) -> None:
	f = tmp_path / "main.drift"
	f.write_text('{"json": true}\n', encoding="utf-8")
	_modules, _table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir([f], module_paths=[tmp_path])
	errors = [d for d in diags if d.severity == "error"]
	assert errors, diags
	msg = str(errors[0].message)
	assert "not valid Drift source" in msg, msg
	assert ".drift file" in msg, msg


def test_valid_drift_syntax_error_does_not_say_invalid_input(tmp_path: Path) -> None:
	f = tmp_path / "main.drift"
	f.write_text(
		"""
module main

fn main() nothrow -> Int {
	val x = 42
	return x;
}
""".lstrip(),
		encoding="utf-8",
	)
	_modules, _table, _exc, _exports, _deps, diags = parse_drift_workspace_to_hir([f], module_paths=[tmp_path])
	errors = [d for d in diags if d.severity == "error"]
	assert errors, diags
	msg = str(errors[0].message)
	assert "not valid Drift source" not in msg, msg
