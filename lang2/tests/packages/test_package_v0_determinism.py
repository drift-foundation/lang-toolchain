# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import zipfile
from pathlib import Path

from lang2.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_emit_package_is_deterministic(tmp_path: Path) -> None:
	# Layout matches the module-path inference rules:
	# - files directly under the module root infer module_id == "main"
	# - files under <root>/lib infer module_id == "lib"
	_write_file(
		tmp_path / "main.drift",
		"""
module main

import lib as lib

fn main() returns Int {
	return lib.add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

pub fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)

	out1 = tmp_path / "p1.dmp"
	out2 = tmp_path / "p2.dmp"

	argv = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package"]

	assert driftc_main(argv + [str(out1)]) == 0
	assert driftc_main(argv + [str(out2)]) == 0

	b1 = out1.read_bytes()
	b2 = out2.read_bytes()
	assert b1 == b2

	# Sanity: the artifact contains a deterministic manifest with the pinned markers.
	with zipfile.ZipFile(out1) as zf:
		manifest = zf.read("manifest.json").decode("utf-8")
		assert "\"payload_kind\":\"provisional-dmir\"" in manifest
		assert "\"payload_version\":0" in manifest
		assert "\"unstable_format\":true" in manifest

