from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


def _run_driftc_json(argv: list[str]) -> tuple[int, dict]:
	cmd = ["./.venv/bin/python3", "-m", "lang.driftc.driftc", *argv, "--json"]
	res = subprocess.run(cmd, cwd=Path(__file__).parents[3], capture_output=True, text=True)
	out = res.stdout.strip()
	if not out:
		out = "{}"
	return res.returncode, json.loads(out)


def _write_file(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def test_result_variant_payload_is_sized_for_forward_nominal_ok_arm(tmp_path: Path) -> None:
	mod_root = tmp_path / "mods"
	_write_file(
		mod_root / "wire" / "types.drift",
		"""
module wire.types

pub struct Payload {
	pub a: Int,
	pub b: Int,
	pub c: Int,
	pub d: Int,
	pub e: Int,
	pub f: Int,
	pub flag: Bool
}

export { Payload };
""".lstrip(),
	)
	_write_file(
		mod_root / "wire" / "lib.drift",
		"""
module wire

import std.core as core;
import wire.types as types;

pub type P = types.Payload;
export { mk };

pub fn mk() nothrow -> core.Result<P, Int> {
	return core.Result::Ok(types.Payload(a = 11, b = 12, c = 13, d = 14, e = 15, f = 16, flag = false));
}
""".lstrip(),
	)
	_write_file(
		mod_root / "main" / "main.drift",
		"""
module main

import std.core as core;
import wire as wire;

fn main() nothrow -> Int {
	match wire.mk() {
		core.Result::Ok(v) => {
			if v.flag { return 1; }
			return 0;
		},
		core.Result::Err(_) => { return 2; }
	}
}
""".lstrip(),
	)
	paths = sorted(mod_root.rglob("*.drift"))
	ir_path = tmp_path / "out.ll"
	rc, payload = _run_driftc_json(["--no-prelude", "-M", str(mod_root), *map(str, paths), "--emit-ir", str(ir_path)])
	assert rc == 0, payload
	ir = ir_path.read_text()
	fn_match = re.search(r'define\s+(%Variant_[^\s]+)\s+@"wire::mk__impl"\(', ir)
	assert fn_match is not None, "wire::mk__impl not found in IR"
	variant_ty = re.escape(fn_match.group(1))
	variant_decl = re.search(rf"^{variant_ty}\s*=\s*type\s*\{{\s*i8,\s*\[[0-9]+\s+x\s+i8\],\s*\[([0-9]+)\s+x\s+i64\]\s*\}}$", ir, re.MULTILINE)
	assert variant_decl is not None, f"variant declaration for {fn_match.group(1)} not found"
	payload_words = int(variant_decl.group(1))
	assert payload_words >= 7, f"expected payload words >= 7 for large Payload Ok arm, got {payload_words}"
