# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang.driftc.driftc import main as driftc_main
from lang.driftc.packages.provider_v1 import load_package_v1


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_load_package_v0_round_trip(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

pub fn main() nothrow -> Int{
	return lib.add(40, 2);
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { add };

pub fn add(a: Int, b: Int) -> Int {
	return a + b;
}
""".lstrip(),
	)

	out = tmp_path / "p.dmp"
	argv = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--package-id", "test.loader", "--package-version", "0.0.0", "--package-target", "test-target", "--emit-package", str(out)]
	assert driftc_main(argv) == 0

	pkg = load_package_v1(out)
	assert pkg.manifest["format"] == "dmir-pkg"
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert "lib" in pkg.modules_by_id
	assert "main" in pkg.modules_by_id

	lib_iface = pkg.modules_by_id["lib"].interface
	assert lib_iface["module_id"] == "lib"
	assert "add" in lib_iface["exports"]["values"]

