# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""Container-level tests for asset blobs in a DMIR-PKG v0 package.

Covers:
  - assets round-trip: `manifest.assets` + content-addressed blobs load
    back into `LoadedPackage.assets` with verified bytes;
  - BACKWARD COMPATIBILITY (the load-bearing additive-tolerance claim):
    adding asset blobs + a `manifest.assets` field does NOT perturb module
    decoding — a code consumer that ignores assets sees byte-identical
    modules, and the loader does not reject the new blob type / field;
  - malformed `manifest.assets` (missing blob, len mismatch, unsafe path,
    duplicate path) is rejected with `PackageMetadataError`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lang.driftc.packages.dmir_pkg_v0 import (
	BLOB_TYPE_ASSET,
	PackageMetadataError,
	canonical_json_bytes,
	load_dmir_pkg_v0,
	sha256_hex,
	write_dmir_pkg_v0,
)

_IFACE = {"exports": {}}
_PAYLOAD = {"dmir": "stub"}


def _shas() -> tuple[bytes, str, bytes, str]:
	ib = canonical_json_bytes(_IFACE)
	pb = canonical_json_bytes(_PAYLOAD)
	return ib, sha256_hex(ib), pb, sha256_hex(pb)


def _base_manifest_and_blobs():
	ib, ish, pb, psh = _shas()
	manifest = {
		"format": "dmir-pkg", "format_version": 0,
		"package_id": "p", "package_version": "1.0.0",
		"target": "drift-dev",
		"modules": [{
			"module_id": "p.m", "exports": {},
			"interface_blob": f"sha256:{ish}", "payload_blob": f"sha256:{psh}",
		}],
		"blobs": {
			f"sha256:{ish}": {"type": "exports", "length": len(ib)},
			f"sha256:{psh}": {"type": "dmir", "length": len(pb)},
		},
	}
	blobs = {ish: ib, psh: pb}
	blob_types = {ish: 2, psh: 1}
	blob_names = {ish: "iface:p.m", psh: "dmir:p.m"}
	return manifest, blobs, blob_types, blob_names


def _add_asset(manifest, blobs, blob_types, blob_names, *, path: str, content: bytes):
	sha = sha256_hex(content)
	blobs[sha] = content
	blob_types[sha] = BLOB_TYPE_ASSET
	blob_names[sha] = f"asset:{path}"
	manifest["blobs"][f"sha256:{sha}"] = {"type": "asset", "length": len(content)}
	manifest.setdefault("assets", []).append({"path": path, "blob": f"sha256:{sha}", "len": len(content)})
	return sha


def _write(tmp_path: Path, manifest, blobs, blob_types, blob_names) -> Path:
	p = tmp_path / "p.dmp"
	write_dmir_pkg_v0(p, manifest_obj=manifest, blobs=blobs, blob_types=blob_types, blob_names=blob_names)
	return p


def test_assets_round_trip(tmp_path: Path) -> None:
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	_add_asset(manifest, blobs, bt, bn, path="assets/db/a.sql", content=b"CREATE TABLE a();\n")
	_add_asset(manifest, blobs, bt, bn, path="assets/db/b.sql", content=b"CREATE TABLE b();\n")
	lp = load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))
	got = {a.path: lp.blobs_by_sha256[a.sha256] for a in lp.assets}
	assert got == {
		"assets/db/a.sql": b"CREATE TABLE a();\n",
		"assets/db/b.sql": b"CREATE TABLE b();\n",
	}
	for a in lp.assets:
		assert a.length == len(lp.blobs_by_sha256[a.sha256])


def test_assets_do_not_perturb_module_decoding(tmp_path: Path) -> None:
	"""Backward compatibility: a code consumer sees identical modules whether
	or not assets are present, and the loader tolerates the asset blob/field."""
	m0, b0, t0, n0 = _base_manifest_and_blobs()
	lp_plain = load_dmir_pkg_v0(_write(tmp_path / "plain", m0, b0, t0, n0))

	m1, b1, t1, n1 = _base_manifest_and_blobs()
	_add_asset(m1, b1, t1, n1, path="assets/db/a.sql", content=b"sql\n")
	lp_assets = load_dmir_pkg_v0(_write(tmp_path / "withassets", m1, b1, t1, n1))

	# Modules decode identically — assets are invisible to code consumption.
	assert set(lp_plain.modules_by_id) == set(lp_assets.modules_by_id)
	for mid in lp_plain.modules_by_id:
		assert lp_plain.modules_by_id[mid].interface == lp_assets.modules_by_id[mid].interface
		assert lp_plain.modules_by_id[mid].payload == lp_assets.modules_by_id[mid].payload
	# Plain package has no assets; asset package has exactly one.
	assert lp_plain.assets == []
	assert len(lp_assets.assets) == 1


def test_asset_missing_blob_rejected(tmp_path: Path) -> None:
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	# `manifest.assets` references a blob that is NOT packed (and not claimed
	# in manifest.blobs/TOC, so the manifest↔TOC consistency check passes and
	# the asset guard is what fires).
	bogus = "0" * 64
	manifest["assets"] = [{"path": "assets/x", "blob": f"sha256:{bogus}", "len": 3}]
	with pytest.raises(PackageMetadataError):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))


def test_asset_len_mismatch_rejected(tmp_path: Path) -> None:
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	sha = _add_asset(manifest, blobs, bt, bn, path="assets/x", content=b"abc")
	# Corrupt the declared length in manifest.assets only.
	manifest["assets"][0]["len"] = 999
	with pytest.raises(PackageMetadataError):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))


def test_asset_unsafe_path_rejected(tmp_path: Path) -> None:
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	_add_asset(manifest, blobs, bt, bn, path="assets/ok", content=b"x")
	# Rewrite the stored logical path to an escaping one.
	manifest["assets"][0]["path"] = "../escape"
	with pytest.raises(PackageMetadataError):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))


def test_asset_aliasing_code_blob_rejected(tmp_path: Path) -> None:
	"""An asset entry may not point at a DMIR/interface (code) blob — that
	would let a malformed signed package have `drift unpack` write a code
	blob to disk."""
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	_ib, ish, pb, psh = _shas()
	# Point an asset at the DMIR payload blob (type 1), which IS present and
	# whose length matches — only the blob-type check should reject it.
	manifest["assets"] = [{"path": "assets/x", "blob": f"sha256:{psh}", "len": len(pb)}]
	with pytest.raises(PackageMetadataError, match="TOC type"):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))


def test_asset_sha_colliding_with_code_blob_rejected(tmp_path: Path) -> None:
	"""Content-addressing SHA collision: an asset whose bytes equal a module's
	payload (code) blob shares one TOC row.  Even though packing flipped the
	row's type to ASSET, the loader must reject it because the same sha is also
	a module interface/payload blob — a blob may be code OR asset, not both."""
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	_ib, _ish, pb, _psh = _shas()
	# Asset content identical to the module's DMIR payload blob → same sha.
	_add_asset(manifest, blobs, bt, bn, path="assets/collide", content=pb)
	with pytest.raises(PackageMetadataError, match="collision"):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))


def test_asset_duplicate_path_rejected(tmp_path: Path) -> None:
	manifest, blobs, bt, bn = _base_manifest_and_blobs()
	_add_asset(manifest, blobs, bt, bn, path="assets/dup", content=b"one")
	_add_asset(manifest, blobs, bt, bn, path="assets/dup", content=b"two")
	with pytest.raises(PackageMetadataError):
		load_dmir_pkg_v0(_write(tmp_path, manifest, blobs, bt, bn))
