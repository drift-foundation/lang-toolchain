# vim: set noexpandtab: -*- indent-tabs-mode: t -*-
"""
DMIR-PKG v0 reader (tooling-side).

This is a tiny, deterministic, streaming-friendly container used by `driftc` and
the `drift` tool. `drift` deliberately does not import `lang2.driftc.*`, so this
module duplicates the minimal header/manifest parsing logic needed for package
manager workflows (publish/fetch/vendor).

This reader is intentionally small:
- it validates the header magic and manifest hash,
- it decodes the manifest JSON,
- it exposes identity fields (`package_id`, `package_version`, `target`).

It does not attempt to replace `driftc`'s full loader or trust enforcement.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MAGIC = b"DMIRPKG\0"
VERSION = 0

# Keep the binary layout pinned to the tooling spec. See
# `docs/design/drift-tooling-and-packages.md`.
_HEADER_STRUCT = struct.Struct("<8sHHI Q 32s Q I 32s 64s")
HEADER_SIZE_V0 = _HEADER_STRUCT.size


def sha256_bytes(data: bytes) -> bytes:
	return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
	return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PackageIdentity:
	package_id: str
	package_version: str
	target: str
	manifest: dict[str, Any]


class PackageFormatError(ValueError):
	pass


def read_manifest_v0(pkg_path: Path) -> dict[str, Any]:
	data = pkg_path.read_bytes()
	if len(data) < HEADER_SIZE_V0:
		raise PackageFormatError("package file too small for header")

	(
		magic,
		version,
		_flags,
		_header_size,
		manifest_len,
		manifest_sha,
		_toc_len,
		_toc_entry_size,
		_toc_sha,
		_reserved,
	) = _HEADER_STRUCT.unpack(data[:HEADER_SIZE_V0])
	if magic != MAGIC:
		raise PackageFormatError("bad package magic")
	if version != VERSION:
		raise PackageFormatError(f"unsupported package version {version}")

	manifest_off = HEADER_SIZE_V0
	manifest_end = manifest_off + int(manifest_len)
	if manifest_end > len(data):
		raise PackageFormatError("manifest length out of range")

	manifest_bytes = data[manifest_off:manifest_end]
	if sha256_bytes(manifest_bytes) != manifest_sha:
		raise PackageFormatError("manifest sha256 mismatch")

	try:
		obj = json.loads(manifest_bytes.decode("utf-8"))
	except Exception as err:
		raise PackageFormatError(f"invalid manifest JSON: {err}") from err
	if not isinstance(obj, dict):
		raise PackageFormatError("manifest must be a JSON object")
	return obj


def read_identity_v0(pkg_path: Path) -> PackageIdentity:
	manifest = read_manifest_v0(pkg_path)
	pid = manifest.get("package_id")
	pver = manifest.get("package_version")
	target = manifest.get("target")
	if not isinstance(pid, str) or not pid:
		raise PackageFormatError("package manifest missing package_id")
	if not isinstance(pver, str) or not pver:
		raise PackageFormatError("package manifest missing package_version")
	if not isinstance(target, str) or not target:
		raise PackageFormatError("package manifest missing target")
	return PackageIdentity(package_id=pid, package_version=pver, target=target, manifest=manifest)


def canonical_json_bytes(obj: Mapping[str, Any]) -> bytes:
	"""
Deterministic JSON encoding used for indexes/locks.

Rules:
- UTF-8
- no insignificant whitespace
- stable key ordering
	"""
	return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

