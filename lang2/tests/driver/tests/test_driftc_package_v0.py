# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lang2.driftc.driftc import main as driftc_main
from lang2.driftc.packages import dmir_pkg_v0
from lang2.driftc.packages.provider_v0 import discover_package_files
from lang2.driftc.packages.provider_v0 import load_package_v0


def _write_file(path: Path, text: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _patch_file_bytes(path: Path, offset: int, patch: bytes) -> None:
	data = path.read_bytes()
	if offset < 0 or offset + len(patch) > len(data):
		raise ValueError("patch out of range")
	new_data = data[:offset] + patch + data[offset + len(patch) :]
	path.write_bytes(new_data)


def _patch_pkg_header(path: Path, *, manifest_sha256: bytes | None = None, toc_sha256: bytes | None = None) -> None:
	header_bytes = path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		magic,
		version,
		flags,
		header_size,
		manifest_len,
		manifest_sha,
		toc_len,
		toc_entry_size,
		toc_sha,
		reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	if manifest_sha256 is not None:
		manifest_sha = manifest_sha256
	if toc_sha256 is not None:
		toc_sha = toc_sha256
	new_header = dmir_pkg_v0._HEADER_STRUCT.pack(
		magic,
		version,
		flags,
		header_size,
		manifest_len,
		manifest_sha,
		toc_len,
		toc_entry_size,
		toc_sha,
		reserved,
	)
	_patch_file_bytes(path, 0, new_header)


def _patch_pkg_manifest_bytes_same_len(path: Path, patch_fn) -> None:
	"""
	Patch the manifest bytes in-place without changing `manifest_len`.

	This helper is intentionally strict: it requires the new bytes to be the same
	length as the old bytes so TOC offsets remain valid.
	"""
	header_bytes = path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		_magic,
		_version,
		_flags,
		_header_size,
		manifest_len,
		_manifest_sha,
		_toc_len,
		_toc_entry_size,
		_toc_sha,
		_reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	manifest_off = dmir_pkg_v0.HEADER_SIZE_V0
	old = path.read_bytes()[manifest_off : manifest_off + int(manifest_len)]
	new = patch_fn(old)
	if len(new) != len(old):
		raise ValueError("manifest patch must not change length")
	_patch_file_bytes(path, manifest_off, new)
	_patch_pkg_header(path, manifest_sha256=dmir_pkg_v0.sha256_bytes(new))


def test_emit_package_is_deterministic(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)

	out1 = tmp_path / "p1.dmp"
	out2 = tmp_path / "p2.dmp"

	argv_common = ["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package"]
	assert driftc_main(argv_common + [str(out1)]) == 0
	assert driftc_main(argv_common + [str(out2)]) == 0

	assert out1.read_bytes() == out2.read_bytes()

	pkg = load_package_v0(out1)
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert pkg.manifest["payload_version"] == 0
	assert pkg.manifest["unstable_format"] is True


def test_load_package_v0_round_trip(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)

	out = tmp_path / "p.dmp"
	assert driftc_main(
		["-M", str(tmp_path), str(tmp_path / "main.drift"), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(out)]
	) == 0

	pkg = load_package_v0(out)
	assert pkg.manifest["payload_kind"] == "provisional-dmir"
	assert set(pkg.modules_by_id.keys()) >= {"lib", "main"}

	lib_iface = pkg.modules_by_id["lib"].interface
	assert lib_iface["module_id"] == "lib"
	assert "add" in lib_iface["exports"]["values"]


def test_load_package_rejects_bad_blob_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	# Load once to discover a concrete blob offset, then corrupt the blob bytes.
	pkg_ok = load_package_v0(pkg_path)
	assert pkg_ok.toc, "package should have at least one blob"
	blob = pkg_ok.toc[0]
	# Flip one byte at the start of the blob.
	orig = pkg_path.read_bytes()[blob.offset : blob.offset + 1]
	_patch_file_bytes(pkg_path, blob.offset, bytes([orig[0] ^ 0xFF]))

	with pytest.raises(ValueError, match="blob sha256 mismatch for"):
		load_package_v0(pkg_path)


def test_load_package_rejects_bad_manifest_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	_patch_pkg_header(pkg_path, manifest_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="manifest sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_bad_toc_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	_patch_pkg_header(pkg_path, toc_sha256=b"\0" * 32)
	with pytest.raises(ValueError, match="toc sha256 mismatch"):
		load_package_v0(pkg_path)


def test_load_package_rejects_duplicate_toc_blob_hash(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_path = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg_path)]) == 0

	# Duplicate the first TOC entry's sha256 into the second entry.
	header_bytes = pkg_path.read_bytes()[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		_magic,
		_version,
		_flags,
		_header_size,
		manifest_len,
		_manifest_sha,
		toc_len,
		_toc_entry_size,
		_toc_sha,
		_reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	assert toc_len >= 2
	toc_start = dmir_pkg_v0.HEADER_SIZE_V0 + int(manifest_len)
	first_entry_off = toc_start
	second_entry_off = toc_start + dmir_pkg_v0.TOC_ENTRY_SIZE_V0
	first_sha = pkg_path.read_bytes()[first_entry_off : first_entry_off + 32]
	_patch_file_bytes(pkg_path, second_entry_off, first_sha)

	# Update toc_sha256 in the header so we reach TOC parsing.
	toc_bytes = pkg_path.read_bytes()[toc_start : toc_start + int(toc_len) * dmir_pkg_v0.TOC_ENTRY_SIZE_V0]
	_patch_pkg_header(pkg_path, toc_sha256=dmir_pkg_v0.sha256_bytes(toc_bytes))

	with pytest.raises(ValueError, match="duplicate blob sha256 in toc"):
		load_package_v0(pkg_path)


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

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_rejects_type_table_fingerprint_mismatch(tmp_path: Path) -> None:
	# Package defines an extra user type that is not present in the consuming build.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { f }

struct S(x: Int)

fn f() returns Int {
	return 1
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import f

fn main() returns Int {
	val x = f()
	return 0
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--allow-unsigned-from",
			str(tmp_path),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_package_embedding_includes_only_call_graph_closure(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}

fn unused() returns Int {
	return 999
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	ir_path = tmp_path / "out.ll"
	assert (
		driftc_main(
			[
				"-M",
				str(tmp_path),
				"--package-root",
				str(tmp_path),
				"--allow-unsigned-from",
				str(tmp_path),
				str(tmp_path / "main.drift"),
				"--emit-ir",
				str(ir_path),
			]
		)
		== 0
	)
	ir = ir_path.read_text(encoding="utf-8")
	assert "lib::unused" not in ir
	assert "define i64 @\"lib::add\"" in ir
	assert "define i64 @lib::unused" not in ir


def test_discover_package_files_accepts_package_file_path(tmp_path: Path) -> None:
	pkg = tmp_path / "one.dmp"
	pkg.write_bytes(b"")
	assert discover_package_files([pkg]) == [pkg]


def test_driftc_rejects_unsigned_package_outside_allowlist(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)

	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_missing_explicit_trust_store_is_reported_as_diagnostic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	_write_file(
		tmp_path / "main.drift",
		"""
module main

fn main() returns Int {
	return 0
}
""".lstrip(),
	)
	missing = tmp_path / "nope-trust.json"
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(tmp_path),
			"--trust-store",
			str(missing),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
			"--json",
		]
	)
	assert rc != 0
	out = capsys.readouterr().out
	obj = json.loads(out)
	assert obj["exit_code"] == 1
	assert obj["diagnostics"][0]["phase"] == "package"
	assert "trust store not found" in obj["diagnostics"][0]["message"]


def test_driftc_rejects_unsigned_package_without_manifest_marker(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	# Remove the "unsigned": true marker without changing manifest length.
	def patch_manifest(old: bytes) -> bytes:
		needle = b"\"unsigned\":true"
		if needle not in old:
			raise ValueError("expected unsigned marker in manifest")
		return old.replace(needle, b"\"unsigned\":null")

	_patch_pkg_manifest_bytes_same_len(pkg, patch_manifest)

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0


def test_driftc_require_signatures_rejects_unsigned_packages(tmp_path: Path) -> None:
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib

export { add }

fn add(a: Int, b: Int) returns Int {
	return a + b
}
""".lstrip(),
	)
	pkg_root = tmp_path / "pkgs"
	pkg_root.mkdir(parents=True, exist_ok=True)
	pkg = pkg_root / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), "--emit-package", str(pkg)]) == 0

	_write_file(
		tmp_path / "main.drift",
		"""
module main

from lib import add

fn main() returns Int {
	return add(40, 2)
}
""".lstrip(),
	)
	rc = driftc_main(
		[
			"-M",
			str(tmp_path),
			"--package-root",
			str(pkg_root),
			"--allow-unsigned-from",
			str(pkg_root),
			"--require-signatures",
			str(tmp_path / "main.drift"),
			"--emit-ir",
			str(tmp_path / "out.ll"),
		]
	)
	assert rc != 0
