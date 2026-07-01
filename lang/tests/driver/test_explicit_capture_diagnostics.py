# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	rc = driftc_main(argv + ["--json"])
	out = capsys.readouterr().out
	payload = json.loads(out) if out.strip() else {}
	return rc, payload


def _compile_single_module(
	tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> tuple[int, dict]:
	root = tmp_path / "mods"
	main_path = root / "m_main" / "main.drift"
	_write_file(main_path, source)
	argv = ["-M", str(root), str(main_path)]
	return _run_driftc_json(argv, capsys)


def test_explicit_capture_missing_root_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	val x = 1;
	val y = 2;
	return (| | captures(copy x) => { return y; })();
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("value used in closure body is not listed in captures(...)" in m for m in msgs)


def test_explicit_capture_duplicate_root_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	val x = 1;
	return (| | captures(&x, &x) => { return 0; })();
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("duplicate capture in captures(...) list" in m for m in msgs)


def test_explicit_capture_param_collision_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	val x = 1;
	return (|x: Int| captures(&x) => { return x; })(1);
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("capture name collides with lambda param/local" in m for m in msgs)


def test_explicit_capture_shared_write_requires_mut_capture(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	var x = 1;
	return (| | captures(&x) => { x += 1; return 0; })();
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("capture &mut x" in m for m in msgs)


def test_explicit_capture_borrow_escape_via_store_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	var x = 1;
	val f = | | captures(&mut x) => { return 0; };
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("borrowed captures are non-escaping in v0" in m for m in msgs)


def test_explicit_capture_borrow_escape_via_return_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

fn make() -> Int {
	var x = 1;
	return | | captures(&x) => { return 0; };
}

pub fn main() nothrow -> Int{
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("borrowed captures are non-escaping in v0" in m for m in msgs)


def test_explicit_capture_borrow_escape_via_call_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

fn sink<T>(cb: T) -> Int {
	return 0;
}

pub fn main() nothrow -> Int{
	var x = 1;
	return sink(| | captures(&x) => { return 0; });
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("borrowed captures are non-escaping in v0" in m for m in msgs)


def test_explicit_capture_value_escape_allowed(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	val x = 1;
	val f = | | captures(copy x) => { return x; };
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc == 0
	assert payload.get("diagnostics", []) == []


def test_explicit_capture_copy_non_copyable_reports_driver_diag(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

struct Box { value: Array<Int> }

pub fn main() nothrow -> Int{
	val b = Box(value = [1]);
	val f = | | captures(copy b) => { return 0; };
	return 0;
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc != 0
	msgs = [d.get("message", "") for d in payload.get("diagnostics", [])]
	assert any("cannot copy 'b': type is not Copy" in m for m in msgs)


def test_explicit_empty_captures_allows_lambda_param_usage(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	source = """
module m_main;

pub fn main() nothrow -> Int{
	return (|x: Int| captures() => { return x; })(1);
}
"""
	rc, payload = _compile_single_module(tmp_path, capsys, source)
	assert rc == 0, payload
