# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

from lang2.driftc.driftc import main as driftc_main


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_import_from_package_root_compiles_to_ir(tmp_path: Path) -> None:
	# Build a package that provides module `lib`.
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

	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	# Compile a main module that imports `lib` from the package root.
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

	ir_path = tmp_path / "out.ll"
	assert driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(ir_path),
		]
	) == 0

	ir = ir_path.read_text(encoding="utf-8")
	assert "define i64 @lib::add" in ir

