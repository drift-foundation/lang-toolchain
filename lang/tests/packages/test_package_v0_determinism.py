# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import zipfile
from pathlib import Path

from lang.driftc.driftc import main as driftc_main, host_word_bits
from lang.driftc.packages.provider_v0 import load_package_v0


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _emit_pkg_args(package_id: str) -> list[str]:
	return [
		"--package-id",
		package_id,
		"--package-version",
		"0.0.0",
		"--package-target",
		"test-target",
	]


def test_emit_package_is_deterministic(tmp_path: Path) -> None:
	# Layout matches the module-path inference rules:
	# - files directly under the module root infer module_id == "main"
	# - files under <root>/lib infer module_id == "lib"
	_write_file(
		tmp_path / "main.drift",
		"""
module main

import lib as lib;

fn main() nothrow -> Int{
	return lib.add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add };

pub fn add(a: Int, b: Int) -> Int {
	return a + b
}
""".lstrip(),
	)

	out1 = tmp_path / "p1.dmp"
	out2 = tmp_path / "p2.dmp"

	argv = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), *_emit_pkg_args("test.determinism"), "--emit-package"]

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


def test_manifest_reports_iface_inline_bytes(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main

fn main() nothrow -> Int{
	return 0;
}
""".lstrip(),
	)
	out = tmp_path / "p.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "main.drift"), *_emit_pkg_args("test.inline-bytes"), "--emit-package", str(out)]) == 0

	pkg = load_package_v0(out)
	abi = pkg.manifest.get("abi_fingerprint")
	assert isinstance(abi, dict)
	expected_inline = (host_word_bits() // 8) * 4
	assert abi.get("iface_inline_bytes") == expected_inline
