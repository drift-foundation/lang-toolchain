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
		# Some paths may only write to stderr; normalize to empty json.
		out = "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_emit_ir_with_stdlib_import_has_no_internal_callinfo_errors(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.net as net;

pub fn main() nothrow -> Int {
	val _a = net.socket_addr("127.0.0.1", 5555);
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys
	)
	assert rc == 0
	diags = payload.get("diagnostics", [])
	assert not any("missing CallInfo" in d.get("message", "") for d in diags)
