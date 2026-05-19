# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
Regression: --dep foo@2.0.0 must not load/verify foo/1.0.0.

LANGUAGE_BUG: the package discovery prefilter only narrowed by package_id,
not by version.  All sibling versions were loaded and trust-verified, so a
malformed or untrusted older version could fail the build even when an exact
newer version was requested.
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_peekable_dmp(dest: Path, package_id: str, version: str, module_id: str) -> None:
	"""Create a minimal dmir-pkg-v0 binary that peek_package_id_and_version can read.

	The blob section is empty/invalid — full load will fail.  This is
	intentional: the test asserts that the compiler never attempts full
	load of the wrong version.
	"""
	from lang.driftc.packages.dmir_pkg_v0 import MAGIC, VERSION, HEADER_SIZE_V0
	manifest = json.dumps({
		"format": "dmir-pkg",
		"format_version": 0,
		"package_id": package_id,
		"package_version": version,
		"target": "drift-dev",
		"abi_fingerprint": "test",
		"unsigned": True,
		"unstable_format": True,
		"payload_kind": "provisional-dmir",
		"payload_version": 0,
		"modules": [{"module_id": module_id, "interface_sha256": "bad", "payload_sha256": "bad"}],
		"blobs": [],
	}).encode("utf-8")
	import hashlib
	manifest_hash = hashlib.sha256(manifest).digest()
	# dmir-pkg-v0 header: 8s magic, H version, H flags, I header_size,
	#   Q manifest_len, 32s manifest_hash, Q toc_entries, I toc_entry_size,
	#   32s toc_hash, 64s reserved
	header = struct.pack(
		"<8sHHI Q 32s Q I 32s 64s",
		MAGIC, VERSION, 0, HEADER_SIZE_V0,
		len(manifest), manifest_hash,
		0, 0, b"\0" * 32, b"\0" * 64,
	)
	dest.parent.mkdir(parents=True, exist_ok=True)
	dest.write_bytes(header + manifest)


class TestDepVersionPrefilter:

	def test_wrong_version_not_loaded(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
		"""
		Package root has test-dep/1.0.0 and test-dep/2.0.0 (both broken).
		Compile with --dep test-dep@2.0.0.

		With the bug: compiler loads 1.0.0, fails on it.
		With the fix: compiler only loads 2.0.0, never touches 1.0.0.

		We verify by patching load_package_v1_with_policy to record which
		paths are loaded.
		"""
		pkg_root = tmp_path / "libs"
		_make_peekable_dmp(
			pkg_root / "test-dep" / "1.0.0" / "test-dep.dmp",
			"test-dep", "1.0.0", "test.dep",
		)
		_make_peekable_dmp(
			pkg_root / "test-dep" / "2.0.0" / "test-dep.dmp",
			"test-dep", "2.0.0", "test.dep",
		)

		loaded_paths: list[str] = []
		_original_load = None

		def _tracking_load(path, **kwargs):
			loaded_paths.append(str(path))
			raise ValueError(f"intentional test failure loading {path.name}")

		from lang.driftc import driftc as driftc_mod
		mod_root = tmp_path / "mods"
		(mod_root / "main").mkdir(parents=True)
		(mod_root / "main" / "main.drift").write_text(
			"module main;\nfn main() nothrow -> Int { return 0; }\n"
		)
		ir_path = tmp_path / "out.ll"

		argv = [
			"-M", str(mod_root),
			str(mod_root / "main" / "main.drift"),
			"--stdlib-root", "stdlib",
			"--package-root", str(pkg_root),
			"--dep", "test-dep@2.0.0",
			"--emit-ir", str(ir_path),
		]

		with patch("lang.driftc.driftc.load_package_v1_with_policy", side_effect=_tracking_load):
			rc = driftc_mod.main(argv)

		# The build will fail because load raises — that's expected.
		# The key assertion: only 2.0.0 was loaded, never 1.0.0.
		assert any("2.0.0" in p for p in loaded_paths), (
			f"expected 2.0.0 to be loaded, but loaded: {loaded_paths}"
		)
		assert not any("1.0.0" in p for p in loaded_paths), (
			f"1.0.0 must NOT be loaded when --dep test-dep@2.0.0 is specified, "
			f"but loaded: {loaded_paths}"
		)

	def test_path_manifest_version_mismatch_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
		"""
		Package at <root>/foo/2.0.0/foo.dmp with manifest claiming 1.0.0
		must produce an identity mismatch error, not "version not found".
		"""
		from lang.driftc.driftc import main as driftc_main

		pkg_root = tmp_path / "libs"
		# Create package at standard layout path for 2.0.0...
		_make_peekable_dmp(
			pkg_root / "test-dep" / "2.0.0" / "test-dep.dmp",
			"test-dep", "1.0.0",  # manifest claims 1.0.0 — mismatch!
			"test.dep",
		)

		mod_root = tmp_path / "mods"
		(mod_root / "main").mkdir(parents=True)
		(mod_root / "main" / "main.drift").write_text(
			"module main;\nfn main() nothrow -> Int { return 0; }\n"
		)

		argv = [
			"-M", str(mod_root),
			str(mod_root / "main" / "main.drift"),
			"--stdlib-root", "stdlib",
			"--package-root", str(pkg_root),
			"--dep", "test-dep@2.0.0",
			"--emit-ir", str(tmp_path / "out.ll"),
			"--json",
		]
		rc = driftc_main(argv)
		assert rc != 0

		out = capsys.readouterr().out
		payload = json.loads(out) if out.strip() else {}
		diag_msg = payload.get("diagnostics", [{}])[0].get("message", "")
		assert "identity mismatch" in diag_msg or "package identity mismatch" in diag_msg, (
			f"expected identity mismatch error, got: {diag_msg}"
		)

	def test_path_manifest_package_id_mismatch_rejected(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
		"""
		Package at <root>/test-dep/2.0.0/test-dep.dmp with manifest claiming
		package_id 'wrong-pkg' must produce an identity mismatch error.
		"""
		from lang.driftc.driftc import main as driftc_main

		pkg_root = tmp_path / "libs"
		_make_peekable_dmp(
			pkg_root / "test-dep" / "2.0.0" / "test-dep.dmp",
			"wrong-pkg", "2.0.0",  # package_id mismatch!
			"test.dep",
		)

		mod_root = tmp_path / "mods"
		(mod_root / "main").mkdir(parents=True)
		(mod_root / "main" / "main.drift").write_text(
			"module main;\nfn main() nothrow -> Int { return 0; }\n"
		)

		argv = [
			"-M", str(mod_root),
			str(mod_root / "main" / "main.drift"),
			"--stdlib-root", "stdlib",
			"--package-root", str(pkg_root),
			"--dep", "test-dep@2.0.0",
			"--emit-ir", str(tmp_path / "out.ll"),
			"--json",
		]
		rc = driftc_main(argv)
		assert rc != 0

		out = capsys.readouterr().out
		payload = json.loads(out) if out.strip() else {}
		diag_msg = payload.get("diagnostics", [{}])[0].get("message", "")
		assert "identity mismatch" in diag_msg, (
			f"expected package_id identity mismatch error, got: {diag_msg}"
		)
