# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def _run_driftc_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip() or "{}"
	payload = json.loads(out)
	_ = capsys.readouterr()
	return res.returncode, payload


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


@pytest.mark.heavy
def test_emit_ir_map_literal_move_value_no_noncanonical_move_assert(
	tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main;

import std.core as core;
import std.log as log;
import std.concurrent as conc;

struct Document {
	name: String,
	size: Int,
}

implement log.Debuggable for Document {
	pub fn to_debug_json_text(self: &Document) nothrow -> String {
		return core.diagnostic_json_int(self.size);
	}
}

fn main() nothrow -> Int {
	var cfgb = log.config_builder();
	cfgb.sink(log.stderr_sink());
	cfgb.min_level(log.Level::Debug());
	cfgb.enqueue_timeout(conc.Duration(millis = 1));
	cfgb.write_timeout(conc.Duration(millis = 1));
	val cfg = cfgb.build();
	val lg = log.create_logger("main", move cfg);
	val doc = Document(name = "contract.pdf", size = 42);
	lg.debug("document-indexed", {"doc": move doc});
	return 0;
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	rc, payload = _run_driftc_json(
		["-M", str(mod_root), *map(str, paths), "--emit-ir", str(tmp_path / "out.ll")], capsys
	)
	assert rc == 0, payload
