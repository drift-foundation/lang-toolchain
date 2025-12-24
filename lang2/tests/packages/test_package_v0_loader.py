# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc.driftc import main as driftc_main
from lang2.driftc.packages.provider_v0 import load_package_v0


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_load_package_v0_round_trip(tmp_path: Path) -> None:
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

	out = tmp_path / "p.dmp"
	argv = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(out)]
	assert driftc_main(argv) == 0

	pkg = load_package_v0(out)
	assert pkg.manifest["kind"] == "drift-package"
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert set(pkg.modules_by_id.keys()) == {"lib", "main", "lang.core"}

	lib_iface = pkg.modules_by_id["lib"].interface
	assert lib_iface["module_id"] == "lib"
	assert "add" in lib_iface["exports"]["values"]

