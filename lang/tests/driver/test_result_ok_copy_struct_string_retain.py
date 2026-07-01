from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip()
	if not out:
		out = "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_result_ok_struct_string_binding_does_not_emit_retain_in_main(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.core as core;

struct Payload {
	msg: String
}

fn make() nothrow -> core.Result<Payload, Int> {
	return core.Result::Ok(Payload(msg = "hello"));
}

pub fn main() nothrow -> Int {
	match make() {
		core.Result::Ok(v) => {
			if v.msg != "hello" { return 1; }
			return 0;
		},
		core.Result::Err(_) => { return 2; }
	}
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	ir_path = tmp_path / "out.ll"
	rc, payload = _run_driftc_json(
		["--no-prelude", "-M", str(mod_root), *map(str, paths), "--emit-ir", str(ir_path)],
		capsys,
	)
	assert rc == 0, payload
	ir = ir_path.read_text()
	main_start = ir.find("define i64 @drift_main()")
	assert main_start >= 0
	main_end = ir.find("\n}", main_start)
	assert main_end > main_start
	main_ir = ir[main_start:main_end]
	assert "@drift_string_retain(" not in main_ir
	assert "@drift_string_release(" in main_ir
