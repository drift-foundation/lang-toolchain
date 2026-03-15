# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.driftc import main as driftc_main, host_word_bits
from lang.driftc.packages import dmir_pkg_v0
from lang.driftc.packages.provider_v0 import load_package_v0


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


def test_load_package_rejects_bad_blob_hash(tmp_path: Path) -> None:
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
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), *_emit_pkg_args("lib"), "--emit-package", str(pkg)]) == 0

	# Corrupt a blob byte in-place so its SHA256 no longer matches the TOC.
	data = pkg.read_bytes()
	header_bytes = data[: dmir_pkg_v0.HEADER_SIZE_V0]
	(
		_magic, _version, _flags, _header_size,
		manifest_len, _manifest_sha,
		toc_len, _toc_entry_size, _toc_sha, _reserved,
	) = dmir_pkg_v0._HEADER_STRUCT.unpack(header_bytes)
	blob_start = dmir_pkg_v0.HEADER_SIZE_V0 + int(manifest_len) + (int(toc_len) * dmir_pkg_v0.TOC_ENTRY_SIZE_V0)
	assert blob_start < len(data), "no blob region to corrupt"
	# Flip one byte in the first blob.
	corrupted = bytearray(data)
	corrupted[blob_start] ^= 0xFF
	pkg.write_bytes(bytes(corrupted))

	with pytest.raises(ValueError, match="blob sha256 mismatch"):
		load_package_v0(pkg)


def test_driftc_rejects_duplicate_module_id_across_packages(tmp_path: Path) -> None:
	# Create two packages that both provide module `lib`.
	for n in (1, 2):
		root = tmp_path / f"p{n}"
		_write_file(
			root / "lib" / "lib.drift",
			f"""
module lib;

export {{ add }};

pub fn add(a: Int, b: Int) -> Int {{
	return a + b + {n};
}}
""".lstrip(),
		)
		pkg = tmp_path / f"lib{n}.dmp"
		assert driftc_main(["-M", str(root), str(root / "lib" / "lib.drift"), *_emit_pkg_args(f"lib{n}"), "--emit-package", str(pkg)]) == 0

	# Compile a main module with both packages in the same package root.
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	return lib.add(40, 2);
}
""".lstrip(),
	)
	# Point package-root at tmp_path so both lib1.dmp and lib2.dmp are discovered.
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
	# Build a package that forces Float into its TypeTable.
	_write_file(
		tmp_path / "lib" / "lib.drift",
		"""
module lib;

export { f };

pub fn f() -> Float {
	return 1.0;
}
""".lstrip(),
	)
	pkg = tmp_path / "lib.dmp"
	assert driftc_main(["-M", str(tmp_path), str(tmp_path / "lib" / "lib.drift"), *_emit_pkg_args("lib"), "--emit-package", str(pkg)]) == 0

	# Now compile a main module that does not use Float otherwise. MVP rule requires
	# identical type-table fingerprints, so this should be rejected.
	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib as lib;

fn main() nothrow -> Int{
	val x = lib.f();
	return 0;
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


def test_driftc_rejects_abi_fingerprint_mismatch_across_packages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
	for n in (1, 2):
		root = tmp_path / f"p{n}"
		_write_file(
			root / "lib" / "lib.drift",
			f"""
module lib{n};

export {{ add }};

pub fn add(a: Int, b: Int) -> Int {{
	return a + b + {n};
}}
""".lstrip(),
		)
		pkg = tmp_path / f"lib{n}.dmp"
		assert driftc_main(["-M", str(root), str(root / "lib" / "lib.drift"), *_emit_pkg_args(f"lib{n}"), "--emit-package", str(pkg)]) == 0

	# Corrupt the ABI fingerprint in the second package manifest without changing length.
	expected_inline = (host_word_bits() // 8) * 4
	def _pick_same_len_value(value: int) -> int:
		candidate = value + 1
		if len(str(candidate)) == len(str(value)):
			return candidate
		candidate = value - 1
		if candidate > 0 and len(str(candidate)) == len(str(value)):
			return candidate
		raise ValueError("unable to select same-length ABI inline bytes override")

	new_inline = _pick_same_len_value(expected_inline)
	old_token = f"\"iface_inline_bytes\":{expected_inline}"
	new_token = f"\"iface_inline_bytes\":{new_inline}"

	def patch_manifest(old: bytes) -> bytes:
		text = old.decode("utf-8")
		if old_token not in text:
			raise ValueError("expected iface_inline_bytes in manifest")
		return text.replace(old_token, new_token).encode("utf-8")

	_patch_pkg_manifest_bytes_same_len(tmp_path / "lib2.dmp", patch_manifest)

	_write_file(
		tmp_path / "main.drift",
		"""
module main;

import lib1 as lib1;
import lib2 as lib2;

fn main() nothrow -> Int{
	return lib1.add(20, 1) + lib2.add(20, 1);
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
	out = capsys.readouterr().err
	assert rc != 0
	assert "ABI fingerprint mismatch across packages in build" in out
