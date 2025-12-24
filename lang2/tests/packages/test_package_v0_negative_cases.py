# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from lang2.driftc.driftc import main as driftc_main
from lang2.driftc.packages.provider_v0 import load_package_v0


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def test_load_package_rejects_bad_blob_hash(tmp_path: Path) -> None:
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

	# Corrupt the manifest in-place (still valid JSON but changes bytes).
	with zipfile.ZipFile(pkg, "a") as zf:
		manifest = zf.read("manifest.json").decode("utf-8")
		zf.writestr("manifest.json", manifest.replace("\"unsigned\":true", "\"unsigned\":false"))

	with pytest.raises(ValueError, match="blob sha256 mismatch|manifest references unknown blob"):
		load_package_v0(pkg)


def test_driftc_rejects_duplicate_module_id_across_packages(tmp_path: Path) -> None:
	# Create two packages that both provide module `lib`.
	for n in (1, 2):
		root = tmp_path / f"p{n}"
		_write_file(
			root / "lib" / "lib.drift",
			f"""
module lib

export {{ add }}

fn add(a: Int, b: Int) returns Int {{
	return a + b + {n}
}}
""".lstrip(),
		)
		pkg = tmp_path / f"lib{n}.dmp"
		assert driftc_main(["-M", str(root), str(root / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	# Compile a main module with both packages in the same package root.
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
	# Point package-root at tmp_path so both lib1.dmp and lib2.dmp are discovered.
	rc = driftc_main(["-M", str(tmp_path), "--package-root", str(tmp_path), str(tmp_path / "main.drift"), "--emit-ir", str(tmp_path / "out.ll")])
	assert rc != 0


def test_driftc_rejects_type_table_fingerprint_mismatch(tmp_path: Path) -> None:
	# Build a package that forces Float into its TypeTable.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { f }

pub fn f() returns Float {
	return 1.0
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	# Now compile a main module that does not use Float otherwise. MVP rule requires
	# identical type-table fingerprints, so this should be rejected.
	_write_file(
		tmp_path / "main.drift",
		"""
module main

import lib as lib

fn main() returns Int {
	val x = lib.f()
	return 0
}
""".lstrip(),
	)
	rc = driftc_main(["-M", str(tmp_path), "--package-root", str(tmp_path), str(tmp_path / "main.drift"), "--emit-ir", str(tmp_path / "out.ll")])
	assert rc != 0

